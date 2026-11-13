"""Projects, membership and API keys.

Access rule (IDOR protection): every operation first resolves the project through the
caller's membership. A project the caller does not belong to is reported as "not found",
the same as a project that does not exist, so ids cannot be probed.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, NotFound
from app.modules.audit import service as audit
from app.modules.auth.models import Admin
from app.modules.auth.repository import AuthRepository
from app.modules.auth.service import normalize_email
from app.modules.projects import keys
from app.modules.projects.models import ApiKey, Project
from app.modules.projects.repository import ProjectRepository
from app.modules.projects.schemas import KeyCreate, KeyUpdate, ProjectCreate


class ProjectService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repo = ProjectRepository(session)
        self.admins = AuthRepository(session)

    async def _project(self, admin: Admin, project_id: uuid.UUID) -> Project:
        project = await self.repo.get_for_admin(project_id, admin.id)
        if project is None:
            raise NotFound("Project not found")
        return project

    # projects ---------------------------------------------------------------------------
    async def create_project(self, admin: Admin, data: ProjectCreate, ip: str | None) -> Project:
        project = Project(name=data.name, description=data.description, created_by_id=admin.id)
        self.repo.add_project(project)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise Conflict("A project with this name already exists") from exc
        self.repo.add_member(project.id, admin.id)  # the creator becomes the first member
        audit.record(
            self.session,
            action="project.create",
            actor_id=admin.id,
            target_type="project",
            target_id=project.id,
            ip=ip,
        )
        await self.session.commit()
        return project

    async def list_projects(self, admin: Admin) -> list[Project]:
        return await self.repo.list_for_admin(admin.id)

    async def get_project(self, admin: Admin, project_id: uuid.UUID) -> Project:
        return await self._project(admin, project_id)

    # members ----------------------------------------------------------------------------
    async def list_members(
        self, admin: Admin, project_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, str]]:
        await self._project(admin, project_id)
        return await self.repo.list_members(project_id)

    async def add_member(
        self, admin: Admin, project_id: uuid.UUID, email: str, ip: str | None
    ) -> tuple[uuid.UUID, str]:
        await self._project(admin, project_id)
        member = await self.admins.get_admin_by_email(normalize_email(email))
        if member is None:
            raise NotFound("Administrator not found")
        if await self.repo.is_member(project_id, member.id):
            raise Conflict("The administrator is already a member")
        self.repo.add_member(project_id, member.id)
        audit.record(
            self.session,
            action="project.member_add",
            actor_id=admin.id,
            target_type="project",
            target_id=project_id,
            details={"member_id": str(member.id)},
            ip=ip,
        )
        await self.session.commit()
        return member.id, member.email

    async def remove_member(
        self, admin: Admin, project_id: uuid.UUID, member_id: uuid.UUID, ip: str | None
    ) -> None:
        await self._project(admin, project_id)
        if await self.repo.count_members(project_id) <= 1:
            raise Conflict("A project must keep at least one member")
        if not await self.repo.remove_member(project_id, member_id):
            raise NotFound("Member not found")
        audit.record(
            self.session,
            action="project.member_remove",
            actor_id=admin.id,
            target_type="project",
            target_id=project_id,
            details={"member_id": str(member_id)},
            ip=ip,
        )
        await self.session.commit()

    # keys -------------------------------------------------------------------------------
    async def create_key(
        self, admin: Admin, project_id: uuid.UUID, data: KeyCreate, ip: str | None
    ) -> tuple[ApiKey, str]:
        await self._project(admin, project_id)
        raw, prefix, secret = keys.generate_key()
        key = ApiKey(
            project_id=project_id,
            name=data.name,
            prefix=prefix,
            secret_hash=keys.hash_secret(secret, self.settings.key_pepper.get_secret_value()),
            default_task_type=data.default_task_type,
            rate_limit_rpm=data.rate_limit_rpm,
            monthly_budget_usd=data.monthly_budget_usd,
            created_by_id=admin.id,
        )
        self.repo.add_key(key)
        await self.session.flush()
        # The audit entry holds the public prefix, never the secret part.
        audit.record(
            self.session,
            action="api_key.create",
            actor_id=admin.id,
            target_type="api_key",
            target_id=key.id,
            details={"project_id": str(project_id), "prefix": prefix},
            ip=ip,
        )
        await self.session.commit()
        # Reload so the response shows what is stored (e.g. numeric scale), as later reads do.
        await self.session.refresh(key)
        return key, raw

    async def list_keys(self, admin: Admin, project_id: uuid.UUID) -> list[ApiKey]:
        await self._project(admin, project_id)
        return await self.repo.list_keys(project_id)

    async def _key(self, admin: Admin, project_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey:
        await self._project(admin, project_id)
        key = await self.repo.get_key(project_id, key_id)
        if key is None:
            raise NotFound("API key not found")
        return key

    async def update_key(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        key_id: uuid.UUID,
        data: KeyUpdate,
        ip: str | None,
    ) -> ApiKey:
        key = await self._key(admin, project_id, key_id)
        changes = data.model_dump(exclude_unset=True)
        if "name" in changes and changes["name"] is None:
            changes.pop("name")  # a key always has a name
        for field, value in changes.items():
            setattr(key, field, value)
        audit.record(
            self.session,
            action="api_key.update",
            actor_id=admin.id,
            target_type="api_key",
            target_id=key.id,
            details={"fields": sorted(changes)},
            ip=ip,
        )
        await self.session.commit()
        await self.session.refresh(key)
        return key

    async def revoke_key(
        self, admin: Admin, project_id: uuid.UUID, key_id: uuid.UUID, ip: str | None
    ) -> ApiKey:
        key = await self._key(admin, project_id, key_id)
        if key.revoked_at is None:
            key.revoked_at = datetime.now(UTC)
            audit.record(
                self.session,
                action="api_key.revoke",
                actor_id=admin.id,
                target_type="api_key",
                target_id=key.id,
                details={"prefix": key.prefix},
                ip=ip,
            )
            await self.session.commit()
        return key

    async def authenticate_key(self, raw: str) -> ApiKey | None:
        """Resolve a presented client key to its record, or None if it is not valid.

        Used by the gateway snapshot builder later; here it defines the verification rules:
        well-formed, known prefix, matching secret hash, not revoked.
        """
        parsed = keys.parse_key(raw)
        if parsed is None:
            return None
        prefix, secret = parsed
        key = await self.repo.get_key_by_prefix(prefix)
        pepper = self.settings.key_pepper.get_secret_value()
        # For an unknown prefix a dummy hash is still compared, so timing does not reveal it.
        stored = key.secret_hash if key else "0" * 64
        matches = keys.verify_secret(secret, stored, pepper)
        if key is None or not matches or key.revoked_at is not None:
            return None
        return key
