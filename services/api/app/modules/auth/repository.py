"""SQL for administrators and refresh tokens. No business rules here."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import Admin, RefreshToken


class AuthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_admin_by_email(self, email: str) -> Admin | None:
        return (await self.session.execute(select(Admin).where(Admin.email == email))).scalar()

    async def get_admin(self, admin_id: uuid.UUID) -> Admin | None:
        return await self.session.get(Admin, admin_id)

    async def count_admins(self) -> int:
        return (await self.session.execute(select(func.count()).select_from(Admin))).scalar_one()

    def add_admin(self, admin: Admin) -> None:
        self.session.add(admin)

    def add_refresh_token(self, token: RefreshToken) -> None:
        self.session.add(token)

    async def get_refresh_for_update(self, token_hash: str) -> RefreshToken | None:
        # FOR UPDATE: two parallel refreshes of the same token are serialised, so exactly one
        # of them wins and the other is seen as a reuse.
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        return (await self.session.execute(stmt)).scalar()

    async def revoke_family(self, family_id: uuid.UUID) -> None:
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await self.session.execute(stmt)

    async def revoke_all_for_admin(self, admin_id: uuid.UUID) -> None:
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.admin_id == admin_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await self.session.execute(stmt)
