"""Admin REST of evaluation: checks per task type, aggregates and verdicts of a run."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.deps import SessionDep
from app.modules.auth.deps import AdminOnly, CurrentAdmin, SettingsDep, client_ip
from app.modules.evaluation.schemas import (
    AggregatesOut,
    CheckCreate,
    CheckOut,
    EvaluationPage,
)
from app.modules.evaluation.service import EvaluationService

router = APIRouter(prefix="/projects/{project_id}", tags=["evaluation"])


def get_service(session: SessionDep, settings: SettingsDep) -> EvaluationService:
    return EvaluationService(session, settings)


ServiceDep = Annotated[EvaluationService, Depends(get_service)]


@router.get("/task-types/{name}/checks", response_model=list[CheckOut])
async def list_checks(
    project_id: uuid.UUID, name: str, admin: CurrentAdmin, service: ServiceDep
) -> list[CheckOut]:
    return [CheckOut.model_validate(c) for c in await service.list_checks(admin, project_id, name)]


@router.post(
    "/task-types/{name}/checks", response_model=CheckOut, status_code=status.HTTP_201_CREATED
)
async def create_check(
    project_id: uuid.UUID,
    name: str,
    body: CheckCreate,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> CheckOut:
    check = await service.create_check(admin, project_id, name, body, client_ip(request))
    return CheckOut.model_validate(check)


@router.delete("/checks/{check_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_check(
    project_id: uuid.UUID,
    check_id: uuid.UUID,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> Response:
    await service.delete_check(admin, project_id, check_id, client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/calibration-runs/{run_id}/aggregates", response_model=AggregatesOut)
async def run_aggregates(
    project_id: uuid.UUID, run_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> AggregatesOut:
    """Per model: quality, pass rate with its interval, tokens, latency, errors, cost.
    Empty until the evaluation of the run is done (see `evaluation_status`)."""
    return await service.aggregates(admin, project_id, run_id)


@router.get("/calibration-runs/{run_id}/evaluations", response_model=EvaluationPage)
async def run_evaluations(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    admin: CurrentAdmin,
    service: ServiceDep,
    model_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
    passed: bool | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> EvaluationPage:
    """The verdict on each answer, with the judge's reasoning (to see why a model scored low)."""
    return await service.evaluations(
        admin, project_id, run_id, model_id=model_id, status=status_, passed=passed,
        cursor=cursor, limit=limit,
    )  # fmt: skip
