"""OpenAI-compatible routes of the gateway (the data plane)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

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
