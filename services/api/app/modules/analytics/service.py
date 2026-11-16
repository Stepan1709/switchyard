"""Reports: usage, savings, servers, feedback, budgets.

Everything is read from the summaries (`usage_daily`, `utilization_daily`) and the small tables
next to them, never by scanning the traffic log, so a report is cheap however big the log is.
"""

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import Conflict, NotFound, Unprocessable
from app.modules.analytics import calc, usage
from app.modules.analytics.models import NIL_UUID
from app.modules.analytics.schemas import (
    AgreementOut,
    BudgetState,
    FeedbackGroup,
    FeedbackItem,
    FeedbackList,
    FeedbackReport,
    HumanVerdictOut,
    KeyBudget,
    ModelUsage,
    Overview,
    Problem,
    RuleHealth,
    ServersReport,
    ServerUsage,
    SourceBusy,
    TaskTypeUsage,
    Totals,
    UsageReport,
    UsageRow,
)
from app.modules.audit import service as audit
from app.modules.auth.models import Admin
from app.modules.calibration.repository import CalibrationRepository
from app.modules.evaluation.stats import wilson_interval
from app.modules.gateway.budget import counter_name, month_key
from app.modules.projects.repository import ProjectRepository

_ZERO = Decimal(0)
MAX_RECOMPUTE_DAYS = 92


def parse_month(value: str | None, now: datetime) -> tuple[str, date, date]:
    """(label, first day, first day of the next month) of a "YYYY-MM" (default: this month)."""
    try:
        first = (
            datetime.strptime(value, "%Y-%m").date().replace(day=1)
            if value
            else now.date().replace(day=1)
        )
    except ValueError as exc:
        raise Unprocessable("month must look like 2026-11") from exc
    following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first.strftime("%Y-%m"), first, following


def _money(value: Any) -> Decimal:
    return Decimal(value) if value is not None else _ZERO


def _pct(part: Decimal, whole: Decimal) -> float | None:
    return float(part / whole * 100) if whole > 0 else None


class AnalyticsService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        redis: Redis | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.redis = redis
        self.session_factory = session_factory
        self.projects = ProjectRepository(session)
        self.calibration = CalibrationRepository(session)

    async def _check_project(self, admin: Admin, project_id: uuid.UUID) -> None:
        if await self.projects.get_for_admin(project_id, admin.id) is None:
            raise NotFound("Project not found")  # not a member = not found

    async def _models(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
        return await self.calibration.candidates([i for i in set(ids) if i != NIL_UUID])

    async def _monthly_cost(self, model_id: uuid.UUID, at: datetime) -> Decimal | None:
        row = (
            await self.session.execute(
                text(
                    "SELECT monthly_cost_usd FROM model_prices WHERE model_id = :m"
                    " AND monthly_cost_usd IS NOT NULL AND valid_from <= :at"
                    " AND (valid_to IS NULL OR valid_to > :at) LIMIT 1"
                ),
                {"m": model_id, "at": at},
            )
        ).first()
        return None if row is None else Decimal(row.monthly_cost_usd)

    # ---- usage over time ----
    async def usage(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        since: date,
        until: date,
        group_by: str,
    ) -> UsageReport:
        await self._check_project(admin, project_id)
        if until < since:
            raise Unprocessable("'until' must not be before 'since'")
        if (until - since).days > 400:
            raise Unprocessable("At most 400 days per report")
        column = {"day": "day", "model": "model_id", "task_type": "task_type"}[group_by]
        rows = (
            await self.session.execute(
                text(
                    f"SELECT {column} AS k, sum(requests) AS requests, sum(ok) AS ok,"
                    " sum(errors) AS errors, sum(prompt_tokens) AS pt,"
                    " sum(completion_tokens) AS ct,"
                    " sum(cost_usd) AS cost, sum(reference_cost_usd) AS ref,"
                    " sum(duration_sum_ms) AS dur FROM usage_daily"
                    " WHERE project_id = :p AND day >= :a AND day <= :b GROUP BY 1 ORDER BY 1"
                ),
                {"p": project_id, "a": since, "b": until},
            )
        ).all()
        names: dict[uuid.UUID, Any] = {}
        if group_by == "model":
            names = await self._models([r.k for r in rows])
        out = []
        for r in rows:
            out.append(
                UsageRow(
                    day=r.k if group_by == "day" else None,
                    model_id=None if group_by != "model" or r.k == NIL_UUID else r.k,
                    model=(names[r.k].name if r.k in names else None)
                    if group_by == "model"
                    else None,
                    task_type=(r.k or None) if group_by == "task_type" else None,
                    requests=int(r.requests),
                    ok=int(r.ok),
                    errors=int(r.errors),
                    prompt_tokens=int(r.pt),
                    completion_tokens=int(r.ct),
                    cost_usd=_money(r.cost),
                    reference_cost_usd=_money(r.ref),
                    avg_latency_ms=float(r.dur) / int(r.requests) if int(r.requests) else None,
                )
            )
        return UsageReport(group_by=group_by, since=since, until=until, rows=out)  # type: ignore[arg-type]

    # ---- the month ----
    async def overview(self, admin: Admin, project_id: uuid.UUID, month: str | None) -> Overview:
        await self._check_project(admin, project_id)
        now = datetime.now(UTC)
        label, first, following = parse_month(month, now)
        rows = (
            await self.session.execute(
                text(
                    "SELECT model_id, task_type, sum(requests) AS requests, sum(ok) AS ok,"
                    " sum(errors) AS errors, sum(prompt_tokens) AS pt,"
                    " sum(completion_tokens) AS ct,"
                    " sum(cost_usd) AS cost, sum(reference_cost_usd) AS ref,"
                    " sum(duration_sum_ms) AS dur"
                    " FROM usage_daily WHERE project_id = :p AND day >= :a AND day < :b"
                    " GROUP BY model_id, task_type"
                ),
                {"p": project_id, "a": first, "b": following},
            )
        ).all()
        models = await self._models([r.model_id for r in rows])
        month_end = datetime.combine(following, datetime.min.time(), tzinfo=UTC)
        at = min(now, month_end - timedelta(seconds=1))

        # A server paid by the month costs the same however much it is used, so each project
        # carries the share of it that its requests make up in all requests the server handled.
        all_requests: dict[uuid.UUID, int] = {}
        monthly: dict[uuid.UUID, Decimal] = {}
        for model_id, model in models.items():
            if model.cost_type != "fixed_monthly":
                continue
            total = (
                await self.session.execute(
                    text(
                        "SELECT coalesce(sum(requests), 0) AS n FROM usage_daily"
                        " WHERE model_id = :m AND day >= :a AND day < :b"
                    ),
                    {"m": model_id, "a": first, "b": following},
                )
            ).scalar_one()
            all_requests[model_id] = int(total)
            price = await self._monthly_cost(model_id, at)
            if price is not None:
                monthly[model_id] = price

        def fixed_share(model_id: uuid.UUID, requests: int) -> Decimal:
            total = all_requests.get(model_id, 0)
            if model_id not in monthly or total <= 0:
                return _ZERO
            return monthly[model_id] * requests / total

        totals = Totals()
        by_model: dict[uuid.UUID, ModelUsage] = {}
        by_task: dict[str, TaskTypeUsage] = {}
        durations: dict[uuid.UUID, float] = defaultdict(float)
        for r in rows:
            requests = int(r.requests)
            fixed = fixed_share(r.model_id, requests)
            for target in (totals,):
                _add(target, r, fixed)
            model_row = by_model.get(r.model_id)
            if model_row is None:
                model = models.get(r.model_id)
                model_row = ModelUsage(
                    model_id=None if r.model_id == NIL_UUID else r.model_id,
                    name=model.name if model else "(no model)",
                    cost_type=model.cost_type if model else None,
                    share=0.0,
                    avg_latency_ms=None,
                    effective_cost_per_request_usd=None,
                )
                by_model[r.model_id] = model_row
            _add(model_row, r, fixed)
            durations[r.model_id] += float(r.dur or 0)
            task_row = by_task.setdefault(r.task_type, TaskTypeUsage(task_type=r.task_type))
            _add(task_row, r, fixed)

        _finish_totals(totals)
        for model_id, model_row in by_model.items():
            _finish_totals(model_row)
            model_row.share = model_row.requests / totals.requests if totals.requests else 0.0
            model_row.avg_latency_ms = (
                durations[model_id] / model_row.requests if model_row.requests else None
            )
            if model_row.requests:
                model_row.effective_cost_per_request_usd = (
                    model_row.effective_cost_usd / model_row.requests
                )
        for task_row in by_task.values():
            _finish_totals(task_row)

        keys = await self._key_budgets(project_id, label, first, following)
        problems = await self._problems(project_id, models, by_model, keys, label, now)
        return Overview(
            month=label,
            totals=totals,
            models=sorted(by_model.values(), key=lambda m: -m.requests),
            task_types=sorted(by_task.values(), key=lambda t: -t.requests),
            keys=keys,
            problems=problems,
        )

    async def _key_budgets(
        self, project_id: uuid.UUID, label: str, first: date, following: date
    ) -> list[KeyBudget]:
        keys = await self.projects.list_keys(project_id)
        spent = {
            str(r.key_id): _money(r.spent)
            for r in (
                await self.session.execute(
                    text(
                        "SELECT key_id, sum(cost_usd) AS spent FROM requests WHERE project_id = :p"
                        " AND key_id IS NOT NULL AND started_at >= :a AND started_at < :b"
                        " GROUP BY key_id"
                    ),
                    {
                        "p": project_id,
                        "a": datetime.combine(first, datetime.min.time(), tzinfo=UTC),
                        "b": datetime.combine(following, datetime.min.time(), tzinfo=UTC),
                    },
                )
            ).all()
        }
        result = []
        for key in keys:
            if key.revoked_at is not None:
                continue
            used = spent.get(str(key.id), _ZERO)
            counter = await self._counter(str(key.id), label)
            limit = key.monthly_budget_usd
            reference = max(used, counter or _ZERO)
            result.append(
                KeyBudget(
                    key_id=key.id,
                    name=key.name,
                    monthly_budget_usd=limit,
                    spent_usd=used,
                    counter_usd=counter,
                    remaining_usd=None if limit is None else max(limit - reference, _ZERO),
                    used_pct=None
                    if limit is None or limit == 0
                    else float(reference / limit * 100),
                    exhausted=limit is not None and reference >= limit,
                )
            )
        return result

    async def _counter(self, key_id: str, label: str) -> Decimal | None:
        if self.redis is None:
            return None
        try:
            raw = await self.redis.get(counter_name(key_id, label))
        except Exception:  # the report works without Redis
            return None
        return None if raw is None else Decimal(int(raw)) / Decimal(1_000_000)

    async def _problems(
        self,
        project_id: uuid.UUID,
        models: dict[uuid.UUID, Any],
        by_model: dict[uuid.UUID, ModelUsage],
        keys: list[KeyBudget],
        label: str,
        now: datetime,
    ) -> list[Problem]:
        problems: list[Problem] = []
        used = [m for m in by_model if m in models and models[m].cost_type == "fixed_monthly"]
        if used:
            report = await self.servers_report(label)
            for server in report.servers:
                if server.idle and server.model_id in used:
                    problems.append(
                        Problem(
                            kind="idle_server",
                            subject=server.name,
                            detail=f"busy {server.utilization_pct:.1f}% of the time "
                            f"(below {server.idle_threshold_pct:.0f}%)",
                        )
                    )
        tasks = (
            await self.session.execute(
                text(
                    "SELECT DISTINCT task_type FROM routing_rules WHERE project_id = :p"
                    " AND status = 'active'"
                ),
                {"p": project_id},
            )
        ).all()
        for t in tasks:
            health = await self._rule_health(project_id, t.task_type, now)
            if health is not None and health.suggest_rollback:
                problems.append(
                    Problem(kind="dislikes_rose", subject=t.task_type, detail=health.reason)
                )
        for key in keys:
            if key.exhausted:
                problems.append(
                    Problem(
                        kind="budget_exhausted",
                        subject=key.name,
                        detail="the monthly budget is used up",
                    )
                )
        return problems

    # ---- servers ----
    async def servers_report(self, month: str | None) -> ServersReport:
        now = datetime.now(UTC)
        label, first, following = parse_month(month, now)
        start = datetime.combine(first, datetime.min.time(), tzinfo=UTC)
        end = datetime.combine(following, datetime.min.time(), tzinfo=UTC)
        window = (min(now, end) - start).total_seconds()
        month_seconds = (end - start).total_seconds()
        servers = (
            await self.session.execute(
                text(
                    "SELECT id, name, idle_threshold_pct FROM models"
                    " WHERE cost_type = 'fixed_monthly' AND enabled ORDER BY name"
                )
            )
        ).all()
        busy = {
            (r.model_id, r.source): (float(r.busy), int(r.n))
            for r in (
                await self.session.execute(
                    text(
                        "SELECT model_id, source, sum(busy_seconds) AS busy, sum(requests) AS n"
                        " FROM utilization_daily WHERE day >= :a AND day < :b"
                        " GROUP BY model_id, source"
                    ),
                    {"a": first, "b": following},
                )
            ).all()
        }
        out = []
        for s in servers:
            all_busy, requests = busy.get((s.id, "all"), (0.0, 0))
            pct = calc.utilization_pct(all_busy, window)
            threshold = (
                float(s.idle_threshold_pct)
                if s.idle_threshold_pct is not None
                else self.settings.idle_default_threshold_pct
            )
            monthly = await self._monthly_cost(s.id, min(now, end - timedelta(seconds=1)))
            idle_cost = (
                None
                if monthly is None
                else Decimal(
                    str(round(calc.idle_cost(float(monthly), all_busy, window, month_seconds), 6))
                )
            )
            out.append(
                ServerUsage(
                    model_id=s.id,
                    name=s.name,
                    monthly_cost_usd=monthly,
                    window_seconds=window,
                    busy_seconds=all_busy,
                    utilization_pct=round(pct, 4),
                    busy_by_source_seconds=SourceBusy(
                        live=busy.get((s.id, "live"), (0.0, 0))[0],
                        calibration=busy.get((s.id, "calibration"), (0.0, 0))[0],
                        shadow=busy.get((s.id, "shadow"), (0.0, 0))[0],
                    ),
                    idle_threshold_pct=threshold,
                    idle=pct < threshold,
                    idle_cost_usd=idle_cost,
                    requests=requests,
                    effective_cost_per_request_usd=(
                        None if monthly is None or requests == 0 else monthly / requests
                    ),
                )
            )
        return ServersReport(month=label, servers=out)

    # ---- feedback ----
    async def feedback_report(
        self, admin: Admin, project_id: uuid.UUID, since: datetime | None, until: datetime | None
    ) -> FeedbackReport:
        await self._check_project(admin, project_id)
        until = until or datetime.now(UTC)
        since = since or until - timedelta(days=30)
        rows = (
            await self.session.execute(
                text(
                    "SELECT model_id, task_type, count(*) AS n,"
                    " count(*) FILTER (WHERE rating = -1) AS bad FROM feedback"
                    " WHERE project_id = :p AND request_started_at >= :a"
                    " AND request_started_at < :b"
                    " GROUP BY model_id, task_type ORDER BY n DESC"
                ),
                {"p": project_id, "a": since, "b": until},
            )
        ).all()
        names = await self._models([r.model_id for r in rows if r.model_id])
        groups = []
        for r in rows:
            low, high = wilson_interval(int(r.bad), int(r.n)) or (0.0, 0.0)
            groups.append(
                FeedbackGroup(
                    model_id=r.model_id,
                    model=names[r.model_id].name if r.model_id in names else None,
                    task_type=r.task_type,
                    ratings=int(r.n),
                    dislikes=int(r.bad),
                    dislike_rate=int(r.bad) / int(r.n),
                    dislike_rate_low=low,
                    dislike_rate_high=high,
                )
            )
        return FeedbackReport(
            since=since,
            until=until,
            ratings=sum(g.ratings for g in groups),
            dislikes=sum(g.dislikes for g in groups),
            groups=groups,
        )

    async def feedback_list(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        rating: int | None,
        before: datetime | None,
        limit: int,
    ) -> FeedbackList:
        await self._check_project(admin, project_id)
        rows = (
            await self.session.execute(
                text(
                    "SELECT f.request_id, f.rating, f.comment, f.updated_at, f.task_type,"
                    " m.name AS model"
                    " FROM feedback f LEFT JOIN models m ON m.id = f.model_id"
                    " WHERE f.project_id = :p AND (CAST(:r AS int) IS NULL OR f.rating = :r)"
                    " AND (CAST(:before AS timestamptz) IS NULL OR f.updated_at < :before)"
                    " ORDER BY f.updated_at DESC LIMIT :limit"
                ),
                {"p": project_id, "r": rating, "before": before, "limit": limit},
            )
        ).all()
        return FeedbackList(
            items=[
                FeedbackItem(
                    request_id=r.request_id,
                    model=r.model,
                    task_type=r.task_type,
                    rating=r.rating,
                    comment=r.comment,
                    updated_at=r.updated_at,
                )
                for r in rows
            ]
        )

    # ---- the signal on a rule ----
    async def rule_health(self, admin: Admin, project_id: uuid.UUID, task_type: str) -> RuleHealth:
        await self._check_project(admin, project_id)
        health = await self._rule_health(project_id, task_type, datetime.now(UTC))
        if health is None:
            raise Conflict("The task type has no active rule")
        return health

    async def _rule_health(
        self, project_id: uuid.UUID, task_type: str, now: datetime
    ) -> RuleHealth | None:
        rule = (
            await self.session.execute(
                text(
                    "SELECT version, chain, activated_at FROM routing_rules WHERE project_id = :p"
                    " AND task_type = :t AND status = 'active'"
                ),
                {"p": project_id, "t": task_type},
            )
        ).first()
        if rule is None or rule.activated_at is None:
            return None
        reference = (
            await self.session.execute(
                text("SELECT reference_model_id FROM projects WHERE id = :p"), {"p": project_id}
            )
        ).scalar_one()

        async def count(model_ids: list[uuid.UUID], a: datetime, b: datetime) -> tuple[int, int]:
            row = (
                await self.session.execute(
                    text(
                        "SELECT count(*) AS n, count(*) FILTER (WHERE rating = -1) AS bad"
                        " FROM feedback"
                        " WHERE project_id = :p AND task_type = :t AND model_id = ANY(:ids)"
                        " AND request_started_at >= :a AND request_started_at < :b"
                    ),
                    {"p": project_id, "t": task_type, "ids": model_ids, "a": a, "b": b},
                )
            ).one()
            return int(row.bad), int(row.n)

        # now: answers of the models of the rule since it went live; before: what people said
        # about the reference's answers in the 30 days preceding it
        bad_now, n_now = await count(
            list(rule.chain), rule.activated_at, now + timedelta(seconds=1)
        )
        bad_before, n_before = (
            await count([reference], rule.activated_at - timedelta(days=30), rule.activated_at)
            if reference
            else (0, 0)
        )
        signal = calc.rollback_signal(
            bad_now,
            n_now,
            bad_before,
            n_before,
            min_ratings=self.settings.feedback_min_for_signal,
            margin=self.settings.feedback_signal_margin,
        )
        return RuleHealth(
            task_type=task_type,
            rule_version=rule.version,
            activated_at=rule.activated_at,
            suggest_rollback=signal.suggest_rollback,
            reason=signal.reason,
            ratings_since=signal.ratings_now,
            dislikes_since=signal.dislikes_now,
            dislike_rate_since=signal.rate_now,
            dislike_rate_low=signal.rate_low,
            ratings_before=signal.ratings_before,
            dislikes_before=signal.dislikes_before,
            dislike_rate_before=signal.rate_before,
        )

    # ---- recomputing ----
    async def recompute(
        self, admin: Admin, project_id: uuid.UUID, since: date, until: date, ip: str | None
    ) -> int:
        await self._check_project(admin, project_id)
        if until < since:
            raise Unprocessable("'until' must not be before 'since'")
        days = (until - since).days + 1
        if days > MAX_RECOMPUTE_DAYS:
            raise Unprocessable(f"At most {MAX_RECOMPUTE_DAYS} days at a time")
        await usage.recompute_usage(self.session, since, until)
        await usage.recompute_utilization(self.session, since, until)
        audit.record(
            self.session,
            action="analytics.recompute",
            actor_id=admin.id,
            target_type="project",
            target_id=project_id,
            details={"since": since.isoformat(), "until": until.isoformat()},
            ip=ip,
        )
        await self.session.commit()
        return days

    # ---- budgets ----
    async def budget(self, admin: Admin, project_id: uuid.UUID, key_id: uuid.UUID) -> BudgetState:
        await self._check_project(admin, project_id)
        key = await self.projects.get_key(project_id, key_id)
        if key is None:
            raise NotFound("Key not found")
        label = month_key()
        _, first, following = parse_month(label, datetime.now(UTC))
        for item in await self._key_budgets(project_id, label, first, following):
            if item.key_id == key_id:
                return BudgetState(
                    key_id=key_id,
                    month=label,
                    monthly_budget_usd=item.monthly_budget_usd,
                    spent_usd=item.spent_usd,
                    counter_usd=item.counter_usd,
                    remaining_usd=item.remaining_usd,
                    exhausted=item.exhausted,
                )
        raise NotFound("Key not found")  # a revoked key has no budget to show

    # ---- people and the judge ----
    async def save_human_verdict(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
        result_id: uuid.UUID,
        verdict: str,
        comment: str | None,
        ip: str | None,
    ) -> HumanVerdictOut:
        await self._check_project(admin, project_id)
        run = await self.calibration.get_run_of_project(project_id, run_id)
        if run is None:
            raise NotFound("Calibration run not found")
        found = (
            await self.session.execute(
                text("SELECT 1 FROM run_results WHERE id = :r AND run_id = :run"),
                {"r": result_id, "run": run_id},
            )
        ).first()
        if found is None:
            raise NotFound("Result not found in this run")
        await self.session.execute(
            text(
                "INSERT INTO human_verdicts (id, result_id, admin_id, verdict, comment)"
                " VALUES (gen_random_uuid(), :r, :a, :v, :c)"
                " ON CONFLICT (result_id, admin_id) DO UPDATE"
                " SET verdict = EXCLUDED.verdict, comment = EXCLUDED.comment, created_at = now()"
            ),
            {"r": result_id, "a": admin.id, "v": verdict, "c": comment},
        )
        audit.record(
            self.session,
            action="human_verdict.save",
            actor_id=admin.id,
            target_type="run_result",
            target_id=result_id,
            details={"run_id": str(run_id), "verdict": verdict},
            ip=ip,
        )
        recomputed = False
        if run.evaluation_status == "done":
            # a person outranks the judge: the numbers of the run are made again with this verdict
            from app.modules.evaluation.evaluator import compute_aggregates
            from app.modules.evaluation.repository import EvaluationRepository

            await compute_aggregates(
                self.session,
                EvaluationRepository(self.session),
                run_id,
                run.task_type,
                self.settings.calibration_min_samples,
            )
            recomputed = True
        await self.session.commit()
        return HumanVerdictOut(
            result_id=result_id, verdict=verdict, comment=comment, aggregates_recomputed=recomputed
        )

    async def judge_agreement(
        self, admin: Admin, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> AgreementOut:
        await self._check_project(admin, project_id)
        if await self.calibration.get_run_of_project(project_id, run_id) is None:
            raise NotFound("Calibration run not found")
        rows = (
            await self.session.execute(
                text(
                    "SELECT e.judge_verdict, e.judge_mode, h.verdict FROM evaluations e"
                    " JOIN LATERAL (SELECT verdict FROM human_verdicts v"
                    "   WHERE v.result_id = e.result_id"
                    "   ORDER BY v.created_at DESC LIMIT 1) h ON true"
                    " WHERE e.run_id = :run AND e.judge_calls > 0 AND e.judge_verdict IS NOT NULL"
                ),
                {"run": run_id},
            )
        ).all()
        pairs = [(_judge_passed(r.judge_mode, r.judge_verdict), r.verdict == "pass") for r in rows]
        a = calc.agreement(pairs)
        note = (
            "Compared on answers that a person rated explicitly and the judge also judged. "
            "Likes and dislikes of live traffic are not used: they rate the answer the user saw, "
            "not the candidate's."
        )
        return AgreementOut(
            run_id=run_id,
            compared=a.n,
            agreed=a.agreed,
            agreement_rate=a.rate,
            cohen_kappa=a.kappa,
            judge_pass_human_pass=a.judge_pass_human_pass,
            judge_pass_human_fail=a.judge_pass_human_fail,
            judge_fail_human_pass=a.judge_fail_human_pass,
            judge_fail_human_fail=a.judge_fail_human_fail,
            note=note,
        )


def _add(target: Totals, r: Any, fixed: Decimal) -> None:
    target.requests += int(r.requests)
    target.ok += int(r.ok)
    target.errors += int(r.errors)
    target.prompt_tokens += int(r.pt)
    target.completion_tokens += int(r.ct)
    target.cost_usd += _money(r.cost)
    target.fixed_cost_usd += fixed
    target.reference_cost_usd += _money(r.ref)


def _finish_totals(t: Totals) -> None:
    t.effective_cost_usd = t.cost_usd + t.fixed_cost_usd
    t.savings_usd = t.reference_cost_usd - t.effective_cost_usd
    t.savings_pct = _pct(t.savings_usd, t.reference_cost_usd)


def _judge_passed(mode: str | None, verdict: str) -> bool:
    """The judge's own verdict: `pass` when grading; in a pairwise comparison the candidate has
    to have won (or tied) in both orders."""
    return verdict == "pass" if mode == "grade" else verdict == "candidate|candidate"
