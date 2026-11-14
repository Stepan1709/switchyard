"""Idempotency-Key for unsafe administrator POSTs.

A client that did not get a response (timeout, dropped connection) cannot know whether its
POST was executed. With an `Idempotency-Key` header it can send the same request again:

* first request with a key  -> executed; the response is stored under (admin, key)
* same key, same request    -> the stored response is returned again (`Idempotent-Replayed: true`),
                               nothing is executed twice
* same key, different body  -> 422: a key identifies one request, reusing it is a client bug
* same key while the first is still running -> 409 with Retry-After
* the first attempt failed (any error) -> the claim is removed, so a retry may run

The claim is written in its own short transaction and committed BEFORE the action runs, so a
concurrent duplicate sees it at once. A claim left behind by a crashed process is taken over
after `lock_seconds`; stored responses expire after `ttl_hours`.

Responses that contain secrets (client key creation) must not use this: the response body is
stored in the database.
"""

import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import DateTime, ForeignKey, SmallInteger, String, UniqueConstraint, delete, func
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, UUIDPrimaryKeyMixin
from app.core.errors import AppError, Unprocessable

_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,255}")


class IdempotencyKey(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("admin_id", "key", name="uq_idempotency_keys_admin_key"),)

    admin_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admins.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int | None] = mapped_column(SmallInteger)  # null while in progress
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class IdempotencyKeyReused(AppError):
    status_code = 422
    title = "Idempotency key reused"
    code = "idempotency_key_reused"


class RequestInProgress(AppError):
    status_code = 409
    title = "Request in progress"
    code = "request_in_progress"

    def __init__(self) -> None:
        super().__init__(
            "A request with this Idempotency-Key is still being processed",
            headers={"Retry-After": "1"},
        )


class Idempotency:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        admin_id: uuid.UUID,
        key: str | None,
        method: str,
        path: str,
        body: bytes,
        ttl_hours: int = 24,
        lock_seconds: int = 120,
    ) -> None:
        if key is not None and not _KEY_RE.fullmatch(key):
            raise Unprocessable("Idempotency-Key must be 1-255 characters of [A-Za-z0-9._:-]")
        self._factory = session_factory
        self._admin_id = admin_id
        self._key = key
        self._method = method
        self._path = path
        self._hash = hashlib.sha256(body).hexdigest()
        self._ttl = timedelta(hours=ttl_hours)
        self._lock = timedelta(seconds=lock_seconds)

    async def run(
        self, call: Callable[[], Awaitable[BaseModel]], *, status_code: int
    ) -> BaseModel | JSONResponse:
        """Execute `call` once per key. Without the header it is simply executed."""
        if self._key is None:
            return await call()

        replay = await self._claim()
        if replay is not None:
            return replay
        try:
            result = await call()
        except BaseException:
            await self._release()  # a failed attempt must not block the retry
            raise
        await self._store(status_code, result.model_dump(mode="json"))
        return result

    # ---- storage (each step in its own short transaction) -----------------------------------
    async def _claim(self) -> JSONResponse | None:
        """Returns a stored response to replay, or None when this request should run now."""
        assert self._key is not None
        now = datetime.now(UTC)
        values = {
            "admin_id": self._admin_id,
            "key": self._key,
            "method": self._method,
            "path": self._path,
            "request_hash": self._hash,
            "locked_at": now,
            "expires_at": now + self._ttl,
        }
        async with self._factory() as session, session.begin():
            inserted = (
                await session.execute(
                    pg_insert(IdempotencyKey)
                    .values(id=uuid.uuid4(), **values)
                    .on_conflict_do_nothing(constraint="uq_idempotency_keys_admin_key")
                    .returning(IdempotencyKey.id)
                )
            ).first()
            if inserted is not None:
                return None

            row = (
                await session.execute(
                    sa_select(IdempotencyKey)
                    .where(
                        IdempotencyKey.admin_id == self._admin_id, IdempotencyKey.key == self._key
                    )
                    .with_for_update()
                )
            ).scalar_one()
            expired = row.expires_at <= now
            stale = row.status_code is None and row.locked_at <= now - self._lock
            if expired or stale:  # take the key over
                for field, value in values.items():
                    setattr(row, field, value)
                row.status_code = None
                row.response = None
                return None
            if (row.method, row.path, row.request_hash) != (
                self._method,
                self._path,
                self._hash,
            ):
                raise IdempotencyKeyReused(
                    "This Idempotency-Key was already used for a different request"
                )
            if row.status_code is None:
                raise RequestInProgress
            return JSONResponse(
                row.response, status_code=row.status_code, headers={"Idempotent-Replayed": "true"}
            )

    async def _store(self, status_code: int, response: dict[str, Any]) -> None:
        async with self._factory() as session, session.begin():
            row = (
                await session.execute(
                    sa_select(IdempotencyKey).where(
                        IdempotencyKey.admin_id == self._admin_id, IdempotencyKey.key == self._key
                    )
                )
            ).scalar_one()
            row.status_code = status_code
            row.response = response

    async def _release(self) -> None:
        try:
            async with self._factory() as session, session.begin():
                await session.execute(
                    delete(IdempotencyKey).where(
                        IdempotencyKey.admin_id == self._admin_id,
                        IdempotencyKey.key == self._key,
                        IdempotencyKey.status_code.is_(None),
                    )
                )
        except Exception:
            pass  # the stale claim will be taken over after lock_seconds


async def purge_expired(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Delete expired idempotency keys; returns how many were removed."""
    async with session_factory() as session, session.begin():
        result = await session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.expires_at <= datetime.now(UTC))
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
