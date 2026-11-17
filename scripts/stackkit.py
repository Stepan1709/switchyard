"""Helpers shared by the chaos and load scripts: talk to a running compose stack.

Run from WSL/Linux with the stack up (`make up`). Nothing here is imported by the application.
"""

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
API = os.environ.get("API_URL", "http://localhost:8001")
GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8000")
MOCK = os.environ.get("MOCK_URL", "http://localhost:8003")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
PASSWORD_FILE = Path(
    os.environ.get("ADMIN_PASSWORD_FILE", ROOT / "deploy/secrets/dev/admin_password")
)
COMPOSE_FILES = os.environ.get("COMPOSE_FILES", "deploy/compose.yml deploy/compose.dev.yml").split()


def compose(*args: str, check: bool = True, timeout: float = 180) -> str:
    """`docker compose ...` against the same files `make` uses."""
    cmd = ["docker", "compose", "--env-file", ".env"]
    for f in COMPOSE_FILES:
        cmd += ["-f", f]
    done = subprocess.run(
        [*cmd, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {done.stderr.strip()[-400:]}")
    return done.stdout


def psql(sql: str) -> str:
    """Run one statement in the database container and return the bare result."""
    out = compose(
        "exec", "-T", "postgres", "psql", "-U", "switchyard", "-d", "switchyard", "-At", "-c", sql
    )
    return out.strip()


def wait_for(predicate: Any, timeout: float, what: str, interval: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:  # the thing being waited for may be mid-restart
            pass
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}")


def wait_healthy(service: str, timeout: float = 90) -> None:
    def healthy() -> bool:
        out = compose("ps", "--format", "{{.Service}} {{.Status}}", service)
        return "(healthy)" in out

    wait_for(healthy, timeout, f"{service} to be healthy", 2.0)


@dataclass
class Provisioned:
    project_id: str
    key: str
    models: dict[str, str]  # name -> id


class Admin:
    """The admin API with a bearer token."""

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self.client = client
        self.headers = {"authorization": f"Bearer {token}"}

    @classmethod
    async def login(cls, client: httpx.AsyncClient) -> "Admin":
        password = PASSWORD_FILE.read_text().strip()
        r = await client.post(
            f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": password}
        )
        r.raise_for_status()
        return cls(client, r.json()["access_token"])

    async def call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        return await self.client.request(method, f"{API}{path}", headers=self.headers, **kw)

    async def find(self, path: str, name: str) -> dict[str, Any] | None:
        items = (await self.call("GET", path)).json()
        return next((i for i in items if i["name"] == name), None)

    async def provision(
        self,
        tag: str,
        *,
        store_bodies: bool = True,
        models: tuple[tuple[str, int, str, str], ...] = (
            ("mock-strong", 3, "5", "15"),
            ("mock-tiny", 1, "0.2", "0.6"),
        ),
        reference: str = "mock-strong",
    ) -> Provisioned:
        """A provider (the mock), its models, a project with a reference model and a fresh
        key. Idempotent by name, so a rerun reuses what exists."""
        provider = await self.find("/providers", f"{tag}-mock")
        if provider is None:
            r = await self.call(
                "POST",
                "/providers",
                json={
                    "name": f"{tag}-mock",
                    "base_url": "http://mock_llm:8003/v1",
                    "auth_type": "none",
                    "is_local": True,
                    # Calibration replays are paced per provider; do not let the default 5 req/s
                    # be what the measurements show.
                    "calibration_max_concurrency": 32,
                    "calibration_max_rps": "1000",
                },
            )
            r.raise_for_status()
            provider = r.json()
        ids: dict[str, str] = {}
        existing = (await self.call("GET", f"/models?provider_id={provider['id']}")).json()
        for name, tier, cin, cout in models:
            found = next((m for m in existing if m["name"] == name), None)
            if found is None:
                r = await self.call(
                    "POST",
                    "/models",
                    json={
                        "provider_id": provider["id"],
                        "name": name,
                        "tier": tier,
                        "context_window": 128000,
                        "supports_stream": True,
                    },
                )
                r.raise_for_status()
                found = r.json()
                (
                    await self.call(
                        "POST",
                        f"/models/{found['id']}/prices",
                        json={"input_per_1m": cin, "output_per_1m": cout},
                    )
                ).raise_for_status()
            ids[name] = found["id"]
        project = await self.find("/projects", tag)
        if project is None:
            r = await self.call("POST", "/projects", json={"name": tag})
            r.raise_for_status()
            project = r.json()
        pid = project["id"]
        (
            await self.call(
                "PUT", f"/projects/{pid}/reference-model", json={"model_id": ids[reference]}
            )
        ).raise_for_status()
        (
            await self.call("PATCH", f"/projects/{pid}", json={"store_bodies": store_bodies})
        ).raise_for_status()
        key = await self.call(
            "POST",
            f"/projects/{pid}/keys",
            json={"name": f"{tag}-{int(time.time())}", "rate_limit_rpm": 1_000_000},
        )
        key.raise_for_status()
        secret = key.json()["key"]
        await wait_for_key(self.client, secret)
        return Provisioned(pid, secret, ids)


async def wait_for_key(client: httpx.AsyncClient, key: str) -> None:
    """The gateway learns a new key with its next snapshot refresh (up to ~15 s)."""
    for _ in range(60):
        r = await client.post(
            f"{GATEWAY}/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "ping"}]},
            headers={"authorization": f"Bearer {key}", "x-task-type": "warmup"},
        )
        if r.status_code == 200:
            return
        await asyncio.sleep(1)
    raise TimeoutError("the gateway did not accept the new key")


def metric(text: str, name: str, labels: str = "") -> float:
    """One sample from Prometheus text (sums the series that match `labels` as a substring)."""
    total = 0.0
    for line in text.splitlines():
        if line.startswith(name) and not line.startswith("#"):
            head, _, value = line.rpartition(" ")
            if (head.startswith(name + "{") or head == name) and labels in head:
                total += float(value)
    return total


async def gateway_metrics(client: httpx.AsyncClient) -> str:
    return (await client.get(f"{GATEWAY}/metrics")).text


def chat_body(text: str = "Review: great product. Labels: a, b, c", **extra: Any) -> dict[str, Any]:
    return {"model": "auto", "messages": [{"role": "user", "content": text}], **extra}


def pretty(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)
