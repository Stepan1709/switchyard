"""Authentication dependencies: who is calling and with which role."""

import ipaddress
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.deps import RedisDep, SessionDep
from app.core.errors import Forbidden, Unauthorized
from app.core.idempotency import Idempotency
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
    """The caller's address. Behind a trusted proxy (TRUST_FORWARDED_FOR) it is the one the
    proxy reports; a value that is not an IP address is ignored."""
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.trust_forwarded_for:
        first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        try:
            return str(ipaddress.ip_address(first))
        except ValueError:
            pass
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


async def get_idempotency(
    request: Request,
    admin: AdminOnly,
    settings: SettingsDep,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Idempotency:
    """Idempotency helper for the current request (active only when the header is sent)."""
    return Idempotency(
        request.app.state.resources.session_factory,
        admin_id=admin.id,
        key=idempotency_key,
        method=request.method,
        path=request.url.path,
        body=await request.body(),
        ttl_hours=settings.idempotency_ttl_hours,
        lock_seconds=settings.idempotency_lock_seconds,
    )


IdempotencyDep = Annotated[Idempotency, Depends(get_idempotency)]
