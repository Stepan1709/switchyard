from fastapi import APIRouter, Request, Response, status

from app.modules.auth.deps import AdminOnly, AuthServiceDep, CurrentAdmin, client_ip
from app.modules.auth.schemas import (
    AdminCreate,
    AdminOut,
    LoginRequest,
    RefreshRequest,
    TokenPair,
)

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=TokenPair)
async def login(body: LoginRequest, request: Request, service: AuthServiceDep) -> TokenPair:
    return await service.login(body.email, body.password, client_ip(request))


@router.post("/auth/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, request: Request, service: AuthServiceDep) -> TokenPair:
    return await service.refresh(body.refresh_token, client_ip(request))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, service: AuthServiceDep) -> Response:
    await service.logout(body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=AdminOut)
async def me(admin: CurrentAdmin) -> AdminOut:
    return AdminOut.model_validate(admin)


@router.post("/admins", response_model=AdminOut, status_code=status.HTTP_201_CREATED)
async def create_admin(
    body: AdminCreate, request: Request, actor: AdminOnly, service: AuthServiceDep
) -> AdminOut:
    admin = await service.create_admin(body, actor_id=actor.id, ip=client_ip(request))
    return AdminOut.model_validate(admin)
