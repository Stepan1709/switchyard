"""SQL for projects, memberships and API keys."""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import Admin
from app.modules.projects.models import ApiKey, Project, ProjectMember


class ProjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # projects ---------------------------------------------------------------------------
    def add_project(self, project: Project) -> None:
        self.session.add(project)

    def add_member(self, project_id: uuid.UUID, admin_id: uuid.UUID) -> None:
        self.session.add(ProjectMember(project_id=project_id, admin_id=admin_id))

    async def list_for_admin(self, admin_id: uuid.UUID) -> list[Project]:
        stmt = (
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.admin_id == admin_id)
            .order_by(Project.created_at, Project.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_for_admin(self, project_id: uuid.UUID, admin_id: uuid.UUID) -> Project | None:
        # The membership join is the access check: no row means "not found" for this admin.
        stmt = (
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(Project.id == project_id, ProjectMember.admin_id == admin_id)
        )
        return (await self.session.execute(stmt)).scalar()

    async def is_member(self, project_id: uuid.UUID, admin_id: uuid.UUID) -> bool:
        stmt = select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.admin_id == admin_id
        )
        return (await self.session.execute(stmt)).scalar() is not None

    async def count_members(self, project_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(ProjectMember)
            .where(ProjectMember.project_id == project_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_members(self, project_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
        stmt = (
            select(Admin.id, Admin.email)
            .join(ProjectMember, ProjectMember.admin_id == Admin.id)
            .where(ProjectMember.project_id == project_id)
            .order_by(Admin.email)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def remove_member(self, project_id: uuid.UUID, admin_id: uuid.UUID) -> bool:
        stmt = delete(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.admin_id == admin_id
        )
        return (await self.session.execute(stmt)).rowcount > 0  # type: ignore[attr-defined,no-any-return]

    # keys -------------------------------------------------------------------------------
    def add_key(self, key: ApiKey) -> None:
        self.session.add(key)

    async def list_keys(self, project_id: uuid.UUID) -> list[ApiKey]:
        stmt = select(ApiKey).where(ApiKey.project_id == project_id).order_by(ApiKey.created_at)
        return list((await self.session.execute(stmt)).scalars())

    async def get_key(self, project_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey | None:
        stmt = select(ApiKey).where(ApiKey.project_id == project_id, ApiKey.id == key_id)
        return (await self.session.execute(stmt)).scalar()

    async def get_key_by_prefix(self, prefix: str) -> ApiKey | None:
        return (await self.session.execute(select(ApiKey).where(ApiKey.prefix == prefix))).scalar()
