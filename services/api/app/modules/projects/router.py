"""Admin REST for projects and API keys.

Reads are open to any member (admin or viewer); changes require the admin role. Which
projects a caller can see at all is decided by membership (see the service).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.deps import SessionDep
from app.modules.auth.deps import AdminOnly, CurrentAdmin, IdempotencyDep, SettingsDep, client_ip
from app.modules.projects.schemas import (
    KeyCreate,
    KeyCreated,
    KeyOut,
    KeyUpdate,
    MemberAdd,
    MemberOut,
    ProjectCreate,
    ProjectOut,
    ReferenceModelSet,
)
from app.modules.projects.service import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


def get_project_service(session: SessionDep, settings: SettingsDep) -> ProjectService:
    return ProjectService(session, settings)


ServiceDep = Annotated[ProjectService, Depends(get_project_service)]


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
    idem: IdempotencyDep,
) -> Any:
    async def call() -> ProjectOut:
        return ProjectOut.model_validate(
            await service.create_project(admin, body, client_ip(request))
        )

    return await idem.run(call, status_code=status.HTTP_201_CREATED)


@router.get("", response_model=list[ProjectOut])
async def list_projects(admin: CurrentAdmin, service: ServiceDep) -> list[ProjectOut]:
    return [ProjectOut.model_validate(p) for p in await service.list_projects(admin)]


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> ProjectOut:
    return ProjectOut.model_validate(await service.get_project(admin, project_id))


@router.put("/{project_id}/reference-model", response_model=ProjectOut)
async def set_reference_model(
    project_id: uuid.UUID,
    body: ReferenceModelSet,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> ProjectOut:
    project = await service.set_reference_model(
        admin, project_id, body.model_id, client_ip(request)
    )
    return ProjectOut.model_validate(project)


@router.get("/{project_id}/members", response_model=list[MemberOut])
async def list_members(
    project_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> list[MemberOut]:
    members = await service.list_members(admin, project_id)
    return [MemberOut(admin_id=i, email=e) for i, e in members]


@router.post("/{project_id}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(
    project_id: uuid.UUID,
    body: MemberAdd,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> MemberOut:
    member_id, email = await service.add_member(admin, project_id, body.email, client_ip(request))
    return MemberOut(admin_id=member_id, email=email)


@router.delete("/{project_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> Response:
    await service.remove_member(admin, project_id, member_id, client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{project_id}/keys", response_model=KeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(
    project_id: uuid.UUID,
    body: KeyCreate,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> KeyCreated:
    key, raw = await service.create_key(admin, project_id, body, client_ip(request))
    return KeyCreated(**KeyOut.model_validate(key).model_dump(), key=raw)


@router.get("/{project_id}/keys", response_model=list[KeyOut])
async def list_keys(
    project_id: uuid.UUID, admin: CurrentAdmin, service: ServiceDep
) -> list[KeyOut]:
    return [KeyOut.model_validate(k) for k in await service.list_keys(admin, project_id)]


@router.patch("/{project_id}/keys/{key_id}", response_model=KeyOut)
async def update_key(
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    body: KeyUpdate,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> KeyOut:
    key = await service.update_key(admin, project_id, key_id, body, client_ip(request))
    return KeyOut.model_validate(key)


@router.post("/{project_id}/keys/{key_id}/revoke", response_model=KeyOut)
async def revoke_key(
    project_id: uuid.UUID,
    key_id: uuid.UUID,
    request: Request,
    admin: AdminOnly,
    service: ServiceDep,
) -> KeyOut:
    key = await service.revoke_key(admin, project_id, key_id, client_ip(request))
    return KeyOut.model_validate(key)
