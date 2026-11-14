"""Health endpoints.

/health/live  - the process is alive (no dependencies are touched).
/health/ready - PostgreSQL, Redis and RabbitMQ are reachable; 503 otherwise, so an
                orchestrator stops sending traffic without restarting the process.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.health import check_dependencies
from app.core.resources import Resources

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    res: Resources = request.app.state.resources
    checks = await check_dependencies(res, request.app.state.settings.health_check_timeout)
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "unavailable", "checks": checks},
        status_code=200 if ok else 503,
    )
