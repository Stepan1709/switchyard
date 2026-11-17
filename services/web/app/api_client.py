"""Calls to the api on behalf of the logged-in person.

One shared `httpx.AsyncClient` (connection pool); a thin per-request wrapper adds the access
token of the session and renews it with the refresh token shortly before it expires.
"""

import asyncio
import time
from typing import Any

import httpx

from app.config import Settings
from app.sessions import Session, SessionStore


class ApiError(Exception):
    """The api answered with an error (Problem Details) or could not be reached."""

    def __init__(self, status: int, title: str, detail: str = "") -> None:
        super().__init__(f"{status} {title}: {detail}")
        self.status = status
        self.title = title
        self.detail = detail or title

    @property
    def is_conflict(self) -> bool:
        return self.status == 409


class LoginRequired(Exception):
    """The session has no valid tokens any more: the person has to log in again."""


def problem(response: httpx.Response) -> ApiError:
    title, detail = response.reason_phrase or "Error", ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        title = str(body.get("title") or title)
        raw = body.get("detail")
        if isinstance(raw, list):  # request validation errors: field by field
            raw = "; ".join(
                f"{'.'.join(map(str, e.get('loc', [])[1:]))}: {e.get('msg')}" for e in raw
            )
        detail = str(raw or "")
    return ApiError(response.status_code, title, detail)


def forwarded_headers(address: str | None) -> dict[str, str]:
    return {"x-forwarded-for": address} if address else {}


class PublicApi:
    """Calls that need no login (the login itself)."""

    def __init__(self, http: httpx.AsyncClient, forwarded_for: str | None = None) -> None:
        self.http = http
        self.forwarded_for = forwarded_for

    async def post(self, path: str, json: Any) -> Any:
        try:
            response = await self.http.post(
                path, json=json, headers=forwarded_headers(self.forwarded_for)
            )
        except httpx.HTTPError as exc:
            raise ApiError(503, "The api is not reachable", type(exc).__name__) from exc
        if response.status_code >= 400:
            raise problem(response)
        return response.json()


class ApiClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        store: SessionStore,
        session: Session,
        settings: Settings,
        forwarded_for: str | None = None,
    ) -> None:
        self.http = http
        self.store = store
        self.session = session
        self.settings = settings
        self.forwarded_for = forwarded_for

    # ---- tokens ----
    async def _renew_if_needed(self, *, force: bool = False) -> None:
        data = self.session.data
        left = float(data.get("access_expires_at", 0)) - time.time()
        if not force and left > self.settings.refresh_margin_seconds:
            return
        if await self.store.acquire_refresh(self.session.id):
            try:
                try:
                    response = await self.http.post(
                        "/auth/refresh", json={"refresh_token": data.get("refresh_token", "")}
                    )
                except httpx.HTTPError as exc:
                    raise ApiError(503, "The api is not reachable", type(exc).__name__) from exc
                if response.status_code in (401, 403):
                    raise LoginRequired
                if response.status_code >= 400:
                    raise problem(response)
                await self.store.set_tokens(self.session, response.json())
            finally:
                await self.store.release_refresh(self.session.id)
            return
        # Somebody else is refreshing this session: wait for the new tokens.
        for _ in range(40):
            await asyncio.sleep(0.1)
            fresh = await self.store.load(self.session.id)
            if fresh is None:
                raise LoginRequired
            if float(fresh.data.get("access_expires_at", 0)) - time.time() > 0:
                self.session.data = fresh.data
                return
        raise LoginRequired

    # ---- requests ----
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        await self._renew_if_needed()
        for attempt in (1, 2):
            merged = {"authorization": f"Bearer {self.session.data.get('access_token', '')}"}
            merged.update(forwarded_headers(self.forwarded_for))
            merged.update(headers or {})
            try:
                response = await self.http.request(
                    method, path, params=_clean(params), json=json, headers=merged
                )
            except httpx.HTTPError as exc:
                raise ApiError(503, "The api is not reachable", type(exc).__name__) from exc
            if response.status_code == 401 and attempt == 1:
                await self._renew_if_needed(force=True)  # the token was revoked or clocks differ
                continue
            if response.status_code == 401:
                raise LoginRequired
            if response.status_code >= 400:
                raise problem(response)
            return response
        raise LoginRequired  # pragma: no cover

    async def get(self, path: str, **params: Any) -> Any:
        return (await self.request("GET", path, params=params)).json()

    async def send(
        self,
        method: str,
        path: str,
        json: Any = None,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"idempotency-key": idempotency_key} if idempotency_key else None
        response = await self.request(method, path, json=json, headers=headers)
        return response.json() if response.content else None


def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop empty filters so that `?model_id=` is never sent."""
    if not params:
        return None
    return {k: v for k, v in params.items() if v not in (None, "")}
