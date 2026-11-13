"""SIGTERM must let an in-flight request finish, close resources and exit quickly."""

import asyncio
import os
import signal
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import Endpoint

pytestmark = pytest.mark.integration

API_ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_sigterm_finishes_inflight_request_and_exits_fast(
    pg_ep: Endpoint, redis_ep: Endpoint, rabbit_ep: Endpoint
):
    port = free_port()
    env = {
        **os.environ,
        "POSTGRES_HOST": pg_ep.host,
        "POSTGRES_PORT": str(pg_ep.port),
        "POSTGRES_PASSWORD": "test-password",
        "REDIS_HOST": redis_ep.host,
        "REDIS_PORT": str(redis_ep.port),
        "REDIS_PASSWORD": "test-password",
        "RABBITMQ_HOST": rabbit_ep.host,
        "RABBITMQ_PORT": str(rabbit_ep.port),
        "RABBITMQ_PASSWORD": "test-password",
        "JWT_SECRET": "j" * 40,
        "KEY_PEPPER": "p" * 40,
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uvicorn", "tests.integration.slow_app:app",
        "--host", "127.0.0.1", "--port", str(port),
        "--timeout-graceful-shutdown", "20", "--no-access-log",
        cwd=API_ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for _ in range(100):  # wait until the server is ready
                try:
                    if (await client.get(f"{base}/health/ready")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.2)
            else:
                pytest.fail("server did not become ready")

            slow = asyncio.create_task(client.get(f"{base}/slow"))
            await asyncio.sleep(0.7)  # the request is now in flight
            started = time.monotonic()
            proc.send_signal(signal.SIGTERM)

            await asyncio.sleep(0.5)
            with pytest.raises(httpx.TransportError):  # no new connections after SIGTERM
                async with httpx.AsyncClient(timeout=2) as fresh:
                    await fresh.get(f"{base}/health/live")

            response = await slow
            assert response.status_code == 200 and response.json() == {"done": True}

        code = await asyncio.wait_for(proc.wait(), timeout=10)
        # uvicorn re-raises the received signal after a clean shutdown, so the status is
        # either 0 or "killed by SIGTERM"; what matters is that it happened without SIGKILL.
        assert code in (0, -signal.SIGTERM)
        assert time.monotonic() - started < 8
        output = (await proc.stdout.read()).decode() if proc.stdout else ""
        assert "resources_closed" in output
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
