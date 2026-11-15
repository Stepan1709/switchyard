"""Admin REST of calibration runs and labelled examples."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.deps import SessionDep
from app.modules.auth.deps import AdminOnly, CurrentAdmin, IdempotencyDep, SettingsDep, client_ip
from app.modules.calibration.schemas import ImportReport, ResultPage, RunCreate, RunOut
from app.modules.calibration.service import CalibrationService, PayloadTooLarge

router = APIRouter(prefix="/projects/{project_id}", tags=["calibration"])


def get_service(session: SessionDep, settings: SettingsDep) -> CalibrationService:
    return CalibrationService(session, settings)


ServiceDep = Annotated[CalibrationService, Depends(get_service)]


@router.post("/calibration-runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def create_run(
    project_id: uuid.UUID,
    body: RunCreate,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
    idem: IdempotencyDep,
) -> Any:
    """Start a calibration. Send an `Idempotency-Key` so that a repeated request (after a
    timeout, say) does not start a second run."""

    async def call() -> RunOut:
        return await service.create_run(admin, project_id, body, client_ip(request))

    return await idem.run(call, status_code=status.HTTP_201_CREATED)


@router.get("/calibration-runs", response_model=list[RunOut])
async def list_runs(
    project_id: uuid.UUID,
    admin: CurrentAdmin,
    service: ServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[RunOut]:
    return await service.list_runs(admin, project_id, limit)


@router.get("/calibration-runs/{run_id}", response_model=RunOut)
async def get_run(
    project_id: uuid.UUID, run_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> RunOut:
    """The run with its progress (poll this)."""
    return await service.get_run(admin, project_id, run_id)


@router.post("/calibration-runs/{run_id}/pause", response_model=RunOut)
async def pause_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> RunOut:
    return await service.pause(admin, project_id, run_id, client_ip(request))


@router.post("/calibration-runs/{run_id}/resume", response_model=RunOut)
async def resume_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> RunOut:
    return await service.resume(admin, project_id, run_id, client_ip(request))


@router.post("/calibration-runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> RunOut:
    return await service.cancel(admin, project_id, run_id, client_ip(request))


@router.get("/calibration-runs/{run_id}/results", response_model=ResultPage)
async def list_results(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    admin: CurrentAdmin,
    service: ServiceDep,
    model_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ResultPage:
    return await service.list_results(
        admin, project_id, run_id, model_id=model_id, status=status_, cursor=cursor, limit=limit
    )


@router.post("/task-types/{name}/labeled-samples", response_model=ImportReport)
async def import_labeled_samples(
    project_id: uuid.UUID,
    name: str,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
    settings: SettingsDep,
) -> ImportReport:
    """Import labelled examples: a JSON Lines body, one object per line:
    {"messages": [...], "expected": "the right answer"}. `expected` may also be an object with
    "content" or "tool_calls"."""
    limit = settings.calibration_import_max_bytes
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():  # stop reading as soon as the limit is crossed
        size += len(chunk)
        if size > limit:
            raise PayloadTooLarge(f"The import must not exceed {limit} bytes")
        chunks.append(chunk)
    return await service.import_labeled(
        admin, project_id, name, b"".join(chunks), client_ip(request)
    )


@router.get("/task-types/{name}/labeled-samples")
async def count_labeled_samples(
    project_id: uuid.UUID, name: str, admin: CurrentAdmin, service: ServiceDep
) -> dict[str, int]:
    return {"count": await service.count_labeled(admin, project_id, name)}
