"""Test setup: the web app against a fake api and an in-memory Redis (no network, no Docker)."""

import re
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app

PROJECT_ID = "11111111-1111-1111-1111-111111111111"
REFERENCE_ID = "aaaaaaaa-0000-0000-0000-000000000001"
TINY_ID = "aaaaaaaa-0000-0000-0000-000000000002"
RUN_ID = "bbbbbbbb-0000-0000-0000-000000000001"


class FakeRedis:
    """The few commands of redis.asyncio that the web app uses."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(
        self,
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
        keepttl: bool = False,
    ) -> bool | None:
        if nx and key in self.data:
            return None
        self.data[key] = value
        if not keepttl:
            self.ttl[key] = ex
        return True

    async def expire(self, key: str, seconds: int) -> bool:
        if key in self.data:
            self.ttl[key] = seconds
        return key in self.data

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    def sessions(self) -> dict[str, dict[str, Any]]:
        import json

        return {k: json.loads(v) for k, v in self.data.items() if k.startswith("web:session:")}


Handler = Callable[[httpx.Request], httpx.Response]


class FakeApi:
    """Canned answers of the admin api, and a record of what the web app asked."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler | tuple[int, Any]] = {}
        self.calls: list[httpx.Request] = []

    def on(self, method: str, path: str, body: Any = None, status: int = 200) -> None:
        self.routes[(method, path)] = (status, body)

    def on_call(self, method: str, path: str, handler: Handler) -> None:
        self.routes[(method, path)] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        spec = self.routes.get((request.method, request.url.path))
        if spec is None:
            return httpx.Response(
                404,
                json={
                    "title": "Not found",
                    "detail": f"no fake route {request.method} {request.url.path}",
                },
            )
        if callable(spec):
            return spec(request)
        status, body = spec
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    def calls_to(self, method: str, path: str) -> list[httpx.Request]:
        return [c for c in self.calls if c.method == method and c.url.path == path]


def tokens(
    access: str = "access-1", refresh: str = "refresh-1", expires_in: int = 900
) -> dict[str, Any]:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": expires_in,
    }


def admin_user(role: str = "admin") -> dict[str, Any]:
    return {"id": "22222222-2222-2222-2222-222222222222", "email": "admin@example.com", "role": role,
            "is_active": True, "created_at": "2026-11-01T10:00:00Z"}  # fmt: skip


PROJECT = {
    "id": PROJECT_ID, "name": "Support bot", "description": "tickets", "reference_model_id": REFERENCE_ID,
    "store_bodies": True, "mask_pii": False, "created_at": "2026-11-01T10:00:00Z",
}  # fmt: skip

MODELS = [
    {"id": REFERENCE_ID, "provider_id": "p1", "name": "big-model", "tier": 5, "context_window": 128000,
     "max_output_tokens": None, "supports_tools": True, "supports_json": True, "supports_stream": True,
     "cost_type": "per_token", "idle_threshold_pct": None, "enabled": True, "created_at": "2026-11-01T10:00:00Z"},
    {"id": TINY_ID, "provider_id": "p1", "name": "tiny-model", "tier": 1, "context_window": 8192,
     "max_output_tokens": None, "supports_tools": False, "supports_json": True, "supports_stream": True,
     "cost_type": "per_token", "idle_threshold_pct": None, "enabled": True, "created_at": "2026-11-01T10:00:00Z"},
]  # fmt: skip


def totals(**over: Any) -> dict[str, Any]:
    base = {"requests": 120, "ok": 118, "errors": 2, "prompt_tokens": 40000, "completion_tokens": 9000,
            "cost_usd": "1.2000", "fixed_cost_usd": "0", "effective_cost_usd": "1.2000",
            "reference_cost_usd": "9.6000", "savings_usd": "8.4000", "savings_pct": 87.5}  # fmt: skip
    return {**base, **over}


OVERVIEW: dict[str, Any] = {
    "month": "2026-11",
    "totals": totals(),
    "models": [
        {**totals(), "model_id": TINY_ID, "name": "tiny-model", "cost_type": "per_token", "share": 0.9,
         "avg_latency_ms": 420.0, "effective_cost_per_request_usd": "0.01"},
    ],
    "task_types": [{**totals(), "task_type": "classification"}],
    "keys": [
        {"key_id": "k1", "name": "prod", "monthly_budget_usd": "5.0000", "spent_usd": "4.5000",
         "counter_usd": "4.5000", "remaining_usd": "0.5000", "used_pct": 90.0, "exhausted": False},
    ],
    "problems": [{"kind": "idle_server", "subject": "llama-local", "detail": "busy only 3% of the time"}],
}  # fmt: skip

USAGE_DAY = {
    "group_by": "day", "since": "2026-11-01", "until": "2026-11-17",
    "rows": [{"day": "2026-11-16", "requests": 60, "ok": 59, "errors": 1, "prompt_tokens": 100,
              "completion_tokens": 50, "cost_usd": "0.6", "reference_cost_usd": "4.8", "avg_latency_ms": 400.0},
             {"day": "2026-11-17", "requests": 60, "ok": 59, "errors": 1, "prompt_tokens": 100,
              "completion_tokens": 50, "cost_usd": "0.6", "reference_cost_usd": "4.8", "avg_latency_ms": 410.0}],
}  # fmt: skip


def world(api: FakeApi, role: str = "admin") -> None:
    """The routes every logged-in page needs."""
    api.on("POST", "/auth/login", tokens())
    api.on("GET", "/auth/me", admin_user(role))
    api.on("POST", "/auth/logout")
    api.on("GET", "/projects", [PROJECT])
    api.on("GET", "/models", MODELS)
    api.on("GET", "/health/ready", {"status": "ok"})


@pytest.fixture
def api() -> FakeApi:
    fake = FakeApi()
    world(fake)
    return fake


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def settings() -> Settings:
    return Settings(cookie_secure=False, api_url="http://api", env="test")


@pytest.fixture
async def app(settings: Settings, redis: FakeRedis, api: FakeApi) -> AsyncIterator[FastAPI]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handle), base_url="http://api")
    application = create_app(settings, redis=redis, api_http=http)
    async with LifespanManager(application):
        yield application
    await http.aclose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://web", follow_redirects=False
    ) as c:
        yield c


def grab(pattern: str, text: str, flags: int = 0) -> str:
    """The first group of a regular expression; the test fails clearly when it is not there."""
    match = re.search(pattern, text, flags)
    assert match, f"pattern not found: {pattern}"
    return match.group(1)


def csrf_of(html: str) -> str:
    return grab(r'name="csrf_token" value="([^"]+)"', html)


async def sign_in(client: httpx.AsyncClient, next_path: str = "/overview") -> httpx.Response:
    """Open the form, fill it in, send it. Returns the answer to the POST."""
    form = await client.get("/login")
    assert form.status_code == 200
    return await client.post(
        "/login",
        data={
            "email": "admin@example.com",
            "password": "pw",
            "csrf_token": csrf_of(form.text),
            "next": next_path,
        },
    )


@pytest.fixture
async def session(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """A client that is logged in (as an admin)."""
    response = await sign_in(client)
    assert response.status_code == 303, response.text
    return client


def csrf_header(client: httpx.AsyncClient, redis: FakeRedis) -> dict[str, str]:
    """The CSRF token of the (only) logged-in session, for requests that change something."""
    sessions = [s for s in redis.sessions().values() if s.get("access_token")]
    assert len(sessions) == 1
    return {"x-csrf-token": sessions[0]["csrf"]}
