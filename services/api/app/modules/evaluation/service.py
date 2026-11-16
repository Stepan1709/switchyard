"""Admin use cases of evaluation: the checks of a task type and reading the verdicts."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import NotFound, Unprocessable
from app.modules.audit import service as audit
from app.modules.auth.models import Admin
from app.modules.calibration.repository import CalibrationRepository
from app.modules.evaluation.checks import validate_check_config
from app.modules.evaluation.models import TaskCheck
from app.modules.evaluation.repository import EvaluationRepository
from app.modules.evaluation.schemas import (
    AggregateOut,
    AggregatesOut,
    CheckCreate,
    EvaluationOut,
    EvaluationPage,
)
from app.modules.projects.repository import ProjectRepository
from app.modules.traffic.models import TaskType

MAX_CHECKS_PER_TASK = 20


class EvaluationService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repo = EvaluationRepository(session)
        self.runs = CalibrationRepository(session)
        self.projects = ProjectRepository(session)

    async def _check_project(self, admin: Admin, project_id: uuid.UUID) -> None:
        if await self.projects.get_for_admin(project_id, admin.id) is None:
            raise NotFound("Project not found")  # not a member = not found

    async def _check_task_type(self, project_id: uuid.UUID, name: str) -> None:
        found = (
            await self.session.execute(
                select(TaskType.id).where(TaskType.project_id == project_id, TaskType.name == name)
            )
        ).first()
        if found is None:
            raise NotFound("Task type not found")

    # ---- checks of a task type ----
    async def list_checks(self, admin: Admin, project_id: uuid.UUID, name: str) -> list[TaskCheck]:
        await self._check_project(admin, project_id)
        await self._check_task_type(project_id, name)
        return await self.repo.checks_for(project_id, name)

    async def create_check(
        self, admin: Admin, project_id: uuid.UUID, name: str, data: CheckCreate, ip: str | None
    ) -> TaskCheck:
        await self._check_project(admin, project_id)
        await self._check_task_type(project_id, name)
        try:
            config = validate_check_config(data.kind, data.config)
        except ValueError as exc:
            raise Unprocessable(str(exc)) from exc
        existing = await self.repo.checks_for(project_id, name)
        if len(existing) >= MAX_CHECKS_PER_TASK:
            raise Unprocessable(f"At most {MAX_CHECKS_PER_TASK} checks per task type")
        if data.kind == "llm_judge" and any(c.kind == "llm_judge" for c in existing):
            raise Unprocessable("A task type has at most one llm_judge check")
        check = TaskCheck(
            id=uuid.uuid4(),
            project_id=project_id,
            task_type=name,
            kind=data.kind,
            config=config,
            required=data.required,
            position=data.position,
        )
        self.session.add(check)
        audit.record(
            self.session,
            action="task_check.create",
            actor_id=admin.id,
            target_type="project",
            target_id=project_id,
            details={"task_type": name, "kind": data.kind, "required": data.required},
            ip=ip,
        )
        await self.session.commit()
        await self.session.refresh(check)
        return check

    async def delete_check(
        self, admin: Admin, project_id: uuid.UUID, check_id: uuid.UUID, ip: str | None
    ) -> None:
        await self._check_project(admin, project_id)
        check = await self.repo.get_check(project_id, check_id)
        if check is None:
            raise NotFound("Check not found")
        await self.session.delete(check)
        audit.record(
            self.session,
            action="task_check.delete",
            actor_id=admin.id,
            target_type="project",
            target_id=project_id,
            details={"task_type": check.task_type, "kind": check.kind},
            ip=ip,
        )
        await self.session.commit()

    # ---- results of an evaluation ----
    async def _run(self, admin: Admin, project_id: uuid.UUID, run_id: uuid.UUID):  # type: ignore[no-untyped-def]
        await self._check_project(admin, project_id)
        run = await self.runs.get_run_of_project(project_id, run_id)
        if run is None:
            raise NotFound("Calibration run not found")
        return run

    async def aggregates(
        self, admin: Admin, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> AggregatesOut:
        run = await self._run(admin, project_id, run_id)
        rows = await self.repo.list_aggregates(run_id)
        return AggregatesOut(
            run_id=run.id,
            evaluation_status=run.evaluation_status,
            min_samples=self.settings.calibration_min_samples,
            items=[AggregateOut.model_validate(r) for r in rows],
        )

    async def evaluations(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        model_id: uuid.UUID | None,
        status: str | None,
        passed: bool | None,
        cursor: str | None,
        limit: int,
    ) -> EvaluationPage:
        await self._run(admin, project_id, run_id)
        after: uuid.UUID | None = None
        if cursor is not None:
            try:
                after = uuid.UUID(cursor)
            except ValueError as exc:
                raise Unprocessable("The cursor is not valid") from exc
        rows = await self.repo.list_evaluations(
            run_id, model_id=model_id, status=status, passed=passed, after=after, limit=limit + 1
        )
        page = rows[:limit]
        items = []
        for evaluation, model, request in page:
            item = EvaluationOut.model_validate(evaluation)
            item.model_id, item.request_id = model, request
            items.append(item)
        return EvaluationPage(
            items=items, next_cursor=str(page[-1][0].id) if len(rows) > limit else None
        )
