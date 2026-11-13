"""Authentication dependencies: who is calling and with which role."""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.deps import RedisDep, SessionDep
from app.core.errors import Forbidden, Unauthorized
from app.modules.auth import security
from app.modules.auth.models import Admin
from app.modules.auth.rate_limit import LoginLimiter
from app.modules.auth.repository import AuthRepository
from app.modules.auth.service import AuthService

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings)]


def client_ip(request: Request) -> str:
    # Behind nginx this will be taken from a trusted forwarded header (added with nginx).
    return request.client.host if request.client else "unknown"


def get_auth_service(session: SessionDep, redis: RedisDep, settings: SettingsDep) -> AuthService:
    limiter = LoginLimiter(
        redis,
        max_per_email=settings.login_max_failures_per_email,
        max_per_ip=settings.login_max_failures_per_ip,
        window_seconds=settings.login_window_seconds,
    )
    return AuthService(session, settings, limiter)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_current_admin(
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Admin:
    if credentials is None:
        raise Unauthorized("Authentication required")
    claims = security.decode_access_token(
        credentials.credentials, settings.jwt_secret.get_secret_value()
    )
    admin = await AuthRepository(session).get_admin(claims.admin_id)
    # The role and the active flag come from the database, not from the token: demoting or
    # disabling an administrator takes effect immediately instead of when the token expires.
    if admin is None or not admin.is_active:
        raise Unauthorized("Account is disabled")
    return admin


CurrentAdmin = Annotated[Admin, Depends(get_current_admin)]


async def require_admin(admin: CurrentAdmin) -> Admin:
    if admin.role != "admin":
        raise Forbidden("This action requires the admin role")
    return admin


AdminOnly = Annotated[Admin, Depends(require_admin)]
