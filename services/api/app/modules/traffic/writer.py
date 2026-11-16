"""The traffic log writer: consumes `request.completed` and inserts rows in batches.

One INSERT per message would make the database the bottleneck, so messages are collected for a
moment and written together. A handler returns (and its message is acked) only after the batch
it belongs to has been committed, so a crash can never lose an acknowledged event.

Idempotency needs no inbox here: the row's own key (started_at, request_id) is the natural
one, and `ON CONFLICT DO NOTHING` makes a re-delivered event a no-op.
"""

import asyncio
import contextlib
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.events import SHADOW_EXECUTE, RequestCompleted
from app.core.logging import get_logger
from app.core.messaging import Message, PermanentError, TemporaryError
from app.core.outbox import enqueue
from app.modules.traffic import partitions
from app.modules.traffic.models import TrafficRequest

log = get_logger(__name__)

_COLUMNS = [c for c in RequestCompleted.model_fields if c != "shadow_model_ids"]
_TASK_TYPE_SQL = text(
    """
    INSERT INTO task_types (id, project_id, name, mode, auto_registered)
    SELECT gen_random_uuid(), :project_id, :name, 'record', true
    WHERE EXISTS (SELECT 1 FROM projects WHERE id = :project_id)
      AND (SELECT count(*) FROM task_types WHERE project_id = :project_id) < :cap
    ON CONFLICT (project_id, name) DO NOTHING
    """
)
_KNOWN_CACHE_LIMIT = 50_000


class RequestLogWriter:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._pending: list[tuple[RequestCompleted, asyncio.Future[None]]] = []
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._known_months: set[date] = set()
        self._known_task_types: set[tuple[str, str]] = set()
        self.rows_written = 0
        self.batches = 0

    # ---- lifecycle (called by the consumer) ----
    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="request-log-flusher")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            await self._task

    # ---- the message handler ----
    async def handle(self, message: Message) -> None:
        try:
            event = RequestCompleted.model_validate(message.payload)
        except ValidationError as exc:
            raise PermanentError(
                f"invalid request.completed payload: {exc.error_count()} errors"
            ) from exc

        now = datetime.now(UTC)
        if event.started_at > now + timedelta(days=2):
            # A clock that is days ahead would make partitions for a future nobody asked for.
            raise PermanentError("started_at is too far in the future")
        oldest = partitions.add_months(
            partitions.month_start(now), -self._settings.traffic_retention_months
        )
        if event.started_at.date() < oldest:
            log.warning("event_past_retention_dropped", request_id=str(event.request_id))
            return  # a stale event (for example requeued from the DLQ months later): ack and skip

        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending.append((event, future))
        self._wake.set()
        await future  # resolves after the batch is committed, or raises TemporaryError

    # ---- batching ----
    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            if not self._pending:
                if self._stopping:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            deadline = loop.time() + self._settings.traffic_batch_wait
            while len(self._pending) < self._settings.traffic_batch_size and not self._stopping:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), remaining)
            size = self._settings.traffic_batch_size
            batch, self._pending = self._pending[:size], self._pending[size:]
            await self._flush(batch)

    async def _flush(self, batch: list[tuple[RequestCompleted, asyncio.Future[None]]]) -> None:
        try:
            await self._write([event for event, _ in batch])
        except Exception as exc:
            log.error("request_log_batch_failed", size=len(batch), error=type(exc).__name__)
            if len(batch) > 1:
                # One bad event must not make its neighbours fail: write them one by one, so
                # only the event that really cannot be stored goes through the retry path.
                for item in batch:
                    await self._flush([item])
                return
            for _, future in batch:
                if not future.done():
                    future.set_exception(TemporaryError(f"insert failed: {type(exc).__name__}"))
            return
        self.batches += 1
        self.rows_written += len(batch)
        for _, future in batch:
            if not future.done():
                future.set_result(None)

    @staticmethod
    def _queue_shadow_copies(session: AsyncSession, events: list[RequestCompleted]) -> None:
        """Shadow mode: one job per (request, candidate), written in the SAME transaction as the
        request itself. A request that was stored always gets its copies queued, a request that
        was a duplicate delivery (and so not stored again) never queues them twice."""
        for e in events:
            if not e.shadow_model_ids or e.payload_skipped or e.request_payload is None:
                continue  # without the stored prompt there is nothing to copy
            for model_id in e.shadow_model_ids:
                enqueue(
                    session,
                    SHADOW_EXECUTE,
                    {
                        "request_id": str(e.request_id),
                        "started_at": e.started_at.isoformat(),
                        "project_id": str(e.project_id),
                        "task_type": e.task_type,
                        "model_id": str(model_id),
                    },
                    correlation_id=str(e.request_id),
                )

    async def _write(self, events: list[RequestCompleted]) -> None:
        rows: list[dict[str, Any]] = [{c: getattr(e, c) for c in _COLUMNS} for e in events]
        months = {partitions.month_start(e.started_at) for e in events}
        new_months = months - self._known_months
        async with self._session_factory() as session, session.begin():
            if new_months:  # a partition for each month in the batch must exist before the insert
                await partitions.ensure_partitions(session, sorted(new_months))
            inserted = set(
                (
                    await session.execute(
                        pg_insert(TrafficRequest)
                        .values(rows)
                        .on_conflict_do_nothing(index_elements=["started_at", "request_id"])
                        .returning(TrafficRequest.request_id)
                    )
                ).scalars()
            )
            self._queue_shadow_copies(session, [e for e in events if e.request_id in inserted])
            await self._register_task_types(session, events)
        self._known_months |= new_months

    async def _register_task_types(
        self, session: AsyncSession, events: list[RequestCompleted]
    ) -> None:
        """Create a task type the first time it is seen (up to the per-project cap)."""
        fresh = {
            (str(e.project_id), e.task_type)
            for e in events
            if e.task_type and (str(e.project_id), e.task_type) not in self._known_task_types
        }
        for project_id, name in fresh:
            await session.execute(
                _TASK_TYPE_SQL,
                {
                    "project_id": project_id,
                    "name": name,
                    "cap": self._settings.task_types_per_project,
                },
            )
        if len(self._known_task_types) > _KNOWN_CACHE_LIMIT:
            self._known_task_types.clear()
        self._known_task_types |= fresh
