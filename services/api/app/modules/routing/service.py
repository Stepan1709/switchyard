"""Rules of routing: recommendations, versions, activation and rollback.

Only a person changes what is live. A recommendation can PROPOSE a rule version; a person
reads it and activates it. Activation checks that nothing changed since they looked (optimistic
locking on the active version) and the database allows exactly one active version per task
type (a partial unique index), so two people clicking at once cannot both win.

Every change is written to the audit log, and `rules.changed` goes to the outbox in the same
transaction: the gateways hear about a change if and only if it was really made.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.events import RULES_CHANGED
from app.core.outbox import enqueue
from app.modules.audit import service as audit
from app.modules.auth.models import Admin
from app.modules.calibration.models import CalibrationRun
from app.modules.calibration.repository import CalibrationRepository
from app.modules.evaluation.repository import EvaluationRepository
from app.modules.projects.repository import ProjectRepository
from app.modules.routing import recommend as rec
from app.modules.routing.models import MAX_CHAIN, RoutingRule
from app.modules.routing.schemas import CriteriaOut, RecommendationOut, RuleOut, VerdictOut
from app.modules.traffic.models import TaskType


def criteria_for(run: CalibrationRun, settings: Settings) -> rec.Criteria:
    """The thresholds of the run, with the settings' defaults for what it left unset."""
    t = run.thresholds or {}
    return rec.Criteria(
        min_pass_rate=float(t.get("min_pass_rate", settings.recommend_min_pass_rate)),
        min_quality=t.get("min_quality"),
        max_p95_ms=t.get("max_p95_ms"),
        max_error_rate=float(t.get("max_error_rate", settings.recommend_max_error_rate)),
    )


class RoutingService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.projects = ProjectRepository(session)
        self.calibration = CalibrationRepository(session)
        self.evaluation = EvaluationRepository(session)

    # ---- lookups ----
    async def _check_project(self, admin: Admin, project_id: uuid.UUID) -> None:
        if await self.projects.get_for_admin(project_id, admin.id) is None:
            raise NotFound("Project not found")  # not a member = not found

    async def _task_type(self, project_id: uuid.UUID, name: str, *, lock: bool = False) -> TaskType:
        stmt = select(TaskType).where(TaskType.project_id == project_id, TaskType.name == name)
        if lock:
            # One lock per task type serialises activations, rollbacks, new versions and mode
            # changes of it: they read the active version and then change it.
            stmt = stmt.with_for_update()
        task = (await self.session.execute(stmt)).scalars().first()
        if task is None:
            raise NotFound("Task type not found")
        return task

    async def _rule(self, project_id: uuid.UUID, rule_id: uuid.UUID) -> RoutingRule:
        stmt = select(RoutingRule).where(
            RoutingRule.project_id == project_id, RoutingRule.id == rule_id
        )
        rule = (await self.session.execute(stmt)).scalars().first()
        if rule is None:
            raise NotFound("Rule not found")
        return rule

    async def _active(self, project_id: uuid.UUID, task_type: str) -> RoutingRule | None:
        stmt = select(RoutingRule).where(
            RoutingRule.project_id == project_id,
            RoutingRule.task_type == task_type,
            RoutingRule.status == "active",
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def _names(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        found = await self.calibration.candidates(list(set(ids)))
        return {i: c.name for i, c in found.items()}

    async def _out(self, rule: RoutingRule) -> RuleOut:
        names = await self._names(rule.chain)
        out = RuleOut.model_validate(rule)
        out.chain_names = [names.get(i, "(deleted)") for i in rule.chain]
        return out

    async def _check_chain(self, chain: list[uuid.UUID]) -> None:
        if len(set(chain)) != len(chain):
            raise Unprocessable("A model can appear in the chain only once")
        if not 1 <= len(chain) <= MAX_CHAIN:
            raise Unprocessable(f"The chain must have 1 to {MAX_CHAIN} models")
        found = await self.calibration.candidates(chain)
        for model_id in chain:
            model = found.get(model_id)
            if model is None:
                raise Unprocessable(f"Unknown model {model_id}")
            if not (model.enabled and model.provider_enabled):
                raise Unprocessable(f"The model {model.name} is disabled")

    def _announce(self, project_id: uuid.UUID, task_type: str, reason: str) -> None:
        enqueue(
            self.session,
            RULES_CHANGED,
            {"project_id": str(project_id), "task_type": task_type, "reason": reason},
        )

    # ---- reading ----
    async def list_rules(self, admin: Admin, project_id: uuid.UUID, name: str) -> list[RuleOut]:
        await self._check_project(admin, project_id)
        await self._task_type(project_id, name)
        stmt = (
            select(RoutingRule)
            .where(RoutingRule.project_id == project_id, RoutingRule.task_type == name)
            .order_by(RoutingRule.version.desc())
        )
        return [await self._out(r) for r in (await self.session.execute(stmt)).scalars()]

    async def get_rule(self, admin: Admin, project_id: uuid.UUID, rule_id: uuid.UUID) -> RuleOut:
        await self._check_project(admin, project_id)
        return await self._out(await self._rule(project_id, rule_id))

    # ---- recommendation ----
    async def recommendation(
        self, admin: Admin, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> RecommendationOut:
        await self._check_project(admin, project_id)
        run = await self.calibration.get_run_of_project(project_id, run_id)
        if run is None:
            raise NotFound("Calibration run not found")
        if run.evaluation_status != "done":
            raise Conflict("The run is not evaluated yet: wait until its evaluation is done")
        criteria = criteria_for(run, self.settings)
        rows = await self.evaluation.list_aggregates(run_id)
        models = await self.calibration.candidates([r.model_id for r in rows])
        stats = [
            rec.CandidateStats(
                model_id=str(r.model_id),
                name=models[r.model_id].name,
                cost_type=models[r.model_id].cost_type,
                cost_per_request_usd=r.cost_per_request_usd,
                n_evaluated=r.n_evaluated,
                n_context_exceeded=r.n_context_exceeded,
                insufficient_data=r.insufficient_data,
                pass_rate=r.pass_rate,
                quality_mean=r.quality_mean,
                latency_p95_ms=r.latency_p95_ms,
                error_rate=r.error_rate,
            )
            for r in rows
            if r.model_id in models
        ]
        result = rec.recommend(
            stats, str(run.reference_model_id) if run.reference_model_id else None, criteria
        )
        by_id = {s.model_id: s for s in stats}
        aggregates = {str(r.model_id): r for r in rows}
        verdicts = []
        for v in result.verdicts:
            row = aggregates[v.model_id]
            s = by_id[v.model_id]
            verdicts.append(
                VerdictOut(
                    model_id=uuid.UUID(v.model_id),
                    name=v.name,
                    eligible=v.eligible,
                    reasons=v.reasons,
                    fixed_cost=v.fixed_cost,
                    cost_per_request_usd=v.cost_per_request_usd,
                    pass_rate=s.pass_rate,
                    pass_rate_low=row.pass_rate_low,
                    pass_rate_high=row.pass_rate_high,
                    quality_mean=s.quality_mean,
                    latency_p95_ms=s.latency_p95_ms,
                    error_rate=s.error_rate,
                    n_evaluated=s.n_evaluated,
                    insufficient_data=s.insufficient_data,
                )
            )
        chain_ids = [uuid.UUID(i) for i in result.chain]
        names = await self._names(chain_ids)
        return RecommendationOut(
            run_id=run.id,
            task_type=run.task_type,
            criteria=CriteriaOut(
                min_pass_rate=criteria.min_pass_rate,
                min_quality=criteria.min_quality,
                max_p95_ms=criteria.max_p95_ms,
                max_error_rate=criteria.max_error_rate,
            ),
            primary=uuid.UUID(result.primary) if result.primary else None,
            chain=chain_ids,
            chain_names=[names.get(i, "?") for i in chain_ids],
            verdicts=verdicts,
            notes=result.notes,
        )

    async def propose_from_run(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
        note: str | None,
        ip: str | None,
    ) -> RuleOut:
        recommendation = await self.recommendation(admin, project_id, run_id)
        if recommendation.primary is None:
            raise Unprocessable(
                "No candidate passed the thresholds, so there is nothing to propose"
            )
        basis: dict[str, Any] = recommendation.model_dump(mode="json", exclude={"run_id"})
        return await self._create(
            admin, project_id, recommendation.task_type, recommendation.chain, note, ip,
            source_run_id=run_id, basis=basis,
        )  # fmt: skip

    # ---- versions ----
    async def create_rule(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        name: str,
        chain: list[uuid.UUID],
        note: str | None,
        ip: str | None,
    ) -> RuleOut:
        await self._check_project(admin, project_id)
        return await self._create(admin, project_id, name, chain, note, ip)

    async def _create(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        name: str,
        chain: list[uuid.UUID],
        note: str | None,
        ip: str | None,
        *,
        source_run_id: uuid.UUID | None = None,
        basis: dict[str, Any] | None = None,
    ) -> RuleOut:
        await self._task_type(project_id, name, lock=True)
        await self._check_chain(chain)
        latest = (
            await self.session.execute(
                select(func.coalesce(func.max(RoutingRule.version), 0)).where(
                    RoutingRule.project_id == project_id, RoutingRule.task_type == name
                )
            )
        ).scalar_one()
        rule = RoutingRule(
            id=uuid.uuid4(),
            project_id=project_id,
            task_type=name,
            version=int(latest) + 1,
            status="proposed",
            chain=chain,
            source_run_id=source_run_id,
            basis=basis or {},
            note=note,
            created_by_id=admin.id,
        )
        self.session.add(rule)
        audit.record(
            self.session,
            action="routing_rule.propose",
            actor_id=admin.id,
            target_type="routing_rule",
            target_id=rule.id,
            details={"project_id": str(project_id), "task_type": name, "version": rule.version,
                     "chain": [str(c) for c in chain]},
            ip=ip,
        )  # fmt: skip
        await self.session.commit()
        return await self._out(rule)

    async def reject(
        self, admin: Admin, project_id: uuid.UUID, rule_id: uuid.UUID, ip: str | None
    ) -> RuleOut:
        await self._check_project(admin, project_id)
        rule = await self._rule(project_id, rule_id)
        await self._task_type(project_id, rule.task_type, lock=True)
        await self.session.refresh(rule)
        if rule.status != "proposed":
            raise Conflict(f"Only a proposed rule can be rejected (this one is {rule.status})")
        rule.status = "rejected"
        rule.retired_at = datetime.now(UTC)
        audit.record(
            self.session, action="routing_rule.reject", actor_id=admin.id,
            target_type="routing_rule", target_id=rule.id, ip=ip,
        )  # fmt: skip
        await self.session.commit()
        return await self._out(rule)

    async def activate(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        rule_id: uuid.UUID,
        expected_active_version: int | None,
        ip: str | None,
    ) -> RuleOut:
        await self._check_project(admin, project_id)
        rule = await self._rule(project_id, rule_id)
        await self._task_type(project_id, rule.task_type, lock=True)
        await self.session.refresh(rule)  # its status may have changed while we waited
        current = await self._active(project_id, rule.task_type)
        current_version = current.version if current else None
        if current_version != expected_active_version:
            raise Conflict(
                f"The active version is {current_version}, not {expected_active_version}: "
                "somebody changed the rules since you looked. Review and decide again."
            )
        if rule.status not in ("proposed", "superseded"):
            raise Conflict(f"A rule that is {rule.status} cannot be activated")
        await self._check_chain(rule.chain)

        now = datetime.now(UTC)
        if current is not None:
            current.status = "superseded"
            current.retired_at = now
            # The old version must stop being active BEFORE the new one starts: the unique
            # index allows one active row at any moment, and one flush does not order them.
            await self.session.flush()
        rule.status = "active"
        rule.activated_by_id = admin.id
        rule.activated_at = now
        rule.retired_at = None
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise Conflict("Another version was activated at the same moment") from exc
        self._announce(project_id, rule.task_type, f"activated:{rule.version}")
        audit.record(
            self.session,
            action="routing_rule.activate",
            actor_id=admin.id,
            target_type="routing_rule",
            target_id=rule.id,
            details={"task_type": rule.task_type, "version": rule.version,
                     "replaced": current_version},
            ip=ip,
        )  # fmt: skip
        await self.session.commit()
        return await self._out(rule)

    async def rollback(
        self, admin: Admin, project_id: uuid.UUID, name: str, ip: str | None
    ) -> RuleOut:
        """Undo the active version in one step: the previous one comes back. With none to come
        back, the task type returns to `record`, so it never routes by a rule nobody approved."""
        await self._check_project(admin, project_id)
        task = await self._task_type(project_id, name, lock=True)
        active = await self._active(project_id, name)
        if active is None:
            raise Conflict("There is no active rule to roll back")
        previous = (
            (
                await self.session.execute(
                    select(RoutingRule)
                    .where(
                        RoutingRule.project_id == project_id,
                        RoutingRule.task_type == name,
                        RoutingRule.status == "superseded",
                    )
                    # the one the active version replaced: the most recently retired. (Not the
                    # next lower number: a person may have re-activated an old version.)
                    .order_by(RoutingRule.retired_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        now = datetime.now(UTC)
        active.status = "rolled_back"
        active.retired_at = now
        await self.session.flush()
        restored = previous
        if previous is not None:
            previous.status = "active"
            previous.activated_by_id = admin.id
            previous.activated_at = now
            previous.retired_at = None
            await self.session.flush()
        elif task.mode == "route":
            task.mode = "record"
        self._announce(project_id, name, f"rolled_back:{active.version}")
        audit.record(
            self.session,
            action="routing_rule.rollback",
            actor_id=admin.id,
            target_type="routing_rule",
            target_id=active.id,
            details={"task_type": name, "from_version": active.version,
                     "to_version": previous.version if previous else None},
            ip=ip,
        )  # fmt: skip
        await self.session.commit()
        return await self._out(restored or active)
