"""Pure ASGI middleware: correlation id + one access-log line per request.

Written as a raw ASGI middleware, not BaseHTTPMiddleware: the latter wraps the response body
and breaks streaming and contextvars propagation, which matters for the gateway later.
"""

import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import correlation_id_var, get_logger

HEADER = "x-request-id"
log = get_logger("access")


def _valid_id(value: str) -> bool:
    # Accept a client-supplied id only if it is short and harmless (no log injection).
    return 0 < len(value) <= 64 and all(c.isalnum() or c in "-_" for c in value)


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(HEADER.encode(), b"").decode("latin-1")
        cid = incoming if _valid_id(incoming) else str(uuid.uuid4())
        token = correlation_id_var.set(cid)
        # Also kept in scope["state"] for handlers that run outside this middleware.
        scope.setdefault("state", {})["correlation_id"] = cid
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[HEADER] = cid
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            log.info(
                "request",
                method=scope["method"],
                path=scope["path"],
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            correlation_id_var.reset(token)
