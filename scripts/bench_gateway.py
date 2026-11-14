"""Measure the overhead of the gateway: the same request sent straight to the mock provider and
through the gateway.

Run against a running stack whose mock answers instantly, so that what remains is the cost of
the proxy itself:

    MOCK_TIME_SCALE=0 make up
    make bench

It sets up its own project, key and reference model through the admin API (idempotent by
name), then prints a Markdown table. Both paths go over localhost, so the difference between
the two columns is the gateway's contribution: authentication, rate limit, body handling,
the extra HTTP hop to the provider and the answer's way back.
"""

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

API = os.environ.get("API_URL", "http://localhost:8001")
GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8000")
MOCK = os.environ.get("MOCK_URL", "http://localhost:8003")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
_ROOT = Path(__file__).resolve().parent.parent
PASSWORD_FILE = Path(
    os.environ.get("ADMIN_PASSWORD_FILE", _ROOT / "deploy/secrets/dev/admin_password")
)
SEQUENTIAL = int(os.environ.get("BENCH_SEQUENTIAL", "300"))
CONCURRENT = int(os.environ.get("BENCH_CONCURRENT", "2000"))
CONCURRENCY = int(os.environ.get("BENCH_CONCURRENCY", "32"))

BODY = {
    "model": "gpt-4o",
    "messages": [
        {"role": "user", "content": "Review: great product, would buy again. Labels: a, b, c"}
    ],
}


async def setup(client: httpx.AsyncClient) -> str:
    password = PASSWORD_FILE.read_text().strip()
    login = await client.post(
        f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": password}
    )
    login.raise_for_status()
    headers = {"authorization": f"Bearer {login.json()['access_token']}"}

    async def find(path: str, name: str) -> dict:
        items = (await client.get(f"{API}{path}", headers=headers)).json()
        return next(i for i in items if i["name"] == name)

    prov = await client.post(
        f"{API}/providers",
        json={
            "name": "bench-mock",
            "base_url": "http://mock_llm:8003/v1",
            "auth_type": "none",
            "is_local": True,
        },
        headers=headers,
    )
    provider = prov.json() if prov.status_code == 201 else await find("/providers", "bench-mock")
    model_resp = await client.post(
        f"{API}/models",
        json={"provider_id": provider["id"], "name": "mock-strong", "context_window": 128000},
        headers=headers,
    )
    if model_resp.status_code == 201:
        model = model_resp.json()
    else:
        models = (
            await client.get(f"{API}/models?provider_id={provider['id']}", headers=headers)
        ).json()
        model = next(m for m in models if m["name"] == "mock-strong")
    proj = await client.post(f"{API}/projects", json={"name": "bench"}, headers=headers)
    project = proj.json() if proj.status_code == 201 else await find("/projects", "bench")
    await client.put(
        f"{API}/projects/{project['id']}/reference-model",
        json={"model_id": model["id"]},
        headers=headers,
    )
    key = await client.post(
        f"{API}/projects/{project['id']}/keys",
        json={"name": f"bench-{int(time.time())}", "rate_limit_rpm": 1_000_000},
        headers=headers,
    )
    key.raise_for_status()
    return str(key.json()["key"])


async def wait_for_key(client: httpx.AsyncClient, key: str) -> None:
    """The gateway learns new keys with its next snapshot refresh (up to ~15 s)."""
    for _ in range(60):
        r = await client.post(
            f"{GATEWAY}/v1/chat/completions", json=BODY, headers={"authorization": f"Bearer {key}"}
        )
        if r.status_code == 200:
            return
        await asyncio.sleep(1)
    raise SystemExit("the gateway did not pick up the new key; is it running?")


def percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def at(p: float) -> float:
        return ordered[min(int(len(ordered) * p), len(ordered) - 1)]

    return {"p50": at(0.50), "p95": at(0.95), "p99": at(0.99), "mean": statistics.fmean(values)}


async def timed(client: httpx.AsyncClient, url: str, headers: dict, body: dict) -> float:
    started = time.perf_counter()
    r = await client.post(url, json=body, headers=headers)
    r.raise_for_status()
    return (time.perf_counter() - started) * 1000


async def sequential(client, url, headers, body, n) -> list[float]:
    for _ in range(20):  # warm up connections and caches
        await timed(client, url, headers, body)
    return [await timed(client, url, headers, body) for _ in range(n)]


async def concurrent(client, url, headers, body, n, workers) -> tuple[list[float], float]:
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(n):
        queue.put_nowait(i)
    latencies: list[float] = []

    async def worker() -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            latencies.append(await timed(client, url, headers, body))

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(workers)))
    return latencies, n / (time.perf_counter() - started)


async def stream_times(client, url, headers, body, n) -> tuple[list[float], list[float]]:
    first, total = [], []
    for _ in range(n):
        started = time.perf_counter()
        first_at = None
        async with client.stream("POST", url, json={**body, "stream": True}, headers=headers) as r:
            async for chunk in r.aiter_raw():
                if first_at is None and chunk:
                    first_at = (time.perf_counter() - started) * 1000
        first.append(first_at or 0.0)
        total.append((time.perf_counter() - started) * 1000)
    return first, total


def row(name: str, p: dict[str, float]) -> str:
    return f"| {name} | {p['p50']:.2f} | {p['p95']:.2f} | {p['p99']:.2f} | {p['mean']:.2f} |"


async def main() -> None:
    limits = httpx.Limits(
        max_connections=CONCURRENCY * 2, max_keepalive_connections=CONCURRENCY * 2
    )
    async with httpx.AsyncClient(timeout=60, limits=limits) as client:
        key = await setup(client)
        await wait_for_key(client, key)
        gw_headers = {"authorization": f"Bearer {key}"}
        direct_url = f"{MOCK}/v1/chat/completions"
        gw_url = f"{GATEWAY}/v1/chat/completions"
        direct_body = {**BODY, "model": "mock-strong"}

        d = percentiles(await sequential(client, direct_url, {}, direct_body, SEQUENTIAL))
        g = percentiles(await sequential(client, gw_url, gw_headers, BODY, SEQUENTIAL))
        dl, d_rps = await concurrent(client, direct_url, {}, direct_body, CONCURRENT, CONCURRENCY)
        gl, g_rps = await concurrent(client, gw_url, gw_headers, BODY, CONCURRENT, CONCURRENCY)
        dc, gc = percentiles(dl), percentiles(gl)
        df, dt = await stream_times(client, f"{MOCK}/v1/chat/completions", {}, direct_body, 100)
        gf, gt = await stream_times(client, gw_url, gw_headers, BODY, 100)

    print(
        f"\n### Gateway overhead (non-streaming, {SEQUENTIAL} sequential requests, latency in ms)\n"
    )
    print("| path | p50 | p95 | p99 | mean |\n|---|---|---|---|---|")
    print(row("direct to mock", d))
    print(row("through gateway", g))
    print(
        f"| **overhead** | **{g['p50'] - d['p50']:.2f}** | **{g['p95'] - d['p95']:.2f}** |"
        f" **{g['p99'] - d['p99']:.2f}** | **{g['mean'] - d['mean']:.2f}** |"
    )
    print(f"\n### Under load ({CONCURRENT} requests, {CONCURRENCY} concurrent)\n")
    print("| path | req/s | p50 | p95 | p99 | mean |\n|---|---|---|---|---|---|")
    print(
        f"| direct to mock | {d_rps:.0f} | {dc['p50']:.2f} | {dc['p95']:.2f} | {dc['p99']:.2f} | {dc['mean']:.2f} |"
    )
    print(
        f"| through gateway | {g_rps:.0f} | {gc['p50']:.2f} | {gc['p95']:.2f} | {gc['p99']:.2f} | {gc['mean']:.2f} |"
    )
    print("\n### Streaming (100 sequential streams, ms)\n")
    print(
        "| path | first byte p50 | first byte p95 | complete p50 | complete p95 |\n|---|---|---|---|---|"
    )
    for name, f, t in (("direct to mock", df, dt), ("through gateway", gf, gt)):
        pf, pt = percentiles(f), percentiles(t)
        print(f"| {name} | {pf['p50']:.2f} | {pf['p95']:.2f} | {pt['p50']:.2f} | {pt['p95']:.2f} |")
    print(json.dumps({"direct_rps": round(d_rps), "gateway_rps": round(g_rps)}), file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
