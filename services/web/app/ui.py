"""Per-request context of a page: the session, the api client and rendering helpers."""

import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.templating import Jinja2Templates

from app.api_client import ApiClient, ApiError, LoginRequired
from app.config import Settings
from app.security import check_csrf
from app.sessions import Session, SessionStore

NAV = (
    ("overview", "Overview", "/overview"),
    ("matrix", "Matrix", "/matrix"),
    ("models", "Models", "/models"),
    ("runs", "Calibrations", "/runs"),
    ("rules", "Rules", "/rules"),
    ("usage", "Usage", "/usage"),
    ("feedback", "Feedback", "/feedback"),
    ("projects", "Projects", "/projects"),
)


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def safe_local_path(value: str | None, default: str = "/") -> str:
    """Only paths of this site: never an absolute URL (open-redirect protection)."""
    if not value:
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or not value.startswith("/") or value.startswith("//"):
        return default
    return value


@dataclass
class Ctx:
    request: Request
    session: Session
    api: ApiClient
    store: SessionStore
    templates: Jinja2Templates
    settings: Settings
    _projects: list[dict[str, Any]] | None = field(default=None, repr=False)

    # ---- projects ----
    async def projects(self) -> list[dict[str, Any]]:
        if self._projects is None:
            self._projects = await self.api.get("/projects")
        return self._projects

    async def project(self) -> dict[str, Any] | None:
        """The project the person works in: the chosen one, else the first they can see."""
        projects = await self.projects()
        chosen = self.session.project_id
        for p in projects:
            if p["id"] == chosen:
                return p
        if projects:
            self.session.data["project_id"] = projects[0]["id"]
            await self.store.save(self.session)
            return projects[0]
        return None

    async def project_id(self) -> str:
        project = await self.project()
        if project is None:
            raise ApiError(404, "No project yet", "Create a project first (Projects).")
        return str(project["id"])

    # ---- feedback to the person ----
    async def flash(self, kind: str, text: str) -> None:
        items = list(self.session.data.get("flash", []))
        items.append({"kind": kind, "text": text})
        self.session.data["flash"] = items[-5:]
        await self.store.save(self.session)

    async def _take_flash(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = list(self.session.data.pop("flash", []))
        if items:
            await self.store.save(self.session)
        return items

    # ---- answers ----
    async def render(
        self, template: str, *, page: str = "", status_code: int = 200, **values: Any
    ) -> Response:
        context: dict[str, Any] = {
            "session": self.session,
            "csrf": self.session.csrf,
            "nav": NAV,
            "page": page,
            "flashes": await self._take_flash(),
            "can_write": self.session.is_admin,
            **values,
        }
        if not is_htmx(self.request) or self.request.headers.get("hx-boosted") == "true":
            projects = await self.projects()
            context["projects"] = projects
            context["project"] = await self.project()
        return self.templates.TemplateResponse(
            self.request, template, context, status_code=status_code
        )

    def redirect(self, path: str) -> Response:
        return RedirectResponse(safe_local_path(path), status_code=303)

    def new_idempotency_key(self) -> str:
        return str(uuid.uuid4())


def make_ctx(request: Request, session: Session) -> Ctx:
    state = request.app.state
    store: SessionStore = state.sessions
    return Ctx(
        request=request,
        session=session,
        api=ApiClient(state.api_http, store, session, state.settings),
        store=store,
        templates=state.templates,
        settings=state.settings,
    )


async def _session_of(request: Request) -> Session:
    state = request.app.state
    cookie = request.cookies.get(state.settings.session_cookie)
    session: Session | None = await state.sessions.load(cookie)
    if session is None or not session.authenticated:
        raise LoginRequired
    request.state.session = session
    return session


async def page(request: Request) -> Ctx:
    """Dependency of every page that needs a login."""
    return make_ctx(request, await _session_of(request))


async def action(request: Request) -> Ctx:
    """Dependency of every request that changes something: login and CSRF token required."""
    session = await _session_of(request)
    await check_csrf(request, session.csrf)
    return make_ctx(request, session)


async def admin_action(request: Request) -> Ctx:
    ctx = await action(request)
    if not ctx.session.is_admin:
        raise ApiError(403, "Forbidden", "Your account may look but not change.")
    return ctx
