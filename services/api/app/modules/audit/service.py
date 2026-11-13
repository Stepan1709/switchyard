"""Audit trail. `record` only adds a row to the caller's session, so the audit entry is
committed (or rolled back) in the same transaction as the action it describes."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import correlation_id_var
from app.modules.audit.models import AuditLog


def record(
    session: AsyncSession,
    *,
    action: str,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: object | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details=details or {},
            ip=ip,
            correlation_id=correlation_id_var.get(),
        )
    )
