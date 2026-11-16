"""Reading the traffic log and managing task types. Access follows project membership."""

import base64
import json
import uuid
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, Conflict, NotFound, Unprocessable
from app.core.events import RULES_CHANGED
from app.core.outbox import enqueue
from app.modules.audit import service as audit
from app.modules.auth.models import Admin
from app.modules.calibration.repository import CalibrationRepository
from app.modules.projects.repository import ProjectRepository
from app.modules.routing.models import RoutingRule
from app.modules.traffic.models import TaskType, TrafficRequest
from app.modules.traffic.schemas import RequestPage, RequestSummary, TaskTypeUpdate

# Columns of the list view: the bodies (the heavy jsonb columns) are not even read from disk.
_SUMMARY_COLUMNS = [
    TrafficRequest.request_id,
    TrafficRequest.started_at,
    TrafficRequest.key_id,
    TrafficRequest.task_type,
    TrafficRequest.mode,
    TrafficRequest.stream,
    TrafficRequest.status,
    TrafficRequest.http_status,
    TrafficRequest.error_code,
    TrafficRequest.requested_model,
    TrafficRequest.model,
    TrafficRequest.provider,
    TrafficRequest.prompt_tokens,
    TrafficRequest.completion_tokens,
    TrafficRequest.ttft_ms,
    TrafficRequest.duration_ms,
    TrafficRequest.overhead_ms,
    TrafficRequest.payload_skipped,
    TrafficRequest.payload_masked,
]


class InvalidCursor(AppError):
    status_code = 400
    title = "Invalid cursor"
    code = "invalid_cursor"


def encode_cursor(started_at: datetime, request_id: uuid.UUID) -> str:
    raw = json.dumps({"t": started_at.isoformat(), "i": str(request_id)}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(data["t"]), uuid.UUID(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidCursor("The cursor is not valid") from exc


class TrafficService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.projects = ProjectRepository(session)

    async def _check_project(self, admin: Admin, project_id: uuid.UUID) -> None:
        # not a member = "not found": the project must not be probeable by id
        if await self.projects.get_for_admin(project_id, admin.id) is None:
            raise NotFound("Project not found")

    # ---- the log ----
    async def list_requests(
        self,
        admin: Admin,
        project_id: uuid.UUID,
        *,
        task_type: str | None,
        model: str | None,
        status: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> RequestPage:
        await self._check_project(admin, project_id)
        stmt = select(*_SUMMARY_COLUMNS).where(TrafficRequest.project_id == project_id)
        if task_type:
            stmt = stmt.where(TrafficRequest.task_type == task_type)
        if model:
            stmt = stmt.where(TrafficRequest.model == model)
        if status:
            stmt = stmt.where(TrafficRequest.status == status)
        if since:  # also lets PostgreSQL skip whole partitions outside the range
            stmt = stmt.where(TrafficRequest.started_at >= since)
        if until:
            stmt = stmt.where(TrafficRequest.started_at < until)
        if cursor:
            # Keyset pagination: continue strictly after the last row of the previous page.
            # (started_at, request_id) is unique, so no row is skipped or repeated, and the cost
            # does not grow with the page number the way OFFSET does.
            last_time, last_id = decode_cursor(cursor)
            stmt = stmt.where(
                tuple_(TrafficRequest.started_at, TrafficRequest.request_id)
                < tuple_(last_time, last_id)
            )
        stmt = stmt.order_by(TrafficRequest.started_at.desc(), TrafficRequest.request_id.desc())
        rows = (
            await self.session.execute(stmt.limit(limit + 1))
        ).all()  # one extra: is there more?
        page, has_more = rows[:limit], len(rows) > limit
        items = [RequestSummary.model_validate(r, from_attributes=True) for r in page]
        next_cursor = (
            encode_cursor(items[-1].started_at, items[-1].request_id)
            if has_more and items
            else None
        )
        return RequestPage(items=items, next_cursor=next_cursor)

    async def get_request(
        self, admin: Admin, project_id: uuid.UUID, request_id: uuid.UUID
    ) -> TrafficRequest:
        await self._check_project(admin, project_id)
        stmt = select(TrafficRequest).where(
            TrafficRequest.project_id == project_id, TrafficRequest.request_id == request_id
        )
        row = (await self.session.execute(stmt)).scalars().first()
        if row is None:
            raise NotFound("Request not found")
        return row

    # ---- task types ----
    async def list_task_types(self, admin: Admin, project_id: uuid.UUID) -> list[TaskType]:
        await self._check_project(admin, project_id)
        stmt = select(TaskType).where(TaskType.project_id == project_id).order_by(TaskType.name)
        return list((await self.session.execute(stmt)).scalars())

    async def create_task_type(
        self, admin: Admin, project_id: uuid.UUID, name: str, ip: str | None
    ) -> TaskType:
        await self._check_project(admin, project_id)
        task_type = TaskType(project_id=project_id, name=name, auto_registered=False)
        self.session.add(task_type)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise Conflict("This task type already exists") from exc
        audit.record(
            self.session,
            action="task_type.create",
            actor_id=admin.id,
            target_type="project",
            target_id=project_id,
            details={"name": name},
            ip=ip,
        )
        await self.session.commit()
        return task_type

    async def update_task_type(
        self, admin: Admin, project_id: uuid.UUID, name: str, data: TaskTypeUpdate, ip: str | None
    ) -> TaskType:
        """Switch the mode of a task type: record -> shadow -> route (and back).

        record  everything goes to the reference model and is logged
        shadow  the same, and a share of the requests is also sent to the listed models
        route   the active rule decides; it needs one (a person activates it first)
        """
        await self._check_project(admin, project_id)
        # FOR UPDATE: the same lock the rule activation takes, so a mode change and an
        # activation of the same task type never interleave.
        stmt = (
            select(TaskType)
            .where(TaskType.project_id == project_id, TaskType.name == name)
            .with_for_update()
        )
        task_type = (await self.session.execute(stmt)).scalars().first()
        if task_type is None:
            raise NotFound("Task type not found")

        shadow_ids: list[uuid.UUID] = []
        rate = 0.0
        if data.mode == "route":
            active = await self.session.execute(
                select(RoutingRule.id).where(
                    RoutingRule.project_id == project_id,
                    RoutingRule.task_type == name,
                    RoutingRule.status == "active",
                )
            )
            if active.first() is None:
                raise Unprocessable("Route mode needs an active rule: activate one first")
        elif data.mode == "shadow":
            shadow_ids = list(dict.fromkeys(data.shadow_model_ids))
            if not shadow_ids or data.shadow_rate <= 0:
                raise Unprocessable("Shadow mode needs shadow_model_ids and a shadow_rate above 0")
            found = await CalibrationRepository(self.session).candidates(shadow_ids)
            problems = [
                str(i)
                for i in shadow_ids
                if i not in found or not (found[i].enabled and found[i].provider_enabled)
            ]
            if problems:
                raise Unprocessable(f"Unknown or disabled models: {', '.join(problems)}")
            rate = data.shadow_rate
        task_type.mode = data.mode
        task_type.shadow_model_ids = shadow_ids
        task_type.shadow_rate = rate
        enqueue(
            self.session,
            RULES_CHANGED,
            {"project_id": str(project_id), "task_type": name, "reason": f"mode:{data.mode}"},
        )
        audit.record(
            self.session,
            action="task_type.update",
            actor_id=admin.id,
            target_type="project",
            target_id=project_id,
            details={"name": name, "mode": data.mode, "shadow_rate": rate},
            ip=ip,
        )
        await self.session.commit()
        return task_type
