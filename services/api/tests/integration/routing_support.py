"""Setup shared by the routing tests: models, rules through the real admin API, the mock's
counters."""

import asyncio
from typing import Any

import httpx

from app.core.outbox import OutboxRelay
from tests.integration.calibration_support import Env


async def rule_ids(env: Env, *names: str) -> list[str]:
    """Model ids for names such as "tiny", "mid", "ref" (the reference)."""
    return [env.reference_id if n == "ref" else env.candidates[n] for n in names]


async def propose(client, env: Env, *chain: str, note: str | None = None) -> dict[str, Any]:
    r = await client.post(
        f"/projects/{env.project_id}/task-types/classification/rules",
        json={"chain": await rule_ids(env, *chain), "note": note},
        headers=env.admin,
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def activate(client, env: Env, rule: dict[str, Any], expected: int | None = None, **headers):
    return await client.post(
        f"/projects/{env.project_id}/rules/{rule['id']}/activate",
        json={"expected_active_version": expected},
        headers={**env.admin, **headers},
    )


async def rollback(client, env: Env, **headers):
    return await client.post(
        f"/projects/{env.project_id}/task-types/classification/rules/rollback",
        headers={**env.admin, **headers},
    )


async def set_mode(client, env: Env, mode: str, **extra: Any):
    return await client.patch(
        f"/projects/{env.project_id}/task-types/classification",
        json={"mode": mode, **extra},
        headers=env.admin,
    )


async def go_live(client, env: Env, *chain: str, expected: int | None = None) -> dict[str, Any]:
    """Propose, activate and switch the task type to route mode."""
    rule = await propose(client, env, *chain)
    r = await activate(client, env, rule, expected)
    assert r.status_code == 200, r.text
    mode = await set_mode(client, env, "route")
    assert mode.status_code == 200, mode.text
    body: dict[str, Any] = r.json()
    return body


async def mock_calls(mock_base_url: str) -> dict[str, int]:
    """How many calls each model of the mock provider has received so far."""
    async with httpx.AsyncClient() as http:
        r = await http.get(mock_base_url.removesuffix("/v1") + "/_stats")
    stats: dict[str, Any] = r.json()
    models: dict[str, int] = stats["models"]
    return models


class BackgroundRelay:
    """The outbox relay of the worker, running inside the test."""

    def __init__(self, api_app, publisher) -> None:
        self._relay = OutboxRelay(
            api_app.state.resources.session_factory, publisher, poll_interval=0.05
        )
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "BackgroundRelay":
        self._task = asyncio.create_task(self._relay.run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._relay.stop()
        if self._task is not None:
            await self._task
