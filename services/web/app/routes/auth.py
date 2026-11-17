"""Login, logout and the choice of the current project."""

import contextlib
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.api_client import ApiError, PublicApi
from app.sessions import Session
from app.ui import Ctx, action, safe_local_path, visitor_address

router = APIRouter()


def _set_cookie(request: Request, response: Response, session_id: str, max_age: int | None) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        settings.session_cookie,
        session_id,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


async def _login_page(
    request: Request, session: Session, *, next_path: str, error: str = "", status: int = 200
) -> Response:
    templates = request.app.state.templates
    response: Response = templates.TemplateResponse(
        request,
        "login.html",
        {"csrf": session.csrf, "next": next_path, "error": error},
        status_code=status,
    )
    settings = request.app.state.settings
    _set_cookie(request, response, session.id, settings.anonymous_ttl_seconds)
    return response


@router.get("/login")
async def login_form(request: Request, next: str = "/") -> Response:
    state = request.app.state
    existing = await state.sessions.load(request.cookies.get(state.settings.session_cookie))
    if existing is not None and existing.authenticated:
        return RedirectResponse(safe_local_path(next, "/overview"), status_code=303)
    session = existing or await state.sessions.create_anonymous()
    return await _login_page(request, session, next_path=safe_local_path(next))


@router.post("/login")
async def login(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    state = request.app.state
    anonymous = await state.sessions.load(request.cookies.get(state.settings.session_cookie))
    if (
        anonymous is None
        or anonymous.authenticated
        or not secrets.compare_digest(csrf_token.encode(), anonymous.csrf.encode())
    ):
        # The form is older than its session: show a fresh one instead of a bare error.
        fresh = await state.sessions.create_anonymous()
        return await _login_page(
            request, fresh, next_path=safe_local_path(next),
            error="The form has expired. Please sign in again.", status=400,
        )  # fmt: skip
    public = PublicApi(state.api_http, visitor_address(request, state.settings.trust_forwarded_for))
    try:
        tokens = await public.post("/auth/login", {"email": email, "password": password})
        me = (
            await state.api_http.get(
                "/auth/me", headers={"authorization": f"Bearer {tokens['access_token']}"}
            )
        ).json()
    except ApiError as exc:
        message = {
            401: "Wrong email or password.",
            422: "Enter a valid email address and a password.",
            429: "Too many attempts. Wait a few minutes and try again.",
        }.get(exc.status, exc.detail)
        status = 401 if exc.status == 401 else exc.status if exc.status >= 400 else 400
        return await _login_page(
            request, anonymous, next_path=safe_local_path(next), error=message, status=status
        )
    session = await state.sessions.create(tokens, me)
    await state.sessions.destroy(anonymous.id)
    response = RedirectResponse(safe_local_path(next, "/overview"), status_code=303)
    _set_cookie(request, response, session.id, state.settings.session_ttl_seconds)
    return response


@router.post("/logout")
async def logout(request: Request, ctx: Annotated[Ctx, Depends(action)]) -> Response:
    with contextlib.suppress(Exception):  # the session is destroyed whatever the api says
        await ctx.api.http.post(
            "/auth/logout", json={"refresh_token": ctx.session.data.get("refresh_token", "")}
        )
    await ctx.store.destroy(ctx.session.id)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(request.app.state.settings.session_cookie, path="/")
    return response


@router.post("/project/select")
async def select_project(
    request: Request,
    ctx: Annotated[Ctx, Depends(action)],
    project_id: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/overview",
) -> Response:
    projects = await ctx.projects()
    if not any(p["id"] == project_id for p in projects):
        raise ApiError(404, "Not found", "That project does not exist.")
    ctx.session.data["project_id"] = project_id
    await ctx.store.save(ctx.session)
    return ctx.redirect(next)
