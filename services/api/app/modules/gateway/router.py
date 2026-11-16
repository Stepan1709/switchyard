"""OpenAI-compatible routes of the gateway (the data plane)."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.core.events import FEEDBACK_SUBMITTED
from app.modules.gateway import errors
from app.modules.gateway.errors import GatewayError
from app.modules.gateway.proxy import GatewayProxy, models_response

router = APIRouter()


def _proxy(request: Request) -> GatewayProxy:
    proxy: GatewayProxy = request.app.state.proxy
    return proxy


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    # The body is not parsed by FastAPI: the proxy reads and checks it itself, so it can
    # enforce the size limit and avoid building validation models on the hot path.
    return await _proxy(request).chat(request)


@router.post("/v1/feedback")
async def feedback(request: Request) -> Response:
    """A client rates one of its answers: `{"request_id": ..., "rating": 1 | -1, "comment": ...}`.

    The answer is 202: the rating is queued and stored by the worker, which also checks that
    the request belongs to the key's project. The gateway does not look anything up, so the
    endpoint works while the database is down and an unknown or foreign request id is simply
    never stored. Rating the same request again replaces the first rating."""
    proxy = _proxy(request)
    try:
        live = proxy._snapshot()
        key = proxy.authenticate(request, live)
        await proxy._rate_limit(key)
        raw = await proxy._read_body(request)
        payload = _feedback_payload(raw)
    except GatewayError as exc:
        return exc.response()
    request.app.state.events.submit_event(
        FEEDBACK_SUBMITTED,
        {
            **payload,
            "project_id": key.project_id,
            "key_id": key.key_id,
            "submitted_at": datetime.now(UTC).isoformat(),
        },
        correlation_id=payload["request_id"],
    )
    return JSONResponse(
        {"status": "accepted", "request_id": payload["request_id"]}, status_code=202
    )


def _feedback_payload(raw: bytes) -> dict[str, Any]:
    try:
        data: Any = json.loads(raw)
    except ValueError as exc:
        raise errors.bad_request("The body is not valid JSON.", "invalid_json") from exc
    if not isinstance(data, dict):
        raise errors.bad_request("The body must be a JSON object.", "invalid_body")
    try:
        request_id = str(uuid.UUID(str(data.get("request_id"))))
    except ValueError as exc:
        raise errors.bad_request("request_id must be a UUID.", "invalid_request_id") from exc
    rating = data.get("rating")
    if isinstance(rating, bool) or rating not in (1, -1):
        raise errors.bad_request("rating must be 1 (good) or -1 (bad).", "invalid_rating")
    comment = data.get("comment")
    if comment is not None and (not isinstance(comment, str) or len(comment) > 1000):
        raise errors.bad_request(
            "comment must be text of up to 1000 characters.", "invalid_comment"
        )
    return {"request_id": request_id, "rating": rating, "comment": comment}


@router.get("/v1/models")
async def list_models(request: Request) -> Response:
    proxy = _proxy(request)
    try:
        live = proxy._snapshot()
        proxy.authenticate(request, live)
    except GatewayError as exc:
        return exc.response()
    return models_response(live)


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    """Ready when a snapshot is loaded.

    PostgreSQL and RabbitMQ are deliberately not required: a gateway with a snapshot keeps
    serving while they are down, and taking it out of rotation would make an outage worse.
    The body reports how fresh the snapshot is, for diagnostics.
    """
    store = _proxy(request).store
    events = request.app.state.events
    current = store.current
    if current is None:
        return JSONResponse(
            {"status": "unavailable", "checks": {"snapshot": "missing"}}, status_code=503
        )
    return JSONResponse(
        {
            "status": "ok",
            "checks": {"snapshot": "ok"},
            "snapshot": {
                "version": current.snapshot.version,
                "source": store.source,
                "last_refresh_ok": store.last_refresh_ok,
            },
            "traffic_events": {
                "connected": events.connected,
                "buffered": events.buffered,
                "dropped_total": events.dropped_total,
                "published_total": events.published_total,
            },
        }
    )
