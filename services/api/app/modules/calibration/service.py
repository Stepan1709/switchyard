"""Calibration use cases of the control plane: start a run, steer it, read its progress, import
labelled examples. Jobs are not executed here: they are written to the outbox in the same
transaction as the run and the worker takes them from RabbitMQ."""

import hashlib
import json
import random
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, Conflict, NotFound, Unprocessable
from app.core.events import REPLAY_EXECUTE
from app.core.outbox import enqueue
from app.modules.audit import service as audit
from app.modules.auth.models import Admin
from app.modules.calibration import sampling
from app.modules.calibration.lifecycle import ACTIVE, complete_if_done, finish_run
from app.modules.calibration.models import CalibrationRun, RunSample
from app.modules.calibration.repository import CalibrationRepository
from app.modules.calibration.schemas import (
    EvaluationProgress,
    ImportReport,
    ResultOut,
    ResultPage,
    RunCreate,
    RunOut,
    RunProgress,
)
from app.modules.projects.models import Project
from app.modules.projects.repository import ProjectRepository
from app.modules.traffic.models import TaskType


class PayloadTooLarge(AppError):
    status_code = 413
    title = "Payload too large"
    code = "payload_too_large"


def content_hash(request_payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        request_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def parse_labeled_line(number: int, line: str) -> dict[str, Any]:
    """One JSONL line -> {"request_payload", "expected"}; raises ValueError with a message."""
    try:
        obj = json.loads(line)
    except ValueError as exc:
        raise ValueError(f"line {number}: not valid JSON") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"line {number}: must be a JSON object")
    messages = obj.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or not all(isinstance(m, dict) and isinstance(m.get("role"), str) for m in messages)
    ):
        raise ValueError(f"line {number}: 'messages' must be a non-empty list of messages")
    expected = obj.get("expected")
    if isinstance(expected, str):
        expected = {"content": expected}
    if not isinstance(expected, dict) or not (
        isinstance(expected.get("content"), str) or isinstance(expected.get("tool_calls"), list)
    ):
        raise ValueError(
            f"line {number}: 'expected' must be a string, "
            "or an object with 'content' or 'tool_calls'"
        )
    request = {
        k: v for k, v in obj.items() if k not in {"expected", "model", "stream", "stream_options"}
    }
    return {"request_payload": request, "expected": expected}


class CalibrationService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repo = CalibrationRepository(session)
        self.projects = ProjectRepository(session)

    async def _project(self, admin: Admin, project_id: uuid.UUID) -> Project:
        # not a member = "not found": a project must not be probeable by id
        project = await self.projects.get_for_admin(project_id, admin.id)
        if project is None:
            raise NotFound("Project not found")
        return project

    async def _run(self, admin: Admin, project_id: uuid.UUID, run_id: uuid.UUID) -> CalibrationRun:
        await self._project(admin, project_id)
        run = await self.repo.get_run_of_project(project_id, run_id)
        if run is None:
            raise NotFound("Calibration run not found")
        return run

    # ---- start ----
    async def create_run(
        self, admin: Admin, project_id: uuid.UUID, data: RunCreate, ip: str | None
    ) -> RunOut:
        project = await self._project(admin, project_id)
        if project.reference_model_id is None:
            raise Unprocessable("The project has no reference model; set one first")
        reference_id = project.reference_model_id
        if data.sample_size > self.settings.calibration_max_sample_size:
            raise Unprocessable(
                f"sample_size must not exceed {self.settings.calibration_max_sample_size}"
            )
        if data.since and data.until and data.since >= data.until:
            raise Unprocessable("'since' must be earlier than 'until'")

        task = (
            await self.session.execute(
                select(TaskType.id).where(
                    TaskType.project_id == project_id, TaskType.name == data.task_type
                )
            )
        ).first()
        if task is None:
            raise Unprocessable(f"Unknown task type '{data.task_type}' in this project")

        candidate_ids = list(dict.fromkeys(data.candidate_model_ids))  # unique, order kept
        if len(candidate_ids) > self.settings.calibration_max_candidates:
            raise Unprocessable(
                f"At most {self.settings.calibration_max_candidates} candidate models per run"
            )
        if reference_id in candidate_ids:
            raise Unprocessable("The reference model cannot be its own candidate")
        await self._check_candidates(candidate_ids)
        await self._check_judge(data, candidate_ids, reference_id)

        seed = data.seed if data.seed is not None else random.SystemRandom().randrange(2**31)
        pool: list[sampling.PoolItem] = []
        if data.source in ("traffic", "both"):
            pool += await self.repo.traffic_pool(
                project_id,
                data.task_type,
                reference_id,
                since=data.since,
                until=data.until,
                limit=self.settings.calibration_pool_limit,
            )
        if data.source in ("labeled", "both"):
            pool += await self.repo.labeled_pool(project_id, data.task_type)
        chosen = sampling.choose(pool, data.sample_size, data.strategy, seed)
        if not chosen:
            raise Unprocessable(
                "There is nothing to replay: no suitable recorded requests or labelled examples"
            )

        run = CalibrationRun(
            id=uuid.uuid4(),
            project_id=project_id,
            task_type=data.task_type,
            reference_model_id=reference_id,
            candidate_model_ids=candidate_ids,
            status="running",
            strategy=data.strategy,
            source=data.source,
            seed=seed,
            sample_size=data.sample_size,
            samples_taken=len(chosen),
            total_jobs=len(chosen) * len(candidate_ids),
            max_requests=data.max_requests,
            max_cost_usd=data.max_cost_usd,
            thresholds=data.thresholds.model_dump(exclude_none=True),
            judge_model_id=data.judge_model_id,
            judge_pairwise=data.judge_pairwise,
            evaluation_status="pending",
            created_by_id=admin.id,
        )
        self.session.add(run)
        await self.session.flush()  # the samples refer to the run

        await self._copy_samples(project_id, run.id, chosen)
        for item in chosen:
            for model_id in candidate_ids:
                enqueue(
                    self.session,
                    REPLAY_EXECUTE,
                    {
                        "run_id": str(run.id),
                        "request_id": str(item.request_id),
                        "model_id": str(model_id),
                    },
                    correlation_id=str(run.id),
                )
        audit.record(
            self.session,
            action="calibration_run.create",
            actor_id=admin.id,
            target_type="calibration_run",
            target_id=run.id,
            details={
                "project_id": str(project_id),
                "task_type": data.task_type,
                "candidates": [str(c) for c in candidate_ids],
                "samples": len(chosen),
                "jobs": run.total_jobs,
            },
            ip=ip,
        )
        await self.session.commit()
        return await self._out(run)

    async def _check_judge(
        self, data: RunCreate, candidate_ids: list[uuid.UUID], reference_id: uuid.UUID
    ) -> None:
        if data.judge_model_id is None:
            if data.judge_pairwise:
                raise Unprocessable("judge_pairwise needs a judge_model_id")
            return
        judge = data.judge_model_id
        # A model must not judge itself (it would favour its own style), and the reference is
        # the yardstick, not a referee of its own answers.
        if judge in candidate_ids:
            raise Unprocessable("The judge must not be one of the candidate models")
        if judge == reference_id:
            raise Unprocessable("The judge must not be the reference model")
        found = await self.repo.candidates([judge])
        model = found.get(judge)
        if model is None:
            raise Unprocessable("Unknown judge model")
        if not (model.enabled and model.provider_enabled):
            raise Unprocessable("The judge model is disabled")
        if judge not in await self.repo.models_with_prices([judge], datetime.now(UTC)):
            raise Unprocessable("The judge model has no current price")

    async def _check_candidates(self, candidate_ids: list[uuid.UUID]) -> None:
        found = await self.repo.candidates(candidate_ids)
        missing = [str(i) for i in candidate_ids if i not in found]
        if missing:
            raise Unprocessable(f"Unknown candidate models: {', '.join(missing)}")
        unusable = [c.name for c in found.values() if not (c.enabled and c.provider_enabled)]
        if unusable:
            raise Unprocessable(f"Disabled models cannot be candidates: {', '.join(unusable)}")
        priced = await self.repo.models_with_prices(candidate_ids, datetime.now(UTC))
        unpriced = [c.name for c in found.values() if c.model_id not in priced]
        if unpriced:
            raise Unprocessable(f"Models without a current price: {', '.join(unpriced)}")

    async def _copy_samples(
        self, project_id: uuid.UUID, run_id: uuid.UUID, chosen: list[sampling.PoolItem]
    ) -> None:
        traffic_ids = [i.request_id for i in chosen if i.source == "traffic"]
        labeled_ids = [i.request_id for i in chosen if i.source == "labeled"]
        if traffic_ids:
            for row in await self.repo.traffic_rows(project_id, traffic_ids):
                self.session.add(
                    RunSample(
                        run_id=run_id,
                        request_id=row.request_id,
                        source="traffic",
                        request_payload=row.request_payload,
                        reference_response=row.response_payload,
                        reference_prompt_tokens=row.prompt_tokens,
                        reference_completion_tokens=row.completion_tokens,
                    )
                )
        if labeled_ids:
            for sample in await self.repo.labeled_rows(project_id, labeled_ids):
                self.session.add(
                    RunSample(
                        run_id=run_id,
                        request_id=sample.id,
                        source="labeled",
                        request_payload=sample.request_payload,
                        expected=sample.expected,
                    )
                )
        await self.session.flush()

    # ---- read ----
    async def _progress(self, run: CalibrationRun) -> RunProgress:
        by_status = await self.repo.status_counts(run.id)
        spent, calls = await self.repo.spending(run.id)
        finished = sum(by_status.values())
        return RunProgress(
            total_jobs=run.total_jobs,
            finished=finished,
            percent=round(100 * finished / run.total_jobs, 1) if run.total_jobs else 100.0,
            by_status=by_status,
            calls=calls,
            spent_usd=spent,
            remaining_requests=None
            if run.max_requests is None
            else max(0, run.max_requests - calls),
            remaining_usd=None
            if run.max_cost_usd is None
            else max(run.max_cost_usd - spent, Decimal(0)),
        )

    async def _out(self, run: CalibrationRun) -> RunOut:
        out = RunOut.model_validate(run)
        out.progress = await self._progress(run)
        out.evaluation = EvaluationProgress(
            status=run.evaluation_status,
            total=run.evaluation_total,
            done=await self.repo.count_evaluations(run.id),
        )
        return out

    async def list_runs(self, admin: Admin, project_id: uuid.UUID, limit: int) -> list[RunOut]:
        await self._project(admin, project_id)
        return [await self._out(r) for r in await self.repo.list_runs(project_id, limit)]

    async def get_run(self, admin: Admin, project_id: uuid.UUID, run_id: uuid.UUID) -> RunOut:
        return await self._out(await self._run(admin, project_id, run_id))

    async def list_results(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        model_id: uuid.UUID | None,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> ResultPage:
        await self._run(admin, project_id, run_id)
        after: uuid.UUID | None = None
        if cursor is not None:
            try:
                after = uuid.UUID(cursor)
            except ValueError as exc:
                raise Unprocessable("The cursor is not valid") from exc
        rows = await self.repo.list_results(
            run_id, model_id=model_id, status=status, after=after, limit=limit + 1
        )
        page = rows[:limit]
        return ResultPage(
            items=[ResultOut.model_validate(r) for r in page],
            next_cursor=str(page[-1].id) if len(rows) > limit else None,
        )

    # ---- steer ----
    async def pause(
        self, admin: Admin, project_id: uuid.UUID, run_id: uuid.UUID, ip: str | None
    ) -> RunOut:
        run = await self._run(admin, project_id, run_id)
        if not await self.repo.transition(run.id, ("running",), "paused"):
            raise Conflict("Only a running run can be paused")
        return await self._steered(admin, run, "calibration_run.pause", ip)

    async def resume(
        self, admin: Admin, project_id: uuid.UUID, run_id: uuid.UUID, ip: str | None
    ) -> RunOut:
        run = await self._run(admin, project_id, run_id)
        if not await self.repo.transition(run.id, ("paused",), "running"):
            raise Conflict("Only a paused run can be resumed")
        # Every job may have finished while the run was paused: nobody else would complete it.
        await complete_if_done(self.session, self.repo, run.id)
        return await self._steered(admin, run, "calibration_run.resume", ip)

    async def cancel(
        self, admin: Admin, project_id: uuid.UUID, run_id: uuid.UUID, ip: str | None
    ) -> RunOut:
        run = await self._run(admin, project_id, run_id)
        if not await finish_run(
            self.session, self.repo, run.id, "cancelled", reason="cancelled by an administrator",
            from_statuses=ACTIVE,
        ):  # fmt: skip
            raise Conflict("The run has already finished")
        return await self._steered(admin, run, "calibration_run.cancel", ip)

    async def _steered(
        self, admin: Admin, run: CalibrationRun, action: str, ip: str | None
    ) -> RunOut:
        audit.record(
            self.session,
            action=action,
            actor_id=admin.id,
            target_type="calibration_run",
            target_id=run.id,
            ip=ip,
        )
        await self.session.commit()
        await self.session.refresh(run)
        return await self._out(run)

    # ---- labelled examples ----
    async def import_labeled(
        self, admin: Admin, project_id: uuid.UUID, task_type: str, raw: bytes, ip: str | None
    ) -> ImportReport:
        await self._project(admin, project_id)
        known = (
            await self.session.execute(
                select(TaskType.id).where(
                    TaskType.project_id == project_id, TaskType.name == task_type
                )
            )
        ).first()
        if known is None:
            raise NotFound("Task type not found")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Unprocessable("The file must be UTF-8 text (JSON Lines)") from exc
        lines = [(n, ln) for n, ln in enumerate(text.splitlines(), start=1) if ln.strip()]
        if not lines:
            raise Unprocessable("The file has no lines")
        if len(lines) > self.settings.calibration_import_max_lines:
            raise PayloadTooLarge(
                f"At most {self.settings.calibration_import_max_lines} lines per import"
            )

        problems: list[str] = []
        rows: dict[str, dict[str, Any]] = {}
        for number, line in lines:
            try:
                parsed = parse_labeled_line(number, line)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            digest = content_hash(parsed["request_payload"])
            rows.setdefault(
                digest,
                {
                    "id": uuid.uuid4(),
                    "project_id": project_id,
                    "task_type": task_type,
                    "content_hash": digest,
                    "created_by_id": admin.id,
                    **parsed,
                },
            )
        if problems:  # all or nothing: a half-imported file is hard to reason about
            shown = "; ".join(problems[:10])
            more = f" (and {len(problems) - 10} more)" if len(problems) > 10 else ""
            raise Unprocessable(f"Nothing was imported. {shown}{more}")

        imported = await self.repo.insert_labeled(list(rows.values()))
        audit.record(
            self.session,
            action="labeled_samples.import",
            actor_id=admin.id,
            target_type="project",
            target_id=project_id,
            details={"task_type": task_type, "lines": len(lines), "imported": imported},
            ip=ip,
        )
        await self.session.commit()
        return ImportReport(
            total=len(lines),
            imported=imported,
            duplicates=len(lines) - imported,
            available=await self.repo.count_labeled(project_id, task_type),
        )

    async def count_labeled(self, admin: Admin, project_id: uuid.UUID, task_type: str) -> int:
        await self._project(admin, project_id)
        return await self.repo.count_labeled(project_id, task_type)
