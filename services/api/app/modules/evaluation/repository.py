"""All SQL of the evaluation module."""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.calibration.models import CalibrationRun, RunResult, RunSample
from app.modules.evaluation.models import (
    Evaluation,
    JudgePrompt,
    RunAggregate,
    TaskCheck,
)
from app.modules.evaluation.stats import Aggregate, ResultRow


@dataclass(frozen=True)
class JobData:
    """Everything one evaluation needs, read in one short transaction."""

    result_id: uuid.UUID
    run_id: uuid.UUID
    request_id: uuid.UUID
    model_id: uuid.UUID
    project_id: uuid.UUID
    task_type: str
    response: dict[str, Any] | None
    request: dict[str, Any]
    reference_response: dict[str, Any] | None
    expected: dict[str, Any] | None
    judge_model_id: uuid.UUID | None
    judge_pairwise: bool
    evaluation_status: str
    checks: list[TaskCheck]


class EvaluationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- checks and prompts ----
    async def checks_for(self, project_id: uuid.UUID, task_type: str) -> list[TaskCheck]:
        stmt = (
            select(TaskCheck)
            .where(TaskCheck.project_id == project_id, TaskCheck.task_type == task_type)
            .order_by(TaskCheck.position, TaskCheck.created_at, TaskCheck.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_check(self, project_id: uuid.UUID, check_id: uuid.UUID) -> TaskCheck | None:
        stmt = select(TaskCheck).where(TaskCheck.project_id == project_id, TaskCheck.id == check_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def active_prompt(self, kind: str) -> JudgePrompt | None:
        """The newest active version of a prompt kind."""
        stmt = (
            select(JudgePrompt)
            .where(JudgePrompt.kind == kind, JudgePrompt.active.is_(True))
            .order_by(JudgePrompt.version.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---- jobs ----
    async def load_job(self, result_id: uuid.UUID) -> JobData | None:
        stmt = (
            select(RunResult, CalibrationRun, RunSample)
            .join(CalibrationRun, CalibrationRun.id == RunResult.run_id)
            .join(
                RunSample,
                (RunSample.run_id == RunResult.run_id)
                & (RunSample.request_id == RunResult.request_id),
            )
            .where(RunResult.id == result_id)
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return None
        result, run, sample = row
        return JobData(
            result_id=result.id,
            run_id=run.id,
            request_id=result.request_id,
            model_id=result.model_id,
            project_id=run.project_id,
            task_type=run.task_type,
            response=result.response_payload,
            request=sample.request_payload,
            reference_response=sample.reference_response,
            expected=sample.expected,
            judge_model_id=run.judge_model_id,
            judge_pairwise=run.judge_pairwise,
            evaluation_status=run.evaluation_status,
            checks=await self.checks_for(run.project_id, run.task_type),
        )

    async def evaluation_exists(self, result_id: uuid.UUID) -> bool:
        stmt = select(Evaluation.id).where(Evaluation.result_id == result_id).limit(1)
        return (await self.session.execute(stmt)).first() is not None

    async def insert_evaluation(self, values: dict[str, Any]) -> bool:
        """Store a verdict; False if this result already has one for this prompt version."""
        stmt = (
            pg_insert(Evaluation)
            .values(id=uuid.uuid4(), **values)
            .on_conflict_do_nothing(constraint="uq_evaluations_result_prompt")
            .returning(Evaluation.id)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def count_evaluations(self, run_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Evaluation).where(Evaluation.run_id == run_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def ok_result_ids(self, run_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(RunResult.id)
            .where(RunResult.run_id == run_id, RunResult.status == "ok")
            .order_by(RunResult.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    # ---- aggregates ----
    async def result_rows(self, run_id: uuid.UUID) -> dict[uuid.UUID, list[ResultRow]]:
        sql = text(
            """
            SELECT r.model_id, r.status, r.prompt_tokens, r.completion_tokens, r.ttft_ms,
                   r.latency_ms, r.cost_usd,
                   e.status AS eval_status, e.quality, e.passed, e.valid_schema, e.basis,
                   e.judge_cost_usd
            FROM run_results r LEFT JOIN evaluations e ON e.result_id = r.id
            WHERE r.run_id = :run_id
            """
        )
        grouped: dict[uuid.UUID, list[ResultRow]] = {}
        for row in await self.session.execute(sql, {"run_id": run_id}):
            grouped.setdefault(row.model_id, []).append(
                ResultRow(
                    status=row.status,
                    prompt_tokens=row.prompt_tokens,
                    completion_tokens=row.completion_tokens,
                    ttft_ms=row.ttft_ms,
                    latency_ms=row.latency_ms,
                    cost_usd=None if row.cost_usd is None else Decimal(row.cost_usd),
                    eval_status=row.eval_status,
                    quality=row.quality,
                    passed=row.passed,
                    valid_schema=row.valid_schema,
                    basis=row.basis,
                    judge_cost_usd=None
                    if row.judge_cost_usd is None
                    else Decimal(row.judge_cost_usd),
                )
            )
        return grouped

    async def replace_aggregates(
        self, run_id: uuid.UUID, task_type: str, aggregates: dict[uuid.UUID, Aggregate]
    ) -> None:
        await self.session.execute(
            text("DELETE FROM run_aggregates WHERE run_id = :r"), {"r": run_id}
        )
        for model_id, agg in aggregates.items():
            self.session.add(
                RunAggregate(
                    run_id=run_id,
                    model_id=model_id,
                    task_type=task_type,
                    n_total=agg.n_total,
                    n_ok=agg.n_ok,
                    n_error=agg.n_error,
                    n_context_exceeded=agg.n_context_exceeded,
                    n_evaluated=agg.n_evaluated,
                    n_judge_failed=agg.n_judge_failed,
                    quality_mean=agg.quality_mean,
                    quality_p10=agg.quality_p10,
                    pass_rate=agg.pass_rate,
                    pass_rate_low=agg.pass_rate_low,
                    pass_rate_high=agg.pass_rate_high,
                    valid_rate=agg.valid_rate,
                    prompt_tokens_avg=agg.prompt_tokens_avg,
                    completion_tokens_avg=agg.completion_tokens_avg,
                    latency_p50_ms=agg.latency_p50_ms,
                    latency_p95_ms=agg.latency_p95_ms,
                    ttft_p50_ms=agg.ttft_p50_ms,
                    ttft_p95_ms=agg.ttft_p95_ms,
                    error_rate=agg.error_rate,
                    cost_per_request_usd=agg.cost_per_request_usd,
                    judge_cost_usd=agg.judge_cost_usd,
                    insufficient_data=agg.insufficient_data,
                    basis_counts=agg.basis_counts,
                )
            )
        await self.session.flush()

    async def list_aggregates(self, run_id: uuid.UUID) -> list[RunAggregate]:
        stmt = (
            select(RunAggregate)
            .where(RunAggregate.run_id == run_id)
            .order_by(RunAggregate.model_id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_evaluations(
        self,
        run_id: uuid.UUID,
        *,
        model_id: uuid.UUID | None,
        status: str | None,
        passed: bool | None,
        after: uuid.UUID | None,
        limit: int,
    ) -> list[tuple[Evaluation, uuid.UUID, uuid.UUID]]:
        """(evaluation, model id, request id) rows."""
        stmt = (
            select(Evaluation, RunResult.model_id, RunResult.request_id)
            .join(RunResult, RunResult.id == Evaluation.result_id)
            .where(Evaluation.run_id == run_id)
        )
        if model_id is not None:
            stmt = stmt.where(RunResult.model_id == model_id)
        if status is not None:
            stmt = stmt.where(Evaluation.status == status)
        if passed is not None:
            stmt = stmt.where(Evaluation.passed.is_(passed))
        if after is not None:
            stmt = stmt.where(Evaluation.id > after)
        stmt = stmt.order_by(Evaluation.id).limit(limit)
        return [(e, m, r) for e, m, r in (await self.session.execute(stmt)).all()]
