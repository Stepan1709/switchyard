"""All SQL of the calibration module."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.calibration.models import CalibrationRun, LabeledSample, RunResult, RunSample
from app.modules.calibration.pricing import Price
from app.modules.calibration.sampling import PoolItem
from app.modules.registry.models import Model, ModelPrice, Provider

# What counts as a real call to a provider (budget: number of requests).
CALL_STATUSES = ("ok", "error", "refused")


@dataclass(frozen=True)
class Candidate:
    """A model with the provider data needed to call it."""

    model_id: uuid.UUID
    name: str
    enabled: bool
    context_window: int
    max_output_tokens: int | None
    supports_stream: bool
    cost_type: str
    provider_id: uuid.UUID
    provider_enabled: bool
    base_url: str
    is_local: bool
    auth_type: str
    api_key_encrypted: str | None
    max_concurrency: int | None
    max_rps: Decimal | None


class CalibrationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- runs ----
    async def get_run(self, run_id: uuid.UUID, *, lock: bool = False) -> CalibrationRun | None:
        stmt = select(CalibrationRun).where(CalibrationRun.id == run_id)
        if lock:
            # FOR NO KEY UPDATE, not FOR UPDATE: inserting a result already holds the weaker
            # FOR KEY SHARE on this row (the foreign key check). Two transactions that hold
            # KEY SHARE and both ask for the stronger FOR UPDATE wait for each other: a
            # deadlock. NO KEY UPDATE is compatible with KEY SHARE and only excludes itself.
            stmt = stmt.with_for_update(key_share=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_run_of_project(
        self, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> CalibrationRun | None:
        stmt = select(CalibrationRun).where(
            CalibrationRun.id == run_id, CalibrationRun.project_id == project_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_runs(self, project_id: uuid.UUID, limit: int) -> list[CalibrationRun]:
        stmt = (
            select(CalibrationRun)
            .where(CalibrationRun.project_id == project_id)
            .order_by(CalibrationRun.created_at.desc(), CalibrationRun.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def transition(
        self,
        run_id: uuid.UUID,
        from_statuses: tuple[str, ...],
        to_status: str,
        *,
        reason: str | None = None,
        finished: bool = False,
    ) -> bool:
        """Change the status only if it is still one of `from_statuses` (a compare-and-set).
        Of several workers that try to finish a run at the same moment exactly one wins."""
        values: dict[str, Any] = {"status": to_status, "status_reason": reason}
        if finished:
            values["finished_at"] = func.now()
        stmt = (
            update(CalibrationRun)
            .where(CalibrationRun.id == run_id, CalibrationRun.status.in_(from_statuses))
            .values(**values)
            .returning(CalibrationRun.id)
        )
        return (await self.session.execute(stmt)).first() is not None

    # ---- samples ----
    async def traffic_pool(
        self,
        project_id: uuid.UUID,
        task_type: str,
        reference_model_id: uuid.UUID,
        *,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> list[PoolItem]:
        """Light description (no bodies) of the recorded requests a run may draw from: answered
        by the reference model, successful, with both bodies stored. Newest first."""
        sql = text(
            """
            SELECT request_id,
                   md5(coalesce((request_payload->'messages')::text, '')) AS dedup_key,
                   length(request_payload::text) AS chars,
                   coalesce(jsonb_typeof(request_payload->'tools') = 'array'
                            AND jsonb_array_length(request_payload->'tools') > 0,
                            false) AS has_tools,
                   EXISTS (SELECT 1 FROM feedback f
                           WHERE f.request_id = requests.request_id AND f.rating = -1) AS disliked
            FROM requests
            WHERE project_id = :project_id AND task_type = :task_type
              AND model_id = :reference AND status = 'ok'
              AND request_payload IS NOT NULL AND response_payload IS NOT NULL
              AND NOT payload_skipped
              AND (CAST(:since AS timestamptz) IS NULL OR started_at >= :since)
              AND (CAST(:until AS timestamptz) IS NULL OR started_at < :until)
            ORDER BY started_at DESC
            LIMIT :limit
            """
        )
        rows = await self.session.execute(
            sql,
            {
                "project_id": project_id,
                "task_type": task_type,
                "reference": reference_model_id,
                "since": since,
                "until": until,
                "limit": limit,
            },
        )
        return [
            PoolItem(
                r.request_id,
                "traffic",
                r.dedup_key,
                int(r.chars),
                bool(r.has_tools),
                disliked=bool(r.disliked),
            )
            for r in rows
        ]

    async def session_pool(
        self,
        project_id: uuid.UUID,
        task_type: str,
        reference_model_id: uuid.UUID,
        *,
        since: datetime | None,
        until: datetime | None,
        max_steps: int,
        limit: int,
    ) -> list[PoolItem]:
        """Conversations that can be replayed whole: every step recorded, answered by the
        reference, with bodies, steps 0..n-1 each exactly once. A derived session id shared by
        two unrelated conversations breaks that (two rows for one step) and is left out."""
        sql = text(
            """
            SELECT session_id, count(*) AS steps, max(length(request_payload::text)) AS chars,
                   bool_or(coalesce(jsonb_typeof(request_payload->'tools') = 'array'
                                    AND jsonb_array_length(request_payload->'tools') > 0,
                                    false)) AS has_tools,
                   (array_agg(md5(coalesce((request_payload->'messages')::text, ''))
                              ORDER BY step))[1] AS dedup_key
            FROM requests
            WHERE project_id = :project_id AND task_type = :task_type
              AND model_id = :reference AND status = 'ok' AND session_id IS NOT NULL
              AND request_payload IS NOT NULL AND response_payload IS NOT NULL
              AND NOT payload_skipped
              AND (CAST(:since AS timestamptz) IS NULL OR started_at >= :since)
              AND (CAST(:until AS timestamptz) IS NULL OR started_at < :until)
            GROUP BY session_id
            HAVING count(*) = count(DISTINCT step) AND max(step) = count(*) - 1
               AND count(*) <= :max_steps
            ORDER BY max(started_at) DESC
            LIMIT :limit
            """
        )
        rows = await self.session.execute(
            sql,
            {
                "project_id": project_id,
                "task_type": task_type,
                "reference": reference_model_id,
                "since": since,
                "until": until,
                "max_steps": max_steps,
                "limit": limit,
            },
        )
        return [
            PoolItem(
                uuid.uuid5(uuid.NAMESPACE_URL, f"session:{r.session_id}"),
                "traffic",
                r.dedup_key,
                int(r.chars),
                bool(r.has_tools),
                session_id=r.session_id,
            )
            for r in rows
        ]

    async def session_rows(
        self, project_id: uuid.UUID, session_ids: list[str], reference_model_id: uuid.UUID
    ) -> list[Any]:
        sql = text(
            """
            SELECT request_id, session_id, step, request_payload, response_payload,
                   prompt_tokens, completion_tokens
            FROM requests
            WHERE project_id = :project_id AND session_id = ANY(:ids) AND model_id = :reference
              AND status = 'ok' AND request_payload IS NOT NULL AND response_payload IS NOT NULL
              AND NOT payload_skipped
            ORDER BY session_id, step
            """
        )
        result = await self.session.execute(
            sql, {"project_id": project_id, "ids": session_ids, "reference": reference_model_id}
        )
        return list(result.all())

    async def labeled_pool(self, project_id: uuid.UUID, task_type: str) -> list[PoolItem]:
        sql = text(
            """
            SELECT id, md5(coalesce((request_payload->'messages')::text, '')) AS dedup_key,
                   length(request_payload::text) AS chars,
                   coalesce(jsonb_typeof(request_payload->'tools') = 'array'
                            AND jsonb_array_length(request_payload->'tools') > 0,
                            false) AS has_tools
            FROM labeled_samples
            WHERE project_id = :project_id AND task_type = :task_type
            """
        )
        rows = await self.session.execute(sql, {"project_id": project_id, "task_type": task_type})
        return [
            PoolItem(r.id, "labeled", r.dedup_key, int(r.chars), bool(r.has_tools)) for r in rows
        ]

    async def traffic_rows(self, project_id: uuid.UUID, ids: list[uuid.UUID]) -> list[Any]:
        sql = text(
            """
            SELECT request_id, request_payload, response_payload, prompt_tokens, completion_tokens
            FROM requests WHERE project_id = :project_id AND request_id = ANY(:ids)
            """
        )
        return list((await self.session.execute(sql, {"project_id": project_id, "ids": ids})).all())

    async def labeled_rows(
        self, project_id: uuid.UUID, ids: list[uuid.UUID]
    ) -> list[LabeledSample]:
        stmt = select(LabeledSample).where(
            LabeledSample.project_id == project_id, LabeledSample.id.in_(ids)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_sample(self, run_id: uuid.UUID, request_id: uuid.UUID) -> RunSample | None:
        stmt = select(RunSample).where(
            RunSample.run_id == run_id, RunSample.request_id == request_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---- labelled examples ----
    async def insert_labeled(self, rows: list[dict[str, Any]]) -> int:
        """Insert new examples; those already present (same request) are skipped."""
        if not rows:
            return 0
        stmt = (
            pg_insert(LabeledSample)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_labeled_samples_content")
            .returning(LabeledSample.id)
        )
        return len((await self.session.execute(stmt)).all())

    async def count_labeled(self, project_id: uuid.UUID, task_type: str) -> int:
        stmt = (
            select(func.count())
            .select_from(LabeledSample)
            .where(LabeledSample.project_id == project_id, LabeledSample.task_type == task_type)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    # ---- candidates and prices ----
    async def candidates(self, model_ids: list[uuid.UUID]) -> dict[uuid.UUID, Candidate]:
        stmt = (
            select(Model, Provider)
            .join(Provider, Provider.id == Model.provider_id)
            .where(Model.id.in_(model_ids))
        )
        found: dict[uuid.UUID, Candidate] = {}
        for model, provider in (await self.session.execute(stmt)).all():
            found[model.id] = Candidate(
                model_id=model.id,
                name=model.name,
                enabled=model.enabled,
                context_window=model.context_window,
                max_output_tokens=model.max_output_tokens,
                supports_stream=model.supports_stream,
                cost_type=model.cost_type,
                provider_id=provider.id,
                provider_enabled=provider.enabled,
                base_url=provider.base_url,
                is_local=provider.is_local,
                auth_type=provider.auth_type,
                api_key_encrypted=provider.api_key_encrypted,
                max_concurrency=provider.calibration_max_concurrency,
                max_rps=provider.calibration_max_rps,
            )
        return found

    async def current_price(self, model_id: uuid.UUID, at: datetime) -> Price | None:
        stmt = select(ModelPrice).where(
            ModelPrice.model_id == model_id,
            ModelPrice.valid_from <= at,
            or_(ModelPrice.valid_to.is_(None), ModelPrice.valid_to > at),
        )
        row = (await self.session.execute(stmt)).scalars().first()
        if row is None:
            return None
        if row.input_per_1m is not None:
            return Price("per_token", row.input_per_1m, row.output_per_1m)
        return Price("fixed_monthly")

    async def models_with_prices(self, model_ids: list[uuid.UUID], at: datetime) -> set[uuid.UUID]:
        stmt = select(ModelPrice.model_id).where(
            ModelPrice.model_id.in_(model_ids),
            ModelPrice.valid_from <= at,
            or_(ModelPrice.valid_to.is_(None), ModelPrice.valid_to > at),
        )
        return set((await self.session.execute(stmt)).scalars())

    # ---- results ----
    async def result_exists(
        self, run_id: uuid.UUID, request_id: uuid.UUID, model_id: uuid.UUID
    ) -> bool:
        stmt = select(RunResult.id).where(
            RunResult.run_id == run_id,
            RunResult.request_id == request_id,
            RunResult.model_id == model_id,
        )
        return (await self.session.execute(stmt)).first() is not None

    async def insert_result(self, values: dict[str, Any]) -> bool:
        """Store a result; False if the pair already has one (a repeated job)."""
        stmt = (
            pg_insert(RunResult)
            .values(id=uuid.uuid4(), **values)
            .on_conflict_do_nothing(constraint="uq_run_results_pair")
            .returning(RunResult.id)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def count_results(self, run_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(RunResult).where(RunResult.run_id == run_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def status_counts(self, run_id: uuid.UUID) -> dict[str, int]:
        stmt = (
            select(RunResult.status, func.count())
            .where(RunResult.run_id == run_id)
            .group_by(RunResult.status)
        )
        return {status: int(n) for status, n in (await self.session.execute(stmt)).all()}

    async def count_evaluations(self, run_id: uuid.UUID) -> int:
        row = await self.session.execute(
            text("SELECT count(*) FROM evaluations WHERE run_id = :r"), {"r": run_id}
        )
        return int(row.scalar_one())

    async def spending(self, run_id: uuid.UUID) -> tuple[Decimal, int]:
        """(total cost in USD, number of provider calls made) according to the stored results."""
        stmt = select(
            func.coalesce(func.sum(RunResult.cost_usd), 0),
            func.count().filter(RunResult.status.in_(CALL_STATUSES)),
        ).where(RunResult.run_id == run_id)
        spent, calls = (await self.session.execute(stmt)).one()
        return Decimal(spent or 0), int(calls)

    async def list_results(
        self,
        run_id: uuid.UUID,
        *,
        model_id: uuid.UUID | None,
        status: str | None,
        after: uuid.UUID | None,
        limit: int,
    ) -> list[RunResult]:
        stmt = select(RunResult).where(RunResult.run_id == run_id)
        if model_id is not None:
            stmt = stmt.where(RunResult.model_id == model_id)
        if status is not None:
            stmt = stmt.where(RunResult.status == status)
        if after is not None:
            stmt = stmt.where(and_(RunResult.id > after))
        stmt = stmt.order_by(RunResult.id).limit(limit)
        return list((await self.session.execute(stmt)).scalars())
