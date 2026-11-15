"""Shared setup for the gateway tests: provision data through the admin API, run a gateway."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from app.core.config import Settings
from app.gateway_main import create_app as create_gateway_app
from app.modules.gateway.record import RequestRecord
from tests.integration.conftest import MOCK_KEY


class CollectingRecorder:
    """Keeps the records instead of logging them."""

    def __init__(self) -> None:
        self.records: list[RequestRecord] = []

    def submit(self, record: RequestRecord) -> None:
        self.records.append(record)

    @property
    def last(self) -> RequestRecord:
        return self.records[-1]


@dataclass
class Provisioned:
    """Everything an administrator would have set up, plus a working client key."""

    key: str
    key_id: str
    project: dict[str, Any]
    provider: dict[str, Any]
    model: dict[str, Any] | None
    admin_headers: dict[str, str]
    extra: dict[str, Any] = field(default_factory=dict)


async def provision(
    api: httpx.AsyncClient,
    admin_headers: dict[str, str],
    base_url: str,
    *,
    project: str = "shop",
    provider: str = "mock",
    model: str | None = "mock-strong",
    context_window: int = 128_000,
    reference: bool = True,
    provider_key: str = MOCK_KEY,
    key_settings: dict[str, Any] | None = None,
) -> Provisioned:
    p = await api.post(
        "/providers",
        json={
            "name": provider,
            "base_url": base_url,
            "is_local": True,
            "auth_type": "bearer",
            "api_key": provider_key,
        },
        headers=admin_headers,
    )
    assert p.status_code == 201, p.text
    m = None
    if model:
        r = await api.post(
            "/models",
            json={"provider_id": p.json()["id"], "name": model, "context_window": context_window},
            headers=admin_headers,
        )
        assert r.status_code == 201, r.text
        m = r.json()
    proj = await api.post("/projects", json={"name": project}, headers=admin_headers)
    assert proj.status_code == 201, proj.text
    if m and reference:
        ref = await api.put(
            f"/projects/{proj.json()['id']}/reference-model",
            json={"model_id": m["id"]},
            headers=admin_headers,
        )
        assert ref.status_code == 200, ref.text
    k = await api.post(
        f"/projects/{proj.json()['id']}/keys",
        json={"name": "app", **(key_settings or {})},
        headers=admin_headers,
    )
    assert k.status_code == 201, k.text
    return Provisioned(
        key=k.json()["key"],
        key_id=k.json()["id"],
        project=proj.json(),
        provider=p.json(),
        model=m,
        admin_headers=admin_headers,
    )


@dataclass
class GatewayHandle:
    app: FastAPI
    client: httpx.AsyncClient
    recorder: CollectingRecorder | None

    async def refresh(self) -> None:
        """Rebuild the snapshot now instead of waiting for the periodic refresh."""
        await self.app.state.proxy.store.refresh()

    def auth(self, key: str) -> dict[str, str]:
        return {"authorization": f"Bearer {key}"}


async def run_gateway(
    settings: Settings, *, collect: bool = True, **overrides: Any
) -> AsyncIterator[GatewayHandle]:
    """Run a gateway. With collect=False it uses the real recorder (logs and traffic events)."""
    recorder = CollectingRecorder() if collect else None
    updates = {
        "service_name": "gateway",
        "forward_header_prefixes": ["x-mock-"],
        "gateway_default_rpm": 1000,
        **overrides,
    }
    app = create_gateway_app(settings.model_copy(update=updates), recorder=recorder)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            yield GatewayHandle(app, client, recorder)


def chat_body(text: str = "great product", **extra: Any) -> dict[str, Any]:
    return {"model": "gpt-4o", "messages": [{"role": "user", "content": text}], **extra}
