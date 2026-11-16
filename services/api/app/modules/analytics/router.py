"""Admin REST of analytics: usage, savings, servers, feedback, budgets, judge vs people."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request

from app.core.deps import SessionDep
from app.modules.analytics import budgets
from app.modules.analytics.schemas import (
    AgreementOut,
    BudgetState,
    FeedbackList,
    FeedbackReport,
    HumanVerdictIn,
    HumanVerdictOut,
    Overview,
    RecomputeRequest,
    RuleHealth,
    ServersReport,
    UsageReport,
)
from app.modules.analytics.service import AnalyticsService
from app.modules.auth.deps import AdminOnly, CurrentAdmin, SettingsDep, client_ip

router = APIRouter(tags=["analytics"])


def get_service(request: Request, session: SessionDep, settings: SettingsDep) -> AnalyticsService:
    resources = request.app.state.resources
    return AnalyticsService(session, settings, resources.redis, resources.session_factory)


ServiceDep = Annotated[AnalyticsService, Depends(get_service)]


@router.get("/projects/{project_id}/analytics/overview", response_model=Overview)
async def overview(
    project_id: uuid.UUID,
    admin: CurrentAdmin,
    service: ServiceDep,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
) -> Overview:
    """The month in numbers: requests by model and task type, what they cost, what the reference
    would have cost, the savings, the budgets of the keys and what needs attention."""
    return await service.overview(admin, project_id, month)


@router.get("/projects/{project_id}/analytics/usage", response_model=UsageReport)
async def usage_report(
    project_id: uuid.UUID,
    admin: CurrentAdmin,
    service: ServiceDep,
    since: date | None = None,
    until: date | None = None,
    group_by: Literal["day", "model", "task_type"] = "day",
) -> UsageReport:
    """A series for charts: by day, by model or by task type (default: the last 30 days)."""
    last = until or datetime.now(UTC).date()
    return await service.usage(
        admin, project_id, since or last - timedelta(days=29), last, group_by
    )


@router.post("/projects/{project_id}/analytics/recompute")
async def recompute(
    project_id: uuid.UUID,
    body: RecomputeRequest,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> dict[str, int]:
    """Rebuild the summaries of some days from the traffic log (the worker does it by itself
    every few minutes for the latest days; this is for a day that needs fixing)."""
    days = await service.recompute(admin, project_id, body.since, body.until, client_ip(request))
    return {"days": days}


@router.get("/projects/{project_id}/analytics/feedback", response_model=FeedbackReport)
async def feedback_report(
    project_id: uuid.UUID,
    admin: CurrentAdmin,
    service: ServiceDep,
    since: datetime | None = None,
    until: datetime | None = None,
) -> FeedbackReport:
    """The share of dislikes by model and task type, with its confidence interval."""
    return await service.feedback_report(admin, project_id, since, until)


@router.get("/projects/{project_id}/feedback", response_model=FeedbackList)
async def feedback_list(
    project_id: uuid.UUID,
    admin: CurrentAdmin,
    service: ServiceDep,
    rating: Annotated[int | None, Query(ge=-1, le=1)] = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> FeedbackList:
    """The ratings and comments, newest first."""
    return await service.feedback_list(admin, project_id, rating or None, before, limit)


@router.get("/projects/{project_id}/task-types/{name}/rule-health", response_model=RuleHealth)
async def rule_health(
    project_id: uuid.UUID, name: str, admin: CurrentAdmin, service: ServiceDep
) -> RuleHealth:
    """Do people like the answers less since the active rule went live? A signal for a person
    to look at rolling it back: nothing is rolled back automatically."""
    return await service.rule_health(admin, project_id, name)


@router.get("/projects/{project_id}/keys/{key_id}/budget", response_model=BudgetState)
async def key_budget(
    project_id: uuid.UUID, key_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> BudgetState:
    return await service.budget(admin, project_id, key_id)


@router.post("/projects/{project_id}/keys/{key_id}/budget/recount", response_model=BudgetState)
async def recount_budget(
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> BudgetState:
    """Set the gateway's counter of the key to exactly what the traffic log says (it can also
    go down, which the automatic reconciliation never does)."""
    await service.budget(admin, project_id, key_id)  # existence and access
    resources = request.app.state.resources
    await budgets.recount(resources.session_factory, resources.redis, str(key_id))
    return await service.budget(admin, project_id, key_id)


@router.get("/analytics/servers", response_model=ServersReport)
async def servers(
    _: CurrentAdmin,
    service: ServiceDep,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
) -> ServersReport:
    """The load of the servers paid by the month: how long each was busy (the union of its
    requests, so parallel ones are not added up), idle ones marked, and what idleness cost."""
    return await service.servers_report(month)


@router.put(
    "/projects/{project_id}/calibration-runs/{run_id}/results/{result_id}/human-verdict",
    response_model=HumanVerdictOut,
)
async def human_verdict(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    result_id: uuid.UUID,
    body: HumanVerdictIn,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> HumanVerdictOut:
    """A person's verdict on one answer of a run. It outranks the judge: the numbers of the run
    are made again with it."""
    return await service.save_human_verdict(
        admin, project_id, run_id, result_id, body.verdict, body.comment, client_ip(request)
    )


@router.get(
    "/projects/{project_id}/calibration-runs/{run_id}/judge-agreement", response_model=AgreementOut
)
async def judge_agreement(
    project_id: uuid.UUID, run_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> AgreementOut:
    """How often the judge and the people who checked the same answers agree (and Cohen's kappa)."""
    return await service.judge_agreement(admin, project_id, run_id)
