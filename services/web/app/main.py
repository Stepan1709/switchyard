"""The web interface: server-rendered pages (Jinja2 + HTMX + Alpine) in front of the admin api.

The browser never sees an API token: it holds a random session id in an HttpOnly cookie and the
tokens stay in Redis (the "backend for frontend" pattern).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from starlette.exceptions import HTTPException
from starlette.templating import Jinja2Templates

from app.api_client import ApiError, LoginRequired
from app.config import Settings
from app.format import FILTERS
from app.logging import configure_logging, get_logger
from app.routes import ROUTERS
from app.security import SecurityHeadersMiddleware
from app.sessions import SessionStore
from app.ui import is_htmx, make_ctx, safe_local_path

BASE = Path(__file__).parent
log = get_logger(__name__)


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(BASE / "templates"))
    templates.env.filters.update(FILTERS)
    return templates


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    own_redis = app.state.redis is None
    if own_redis:
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    own_http = app.state.api_http is None
    if own_http:
        app.state.api_http = httpx.AsyncClient(
            base_url=settings.api_url,
            timeout=httpx.Timeout(settings.api_timeout, connect=3.0),
            limits=httpx.Limits(max_connections=settings.api_max_connections),
            follow_redirects=False,
        )
    app.state.sessions = SessionStore(app.state.redis, settings)
    log.info("web.started", api_url=settings.api_url)
    try:
        yield
    finally:
        if own_http:
            await app.state.api_http.aclose()
        if own_redis:
            await app.state.redis.aclose()


def create_app(
    settings: Settings | None = None,
    *,
    redis: Any = None,
    api_http: httpx.AsyncClient | None = None,
) -> FastAPI:
    """`redis` and `api_http` can be given by tests; otherwise they are created here."""
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.env, settings.log_level)
    app = FastAPI(
        title="Switchyard web", lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    app.state.redis = redis
    app.state.api_http = api_http
    app.state.templates = build_templates()
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    for router in ROUTERS:
        app.include_router(router)
    _add_error_handlers(app)
    return app


# ---- errors ------------------------------------------------------------------------------------
def _title(status: int) -> str:
    return {
        401: "Sign in required",
        403: "Not allowed",
        404: "Not found",
        409: "Conflict",
        429: "Too many requests",
        503: "The api is not reachable",
    }.get(status, "Something went wrong")


async def _error_response(request: Request, status: int, title: str, detail: str) -> Response:
    """An error page in the normal layout if the person is logged in, otherwise a bare one."""
    state = request.app.state
    session = await state.sessions.load(request.cookies.get(state.settings.session_cookie))
    if is_htmx(request) and request.headers.get("hx-boosted") != "true":
        # A fragment (progress bar, detail panel): show the problem in its place.
        fragment: Response = state.templates.TemplateResponse(
            request, "partials/error.html", {"title": title, "detail": detail}
        )
        return fragment
    if session is not None and session.authenticated:
        ctx = make_ctx(request, session)
        try:
            return await ctx.render(
                "error.html", status_code=status, title=title, detail=detail, status=status
            )
        except (ApiError, LoginRequired):  # the api is down: fall back to the bare page
            pass
    bare: Response = state.templates.TemplateResponse(
        request,
        "error_bare.html",
        {"title": title, "detail": detail, "status": status},
        status_code=status,
    )
    return bare


def _add_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        state = request.app.state
        await state.sessions.destroy(request.cookies.get(state.settings.session_cookie))
        target = "/login"
        if request.method == "GET" and not is_htmx(request):
            target += "?next=" + safe_local_path(request.url.path)
        if is_htmx(request):
            response: Response = Response(status_code=200, headers={"HX-Redirect": target})
        else:
            response = Response(status_code=303, headers={"location": target})
        response.delete_cookie(state.settings.session_cookie, path="/")
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> Response:
        if request.method != "GET":
            # A form was rejected: say why on the page it came from.
            state = request.app.state
            session = await state.sessions.load(request.cookies.get(state.settings.session_cookie))
            if session is not None and session.authenticated:
                ctx = make_ctx(request, session)
                await ctx.flash("error", exc.detail)
                back = safe_local_path(_referer_path(request), "/")
                return Response(status_code=303, headers={"location": back})
        status = exc.status if exc.status >= 400 else 502
        return await _error_response(request, status, exc.title or _title(status), exc.detail)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        return await _error_response(
            request, exc.status_code, _title(exc.status_code), str(exc.detail)
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> Response:
        log.error("web.unhandled", path=request.url.path, error=type(exc).__name__)
        return await _error_response(request, 500, "Something went wrong", "Please try again.")

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        state = request.app.state
        try:
            await state.redis.ping()
            response = await state.api_http.get("/health/ready", timeout=2.0)
            ok = response.status_code == 200
        except Exception:
            ok = False
        return JSONResponse(
            {"status": "ok" if ok else "unavailable"}, status_code=200 if ok else 503
        )


def _referer_path(request: Request) -> str | None:
    referer = request.headers.get("referer")
    if not referer:
        return None
    parts = urlsplit(referer)
    return parts.path + (f"?{parts.query}" if parts.query else "")
