"""Sending traffic log events from the gateway.

`submit` is called when a request has finished and must never slow the response or fail it: it
only appends to a bounded in-memory buffer. A background task connects to RabbitMQ (retrying
for as long as it takes), serialises (and, if the project wants it, masks) the events and
publishes them with broker confirmation.

While the broker is unreachable events wait in the buffer. When the buffer is full the OLDEST
events are dropped and counted (`dropped_total`): losing some telemetry is acceptable, losing
a client's answer or running out of memory is not.
"""

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aio_pika
from aio_pika.abc import AbstractRobustConnection

from app.core.config import Settings
from app.core.events import REQUEST_COMPLETED, RequestCompleted
from app.core.logging import get_logger
from app.core.messaging import Message, Publisher, PublishError
from app.core.messaging.queues import declare_request_log_queue
from app.modules.gateway.pii import mask_json
from app.modules.gateway.record import RequestRecord

log = get_logger(__name__)

_BASE_EVENT_BYTES = 800  # what an event costs without bodies (for the buffer's byte limit)
_OFFLOAD_BYTES = 16 * 1024  # bigger bodies are masked and encoded outside the event loop


def build_event(record: RequestRecord) -> dict[str, Any]:
    """The event payload for a finished request (masking applied if the project asked)."""
    request_payload, response_payload = record.request_payload, record.response_payload
    masked = False
    if record.mask_pii and (request_payload is not None or response_payload is not None):
        request_payload = mask_json(request_payload) if request_payload is not None else None
        response_payload = mask_json(response_payload) if response_payload is not None else None
        masked = True
    event = RequestCompleted(
        request_id=record.request_id,  # type: ignore[arg-type]  # str -> UUID by validation
        started_at=record.started_at,
        project_id=record.project_id,  # type: ignore[arg-type]
        key_id=record.key_id,  # type: ignore[arg-type]
        task_type=record.task_type,
        mode=record.mode,
        stream=record.stream,
        status=record.status,
        http_status=record.http_status,
        error_code=record.error_code,
        requested_model=record.requested_model,
        model=record.model,
        model_id=record.model_id,  # type: ignore[arg-type]
        provider=record.provider,
        provider_id=record.provider_id,  # type: ignore[arg-type]
        prompt_tokens=record.prompt_tokens,
        completion_tokens=record.completion_tokens,
        ttft_ms=record.ttft_ms,
        duration_ms=record.duration_ms,
        overhead_ms=record.overhead_ms,
        request_payload=request_payload,
        response_payload=response_payload,
        payload_skipped=record.payload_skipped,
        payload_masked=masked,
    )
    return event.model_dump(mode="json")


@dataclass
class _Pending:
    record: RequestRecord
    size: int
    message: Message | None = None  # built once, reused if the publish has to be retried


Connect = Callable[[], Awaitable[AbstractRobustConnection]]


class TrafficEventPublisher:
    """Implements the gateway's `Recorder` protocol."""

    def __init__(self, settings: Settings, connect: Connect | None = None) -> None:
        self._settings = settings
        self._connect = connect or self._default_connect
        self._buffer: deque[_Pending] = deque()
        self._bytes = 0
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._empty = asyncio.Event()  # set while there is nothing waiting to be sent
        self._empty.set()
        self.dropped_total = 0
        self.published_total = 0
        self.connected = False
        self._connection: AbstractRobustConnection | None = None

    async def _default_connect(self) -> AbstractRobustConnection:
        return await aio_pika.connect_robust(
            self._settings.amqp_url,
            client_properties={"connection_name": self._settings.service_name},
        )

    # ---- the Recorder interface (called on the request path: cheap and never raises) ----
    def submit(self, record: RequestRecord) -> None:
        if record.project_id is None:
            return  # unauthenticated requests belong to nobody: nothing to log
        size = _BASE_EVENT_BYTES + record.payload_bytes
        self._buffer.append(_Pending(record, size))
        self._empty.clear()
        self._bytes += size
        self._enforce_limits()
        self._wake.set()

    def _enforce_limits(self) -> None:
        dropped = 0
        while self._buffer and (
            len(self._buffer) > self._settings.traffic_buffer_events
            or self._bytes > self._settings.traffic_buffer_bytes
        ):
            self._bytes -= self._buffer.popleft().size
            dropped += 1
        if not self._buffer:
            self._empty.set()
        if dropped:
            self.dropped_total += dropped
            log.error(
                "log_events_dropped",
                dropped=dropped,
                dropped_total=self.dropped_total,
                buffered=len(self._buffer),
            )

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    # ---- the sender ----
    async def run(self) -> None:
        backoff = 1.0
        publisher: Publisher | None = None
        while not self._stop.is_set():
            try:
                if publisher is None:
                    publisher = await self._connect_and_declare()
                    backoff = 1.0
                await self._drain(publisher)
            except Exception as exc:  # the broker is down or went away: wait and reconnect
                self.connected = False
                log.warning(
                    "traffic_publisher_error", error=type(exc).__name__, buffered=self.buffered
                )
                if not isinstance(exc, PublishError):
                    publisher = None  # connection-level problem: rebuild everything
                await self._sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            # buffer empty: wait for new events
            self._wake.clear()
            if self._buffer:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 1.0)

    async def _close_connection(self) -> None:
        """Close the broker connection, but never wait on a broker that is not answering."""
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.close(), 3)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), seconds)

    async def _connect_and_declare(self) -> Publisher:
        await self._close_connection()
        self._connection = await asyncio.wait_for(self._connect(), 10)
        channel = await self._connection.channel()  # publisher confirms on
        await declare_request_log_queue(channel, self._settings)
        publisher = await Publisher.create(channel)
        self.connected = True
        log.info("traffic_publisher_connected")
        return publisher

    async def _drain(self, publisher: Publisher) -> None:
        while self._buffer:
            pending = self._buffer[0]
            if pending.message is None:
                pending.message = await self._build_message(pending)
            await publisher.publish_event(pending.message)  # raises PublishError if refused
            self.published_total += 1
            # The head may have been dropped by the size limit while we were publishing.
            if self._buffer and self._buffer[0] is pending:
                self._buffer.popleft()
                self._bytes -= pending.size
            if not self._buffer:
                self._empty.set()

    async def _build_message(self, pending: _Pending) -> Message:
        record = pending.record
        if pending.size > _OFFLOAD_BYTES:
            payload = await asyncio.to_thread(build_event, record)  # masking can be heavy
        else:
            payload = build_event(record)
        return Message(
            type=REQUEST_COMPLETED,
            payload=payload,
            message_id=record.request_id,  # one id per request: a re-send is recognisable
            correlation_id=record.request_id,
        )

    # ---- shutdown ----
    async def stop(self) -> None:
        """Give the buffer a short time to drain, then stop."""
        self._wake.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._empty.wait(), self._settings.traffic_drain_timeout)
        if self._buffer:
            log.error("traffic_events_lost_on_shutdown", count=len(self._buffer))
        self._stop.set()
        self._wake.set()
        await self._close_connection()
