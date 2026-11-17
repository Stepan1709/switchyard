"""The gateway as a real process in front of a real (slow) mock provider.

Things that cannot be proven in-process: chunks really arrive one by one, a client that goes
away stops the provider's work, a SIGTERM lets a running stream finish.
"""

import asyncio
import json
import os
import signal
import sys
import time

import httpx
import pytest

from tests.integration.conftest import API_ROOT, MASTER_KEYS, MockServer, wait_until
from tests.integration.gateway_support import Provisioned, chat_body, provision
from tests.integration.test_auth_api import bearer, login
from tests.integration.test_worker import free_port

pytestmark = pytest.mark.integration


class GatewayProcess:
    def __init__(self, settings, **extra_env: str) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {
            **os.environ,
            "POSTGRES_HOST": settings.postgres_host,
            "POSTGRES_PORT": str(settings.postgres_port),
            "POSTGRES_DB": settings.postgres_db,
            "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
            "REDIS_HOST": settings.redis_host,
            "REDIS_PORT": str(settings.redis_port),
            "REDIS_PASSWORD": settings.redis_password.get_secret_value(),
            "KEY_PEPPER": settings.key_pepper.get_secret_value(),
            "MASTER_KEYS": MASTER_KEYS,
            "LOCAL_PROVIDER_HOSTS": "localhost,127.0.0.1",
            "FORWARD_HEADER_PREFIXES": "x-mock-",
            "GATEWAY_DEFAULT_RPM": "100000",
            **extra_env,
        }
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> "GatewayProcess":
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "uvicorn", "--factory", "app.gateway_main:create_app",
            "--host", "127.0.0.1", "--port", str(self.port),
            "--timeout-graceful-shutdown", "20", "--no-access-log",
            cwd=API_ROOT, env=self.env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )  # fmt: skip
        async with httpx.AsyncClient() as client:

            async def ready() -> bool:
                try:
                    return (await client.get(f"{self.url}/health/ready")).status_code == 200
                except httpx.TransportError:
                    return False

            await wait_until(ready, timeout=30)
        return self

    async def cleanup(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
async def slow_stack(client, admin, mock_llm_slow: MockServer) -> Provisioned:
    return await provision(client, admin, mock_llm_slow.base_url)


@pytest.fixture
async def gateway(app_settings, slow_stack):
    procs: list[GatewayProcess] = []

    async def make(**env: str) -> GatewayProcess:
        proc = GatewayProcess(app_settings, **env)
        procs.append(proc)
        return await proc.start()

    yield make
    for p in procs:
        await p.cleanup()


def mock_root(mock: MockServer) -> str:
    return mock.base_url.removesuffix("/v1")


async def stats(mock: MockServer) -> dict[str, int]:
    async with httpx.AsyncClient() as c:
        return (await c.get(f"{mock_root(mock)}/_stats")).json()


def stream_request(stack: Provisioned, tokens: int, **headers: str) -> dict:
    return {
        "json": chat_body("measure me", stream=True),
        "headers": {
            "authorization": f"Bearer {stack.key}",
            "x-mock-output-tokens": str(tokens),
            **headers,
        },
    }


async def test_chunks_arrive_one_by_one_not_all_at_the_end(gateway, slow_stack, mock_llm_slow):
    gw = await gateway()
    arrivals: list[float] = []
    text = ""
    async with httpx.AsyncClient(timeout=30) as client:
        started = time.monotonic()
        async with client.stream(
            "POST", f"{gw.url}/v1/chat/completions", **stream_request(slow_stack, 60)
        ) as r:
            assert r.status_code == 200
            async for line in r.aiter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    arrivals.append(time.monotonic() - started)
                    for c in json.loads(line[6:])["choices"]:
                        text += c["delta"].get("content", "")
    # mock-strong: ~0.3 s to the first token, then 50 tokens per second: a 60-token answer
    # takes over a second. A buffering proxy would deliver everything at the very end.
    assert len(arrivals) > 20  # many separate events, not one blob
    assert arrivals[0] < 1.0
    assert arrivals[-1] - arrivals[0] > 0.5  # a buffering proxy would show about 0 here
    assert text.split()[0] in {"positive", "negative", "neutral"}


async def test_a_client_that_leaves_stops_the_providers_work(gateway, slow_stack, mock_llm_slow):
    # one provider slot only: if it were not released after the disconnect, the second request
    # at the end of this test could never get through
    gw = await gateway(GATEWAY_PROVIDER_MAX_CONCURRENCY="1", GATEWAY_PROVIDER_QUEUE_TIMEOUT="2")
    before = await stats(mock_llm_slow)

    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream(
            "POST", f"{gw.url}/v1/chat/completions", **stream_request(slow_stack, 500)
        ) as r:  # ~10 s of generation at 50 tokens per second
            async for _ in r.aiter_raw():
                break  # read the first bytes ...
        # ... and leave: the context manager closes the connection
        left_at = time.monotonic()

        async def provider_stopped() -> bool:
            s = await stats(mock_llm_slow)
            return s["streams_aborted"] == before["streams_aborted"] + 1

        await wait_until(provider_stopped, timeout=5)
        assert time.monotonic() - left_at < 4  # far less than the 10 s the answer needed
        after = await stats(mock_llm_slow)
        assert after["streams_completed"] == before["streams_completed"]  # it never finished

        # the provider slot was given back
        ok = await client.post(
            f"{gw.url}/v1/chat/completions",
            json=chat_body("again"),
            headers={"authorization": f"Bearer {slow_stack.key}"},
        )
        assert ok.status_code == 200


async def test_a_provider_that_breaks_mid_stream_ends_the_stream_with_an_error_event(
    gateway, slow_stack
):
    gw = await gateway()
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream(
            "POST",
            f"{gw.url}/v1/chat/completions",
            **stream_request(slow_stack, 100, **{"x-mock-drop-after-chunks": "3"}),
        ) as r:
            body = (await r.aread()).decode()
        events = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
        assert "[DONE]" not in events  # the stream did not finish normally
        error = json.loads(events[-1])["error"]
        assert error["code"] == "upstream_error"
        # the gateway is still healthy afterwards
        again = await client.post(
            f"{gw.url}/v1/chat/completions",
            json=chat_body(),
            headers={"authorization": f"Bearer {slow_stack.key}"},
        )
        assert again.status_code == 200


async def test_sigterm_lets_a_running_stream_finish(gateway, slow_stack):
    gw = await gateway()
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream(
            "POST", f"{gw.url}/v1/chat/completions", **stream_request(slow_stack, 80)
        ) as r:
            lines = r.aiter_lines()
            await anext(lines)  # the stream is running
            started = time.monotonic()
            gw.proc.send_signal(signal.SIGTERM)
            rest = [line async for line in lines]
        assert "data: [DONE]" in rest  # the answer was delivered completely
        code = await asyncio.wait_for(gw.proc.wait(), 20)
        # uvicorn re-raises the received signal after a clean shutdown
        assert code in (0, -signal.SIGTERM) and time.monotonic() - started < 15
