"""Hot swap of the routing rules: refresh the snapshot as soon as `rules.changed` arrives.

Every gateway instance has its own private queue bound to the event (see
`declare_broadcast_queue`), so each instance hears every change. The queue lives only while its
owner is connected; what is missed during a disconnect is picked up by the snapshot's periodic
refresh, which stays in place as the safety net.

The listener never blocks the hot path and never makes the gateway depend on RabbitMQ: if the
broker is down it keeps retrying in the background and the gateway keeps routing by the last
snapshot.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.core.config import Settings
from app.core.events import RULES_CHANGED
from app.core.logging import get_logger
from app.core.messaging import declare_broadcast_queue

log = get_logger(__name__)


class RulesListener:
    def __init__(self, settings: Settings, refresh: Callable[[], Awaitable[object]]) -> None:
        self._settings = settings
        self._refresh = refresh
        self._stop = asyncio.Event()
        self._dirty = asyncio.Event()
        self.connected = False
        self.events_seen = 0

    async def run(self) -> None:
        """Listen until stop(). Two cooperating tasks: one receives, one refreshes (so a burst
        of events causes one refresh, not one per event)."""
        refresher = asyncio.create_task(self._refresh_loop(), name="rules-refresh")
        try:
            await self._listen()
        finally:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher

    async def _listen(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            connection = None
            try:
                connection = await asyncio.wait_for(
                    aio_pika.connect_robust(
                        self._settings.amqp_url,
                        client_properties={"connection_name": "gateway-rules"},
                    ),
                    10,
                )
                channel = await connection.channel()
                queue = await declare_broadcast_queue(channel, [RULES_CHANGED])
                await queue.consume(self._on_message)
                self.connected = True
                delay = 1.0
                log.info("rules_listener_connected")
                await self._stop.wait()  # robust connections reconnect and re-subscribe alone
            except Exception as exc:
                self.connected = False
                log.warning("rules_listener_failed", error=type(exc).__name__)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), delay)
                delay = min(delay * 2, 30.0)
            finally:
                self.connected = False
                if connection is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(connection.close(), 3)

    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        async with message.process(ignore_processed=True):
            self.events_seen += 1
            self._dirty.set()

    async def _refresh_loop(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            try:
                await self._refresh()
            except Exception as exc:  # a failed refresh leaves the old snapshot in use
                log.warning("rules_refresh_failed", error=type(exc).__name__)

    def stop(self) -> None:
        self._stop.set()
