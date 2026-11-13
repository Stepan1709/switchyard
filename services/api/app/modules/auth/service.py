"""Authentication rules: login with throttling, rotating refresh tokens, admin creation."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, Conflict, TooManyRequests, Unauthorized
from app.core.logging import get_logger
from app.modules.audit import service as audit
from app.modules.auth import security
from app.modules.auth.models import Admin, RefreshToken
from app.modules.auth.rate_limit import LoginLimiter
from app.modules.auth.repository import AuthRepository
from app.modules.auth.schemas import AdminCreate, TokenPair

log = get_logger(__name__)


class WeakPassword(AppError):
    status_code = 422
    title = "Weak password"
    code = "weak_password"


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AuthService:
    def __init__(
        self, session: AsyncSession, settings: Settings, limiter: LoginLimiter | None = None
    ) -> None:
        self.session = session
        self.settings = settings
        self.limiter = limiter
        self.repo = AuthRepository(session)

    # ---- tokens -------------------------------------------------------------------------
    def _new_refresh(self, admin_id: uuid.UUID, family_id: uuid.UUID) -> tuple[str, RefreshToken]:
        raw = security.new_refresh_token()
        row = RefreshToken(
            admin_id=admin_id,
            family_id=family_id,
            token_hash=security.hash_refresh_token(raw),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=self.settings.refresh_token_ttl_seconds),
        )
        self.repo.add_refresh_token(row)
        return raw, row

    def _pair(self, admin: Admin, family_id: uuid.UUID) -> TokenPair:
        refresh, _ = self._new_refresh(admin.id, family_id)
        access = security.issue_access_token(
            admin.id,
            admin.role,
            self.settings.jwt_secret.get_secret_value(),
            self.settings.access_token_ttl_seconds,
        )
        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            expires_in=self.settings.access_token_ttl_seconds,
        )

    # ---- login --------------------------------------------------------------------------
    async def login(self, email: str, password: str, ip: str) -> TokenPair:
        email = normalize_email(email)
        if self.limiter is not None:
            wait = await self.limiter.retry_after(email, ip)
            if wait:
                audit.record(
                    self.session, action="auth.login_throttled", ip=ip, details={"email": email}
                )
                await self.session.commit()
                raise TooManyRequests(wait, "Too many failed login attempts")

        admin = await self.repo.get_admin_by_email(email)
        ok = await security.verify_password(admin.password_hash if admin else None, password)
        if not ok or admin is None or not admin.is_active:
            if self.limiter is not None:
                await self.limiter.register_failure(email, ip)
            audit.record(self.session, action="auth.login_failed", ip=ip, details={"email": email})
            await self.session.commit()
            # One message for every failure reason: no hint whether the email exists.
            raise Unauthorized("Invalid email or password")

        if self.limiter is not None:
            await self.limiter.reset(email)
        if security.needs_rehash(admin.password_hash):
            admin.password_hash = await security.hash_password(password)
        pair = self._pair(admin, uuid.uuid4())
        audit.record(self.session, action="auth.login", actor_id=admin.id, ip=ip)
        await self.session.commit()
        return pair

    async def refresh(self, raw_token: str, ip: str | None = None) -> TokenPair:
        row = await self.repo.get_refresh_for_update(security.hash_refresh_token(raw_token))
        now = datetime.now(UTC)
        if row is None:
            raise Unauthorized("Invalid refresh token")
        if row.used_at is not None:
            # A rotated token came back: either a replay or a stolen copy. Kill the whole chain.
            await self.repo.revoke_family(row.family_id)
            audit.record(
                self.session,
                action="auth.refresh_reuse_detected",
                actor_id=row.admin_id,
                ip=ip,
                details={"family_id": str(row.family_id)},
            )
            await self.session.commit()
            raise Unauthorized("Refresh token was already used")
        if row.revoked_at is not None or row.expires_at <= now:
            raise Unauthorized("Refresh token is expired or revoked")

        admin = await self.repo.get_admin(row.admin_id)
        if admin is None or not admin.is_active:
            raise Unauthorized("Account is disabled")
        row.used_at = now
        pair = self._pair(admin, row.family_id)
        await self.session.commit()
        return pair

    async def logout(self, raw_token: str) -> None:
        row = await self.repo.get_refresh_for_update(security.hash_refresh_token(raw_token))
        if row is not None:
            await self.repo.revoke_family(row.family_id)
            audit.record(self.session, action="auth.logout", actor_id=row.admin_id)
            await self.session.commit()

    # ---- administrators -------------------------------------------------------------------
    def _check_password(self, password: str) -> None:
        if len(password) < self.settings.password_min_length:
            raise WeakPassword(
                f"Password must be at least {self.settings.password_min_length} characters"
            )

    async def create_admin(
        self, data: AdminCreate, actor_id: uuid.UUID | None = None, ip: str | None = None
    ) -> Admin:
        self._check_password(data.password)
        admin = Admin(
            email=normalize_email(data.email),
            password_hash=await security.hash_password(data.password),
            role=data.role,
        )
        self.repo.add_admin(admin)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise Conflict("An administrator with this email already exists") from exc
        audit.record(
            self.session,
            action="admin.create",
            actor_id=actor_id,
            target_type="admin",
            target_id=admin.id,
            details={"role": admin.role},
            ip=ip,
        )
        await self.session.commit()
        return admin

    async def ensure_first_admin(self, email: str, password: str) -> bool:
        """Create the first administrator unless one exists. Returns True if it was created."""
        if await self.repo.count_admins() > 0:
            return False
        try:
            await self.create_admin(AdminCreate(email=email, password=password, role="admin"))
        except Conflict:
            return False  # another replica created it first
        log.info("first_admin_created")
        return True
