"""Admin REST for the traffic log and task types of a project."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.deps import SessionDep
from app.modules.auth.deps import AdminOnly, CurrentAdmin, SettingsDep, client_ip
from app.modules.traffic.schemas import (
    RequestDetail,
    RequestPage,
    TaskTypeCreate,
    TaskTypeOut,
    TaskTypeUpdate,
)
from app.modules.traffic.service import TrafficService

router = APIRouter(prefix="/projects/{project_id}", tags=["traffic"])


def get_service(session: SessionDep, settings: SettingsDep) -> TrafficService:
    return TrafficService(session, settings)


ServiceDep = Annotated[TrafficService, Depends(get_service)]


@router.get("/requests", response_model=RequestPage)
async def list_requests(
    project_id: uuid.UUID,
    admin: CurrentAdmin,
    service: ServiceDep,
    task_type: str | None = None,
    model: str | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> RequestPage:
    """Newest first. Page through with `next_cursor`; add `since`/`until` to bound the scan."""
    return await service.list_requests(
        admin,
        project_id,
        task_type=task_type,
        model=model,
        status=status_,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
    )


@router.get("/requests/{request_id}", response_model=RequestDetail)
async def get_request(
    project_id: uuid.UUID, request_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> RequestDetail:
    return RequestDetail.model_validate(await service.get_request(admin, project_id, request_id))


@router.get("/task-types", response_model=list[TaskTypeOut])
async def list_task_types(
    project_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> list[TaskTypeOut]:
    return [TaskTypeOut.model_validate(t) for t in await service.list_task_types(admin, project_id)]


@router.post("/task-types", response_model=TaskTypeOut, status_code=status.HTTP_201_CREATED)
async def create_task_type(
    project_id: uuid.UUID,
    body: TaskTypeCreate,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> TaskTypeOut:
    created = await service.create_task_type(admin, project_id, body.name, client_ip(request))
    return TaskTypeOut.model_validate(created)


@router.patch("/task-types/{name}", response_model=TaskTypeOut)
async def update_task_type(
    project_id: uuid.UUID,
    name: str,
    body: TaskTypeUpdate,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> TaskTypeOut:
    updated = await service.update_task_type(admin, project_id, name, body, client_ip(request))
    return TaskTypeOut.model_validate(updated)
