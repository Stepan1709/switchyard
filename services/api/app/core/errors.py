"""Unified error format for the admin API: RFC 9457 Problem Details.

The OpenAI-style error format for /v1/* belongs to the gateway and is added with it.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import correlation_id_var, get_logger

PROBLEM_JSON = "application/problem+json"
log = get_logger(__name__)


class AppError(Exception):
    """Domain error with an HTTP status; handlers turn it into Problem Details."""

    status_code = 400
    title = "Bad request"
    code = "bad_request"

    def __init__(self, detail: str | None = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.headers = headers


class Unauthorized(AppError):
    status_code = 401
    title = "Unauthorized"
    code = "unauthorized"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail, headers={"WWW-Authenticate": "Bearer"})


class Forbidden(AppError):
    status_code = 403
    title = "Forbidden"
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    title = "Not found"
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    title = "Conflict"
    code = "conflict"


class TooManyRequests(AppError):
    status_code = 429
    title = "Too many requests"
    code = "too_many_requests"

    def __init__(self, retry_after: int, detail: str | None = None) -> None:
        super().__init__(detail, headers={"Retry-After": str(max(retry_after, 1))})


def problem(
    status: int,
    title: str,
    detail: str | None = None,
    code: str | None = None,
    correlation_id: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: object,
) -> JSONResponse:
    body: dict[str, object] = {"type": "about:blank", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    if code:
        body["code"] = code
    cid = correlation_id or correlation_id_var.get()
    if cid:
        body["correlation_id"] = cid
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def _cid(request: Request) -> str | None:
    # Starlette runs the catch-all 500 handler outside our middleware, after the context
    # variable is reset, so the id is also stored in the ASGI scope state.
    state = request.scope.get("state") or {}
    return state.get("correlation_id")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return problem(
            exc.status_code,
            exc.title,
            exc.detail,
            exc.code,
            _cid(request),
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, str(exc.detail), correlation_id=_cid(request))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Only location and message are returned; the input value itself may be sensitive.
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return problem(
            422,
            "Validation error",
            code="validation_error",
            correlation_id=_cid(request),
            errors=errors,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", exc_info=exc)
        # Internal details are never exposed to the client.
        return problem(
            500, "Internal server error", code="internal_error", correlation_id=_cid(request)
        )
