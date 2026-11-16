"""Admin REST of routing rules: recommendation, versions, activation, rollback."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import SessionDep
from app.modules.auth.deps import AdminOnly, CurrentAdmin, IdempotencyDep, SettingsDep, client_ip
from app.modules.routing.schemas import (
    ActivateRequest,
    ProposeRequest,
    RecommendationOut,
    RuleCreate,
    RuleOut,
)
from app.modules.routing.service import RoutingService

router = APIRouter(prefix="/projects/{project_id}", tags=["routing"])


def get_service(session: SessionDep, settings: SettingsDep) -> RoutingService:
    return RoutingService(session, settings)


ServiceDep = Annotated[RoutingService, Depends(get_service)]


@router.get("/calibration-runs/{run_id}/recommendation", response_model=RecommendationOut)
async def recommendation(
    project_id: uuid.UUID, run_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> RecommendationOut:
    """The cheapest model that passed the thresholds, the fallbacks, and for every candidate
    the reasons it was accepted or not. Needs a run whose evaluation is done."""
    return await service.recommendation(admin, project_id, run_id)


@router.post(
    "/calibration-runs/{run_id}/recommendation/propose",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
)
async def propose(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    body: ProposeRequest,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> RuleOut:
    """Write the recommendation down as a PROPOSED rule version. It changes nothing live."""
    return await service.propose_from_run(admin, project_id, run_id, body.note, client_ip(request))


@router.get("/task-types/{name}/rules", response_model=list[RuleOut])
async def list_rules(
    project_id: uuid.UUID, name: str, admin: CurrentAdmin, service: ServiceDep
) -> list[RuleOut]:
    """The history of the rules of a task type, newest version first."""
    return await service.list_rules(admin, project_id, name)


@router.post(
    "/task-types/{name}/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED
)
async def create_rule(
    project_id: uuid.UUID,
    name: str,
    body: RuleCreate,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> RuleOut:
    """A rule written by hand (or a proposal with the chain edited). Still only PROPOSED."""
    return await service.create_rule(
        admin, project_id, name, body.chain, body.note, client_ip(request)
    )


@router.post("/task-types/{name}/rules/rollback", response_model=RuleOut)
async def rollback(
    project_id: uuid.UUID,
    name: str,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
    idem: IdempotencyDep,
) -> Any:
    """Undo the active version in one step: the previous version becomes active again."""

    async def call() -> RuleOut:
        return await service.rollback(admin, project_id, name, client_ip(request))

    return await idem.run(call, status_code=status.HTTP_200_OK)


@router.get("/rules/{rule_id}", response_model=RuleOut)
async def get_rule(
    project_id: uuid.UUID, rule_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> RuleOut:
    return await service.get_rule(admin, project_id, rule_id)


@router.post("/rules/{rule_id}/activate", response_model=RuleOut)
async def activate(
    project_id: uuid.UUID,
    rule_id: uuid.UUID,
    body: ActivateRequest,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
    idem: IdempotencyDep,
) -> Any:
    """Make a version active. `expected_active_version` is the version you saw as active; if
    it is no longer the active one the answer is 409 and nothing changes."""

    async def call() -> RuleOut:
        return await service.activate(
            admin, project_id, rule_id, body.expected_active_version, client_ip(request)
        )

    return await idem.run(call, status_code=status.HTTP_200_OK)


@router.post("/rules/{rule_id}/reject", response_model=RuleOut)
async def reject(
    project_id: uuid.UUID,
    rule_id: uuid.UUID,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> RuleOut:
    return await service.reject(admin, project_id, rule_id, client_ip(request))
