"""Load measurements against a running stack; prints (and saves) the table used in the README.

    make load        # starts the stack with small-VPS limits and the instant mock, then runs this

Phases: the gateway's overhead against the provider alone, the most it carries, what storing
request bodies costs, hundreds of long-lived streams, and how fast a calibration runs.
The load generator runs on the same machine as the stack, so the numbers are conservative.
"""

import asyncio
import csv
import os
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import httpx
from stackkit import (
    GATEWAY,
    MOCK,
    ROOT,
    Admin,
    chat_body,
    compose,
    wait_for_key,
    gateway_metrics,
    metric,
    psql,
)

RESULTS = ROOT / "loadtest" / "results"
LOCUSTFILE = ROOT / "loadtest" / "locustfile.py"
SECONDS = int(os.environ.get("LOAD_SECONDS", "20"))


@dataclass
class Stat:
    name: str
    requests: int
    failures: int
    rps: float
    p50: float
    p95: float
    p99: float
    mean: float

    @property
    def fail_pct(self) -> float:
        return 100 * self.failures / self.requests if self.requests else 0.0


def locust(
    user_class: str, users: int, seconds: int, key: str, tag: str, **env: str
) -> dict[str, Stat]:
    """One headless Locust run; returns its rows by request name."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    prefix = RESULTS / tag
    cmd = [
        sys.executable, "-m", "locust", "-f", str(LOCUSTFILE), "--headless",
        "-u", str(users), "-r", str(max(users, 1)), "-t", f"{seconds}s",
        "--csv", str(prefix), "--only-summary", user_class,
    ]  # fmt: skip
    done = subprocess.run(
        cmd,
        env={**os.environ, "LOAD_KEY": key, "GATEWAY_URL": GATEWAY, "MOCK_URL": MOCK, **env},
        capture_output=True,
        text=True,
        timeout=seconds + 120,
        check=False,
    )
    if done.returncode not in (0, 1):  # 1 = some requests failed (reported in the table)
        raise RuntimeError(done.stderr[-600:])
    stats: dict[str, Stat] = {}
    with open(f"{prefix}_stats.csv", newline="") as f:
        for row in csv.DictReader(f):
            if row["Name"] == "Aggregated":
                continue
            stats[row["Name"]] = Stat(
                row["Name"],
                int(row["Request Count"]),
                int(row["Failure Count"]),
                float(row["Requests/s"]),
                float(row["50%"]),
                float(row["95%"]),
                float(row["99%"]),
                float(row["Average Response Time"]),
            )
    return stats


@dataclass
class Sampler:
    """Polls the gateway container's CPU and memory and its in-flight gauge while a phase runs."""

    container: str
    cpu: list[float] = field(default_factory=list)
    mem_mb: list[float] = field(default_factory=list)
    in_flight: list[float] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemUsage}}", self.container],
                    capture_output=True, text=True, timeout=15, check=False,
                ).stdout.split()  # fmt: skip
                if out:
                    self.cpu.append(float(out[0].rstrip("%")))
                    self.mem_mb.append(_mb(out[1]))
                text = httpx.get(f"{GATEWAY}/metrics", timeout=3).text
                self.in_flight.append(metric(text, "switchyard_gateway_in_flight_requests"))
            except Exception:  # a missed sample is fine
                pass
            self._stop.wait(1.0)

    def __enter__(self) -> "Sampler":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=20)

    def summary(self) -> str:
        if not self.cpu:
            return "n/a"
        return f"CPU avg {statistics.mean(self.cpu):.0f}% max {max(self.cpu):.0f}%, memory max {max(self.mem_mb):.0f} MB"


def _mb(text: str) -> float:
    m = re.match(r"([\d.]+)\s*([KMG]i?B)", text)
    if not m:
        return 0.0
    value, unit = float(m.group(1)), m.group(2)[0]
    return value * {"K": 1 / 1024, "M": 1.0, "G": 1024.0}[unit]


def gateway_container() -> str:
    return compose("ps", "-q", "gateway").strip().splitlines()[0]


def fmt(s: Stat) -> str:
    return f"{s.rps:,.0f} | {s.p50:.0f} | {s.p95:.0f} | {s.p99:.0f} | {s.fail_pct:.2f}%"


async def restart_gateway(client: httpx.AsyncClient, key: str, *, log_requests: bool) -> None:
    """Recreate the gateway with the request log on or off and wait until it serves again."""
    os.environ["GATEWAY_LOG_REQUESTS"] = "true" if log_requests else "false"
    compose("up", "-d", "--wait", "gateway", timeout=240)
    await wait_for_key(client, key)


async def set_bodies(admin: Admin, project_id: str, on: bool) -> None:
    (
        await admin.call("PATCH", f"/projects/{project_id}", json={"store_bodies": on})
    ).raise_for_status()
    await asyncio.sleep(20)  # the gateway learns it with its next snapshot refresh


async def wait_for_log_drain(project_id: str) -> tuple[int, float]:
    """After the load the worker is still writing the traffic log. Wait until the row count stops
    growing; returns (rows written meanwhile, seconds), the log's own write speed."""
    started = time.monotonic()
    first = last = int(psql(f"SELECT count(*) FROM requests WHERE project_id = '{project_id}'"))
    steady = 0
    while time.monotonic() - started < 600 and steady < 3:
        await asyncio.sleep(3)
        now = int(psql(f"SELECT count(*) FROM requests WHERE project_id = '{project_id}'"))
        steady = steady + 1 if now == last else 0
        last = now
    return last - first, time.monotonic() - started - 9


async def seed_distinct(client: httpx.AsyncClient, key: str, n: int) -> None:
    """Record n requests with different prompts: the sample drops duplicates, and the load
    phases send the same prompt every time."""
    sem = asyncio.Semaphore(16)

    async def one(i: int) -> None:
        async with sem:
            r = await client.post(
                f"{GATEWAY}/v1/chat/completions",
                json=chat_body(f"Review number {i}: the product was fine. Labels: a, b, c"),
                headers={"authorization": f"Bearer {key}", "x-task-type": "calib"},
            )
            r.raise_for_status()

    await asyncio.gather(*[one(i) for i in range(n)])


async def calibration_speed(admin: Admin, project_id: str, tiny: str) -> tuple[int, float]:
    """Replay recorded requests on one model; returns (jobs, seconds)."""
    recorded = int(
        psql(
            f"SELECT count(*) FROM requests WHERE project_id = '{project_id}' AND task_type = 'calib'"
        )
    )
    size = min(300, recorded)
    created = await admin.call(
        "POST",
        f"/projects/{project_id}/calibration-runs",
        json={"task_type": "calib", "candidate_model_ids": [tiny], "sample_size": size, "seed": 1},
    )
    created.raise_for_status()
    path = f"/projects/{project_id}/calibration-runs/{created.json()['id']}"
    started = time.monotonic()
    while time.monotonic() - started < 600:
        s = (await admin.call("GET", path)).json()
        if s["status"] == "completed":
            return int(s["progress"]["total_jobs"]), time.monotonic() - started
        await asyncio.sleep(0.5)
    raise TimeoutError("the calibration did not finish")


async def main() -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        admin = await Admin.login(client)
        p = await admin.provision("load", store_bodies=True)
        print(
            f"project {p.project_id}; limits: gateway 1 CPU / 512 MB, worker 1 CPU / 512 MB",
            flush=True,
        )
        container = gateway_container()
        lines: list[str] = []

        # 1. overhead: the same request, with and without the gateway
        lines += ["### Gateway overhead (instant mock; latencies in ms)", "",
                  "| Users | Path | req/s | p50 | p95 | p99 | failed |", "|---|---|---|---|---|---|---|"]  # fmt: skip
        for users in (1, 16):
            direct = locust("DirectUser", users, SECONDS, p.key, f"direct-{users}")["direct"]
            via = locust("GatewayUser", users, SECONDS, p.key, f"gateway-{users}")["gateway"]
            lines.append(f"| {users} | mock directly | {fmt(direct)} |")
            lines.append(f"| {users} | through the gateway | {fmt(via)} |")
            lines.append(
                f"| {users} | **added by the gateway** | | {via.p50 - direct.p50:+.0f} | {via.p95 - direct.p95:+.0f} | {via.p99 - direct.p99:+.0f} | |"
            )
            print(f"overhead @ {users}: p50 {via.p50 - direct.p50:+.0f} ms", flush=True)

        # 2. how much it carries
        lines += ["", "### Throughput through the gateway", "",
                  "| Users | req/s | p50 | p95 | p99 | failed | gateway container |", "|---|---|---|---|---|---|---|"]  # fmt: skip
        best = (0.0, 0)
        for users in (8, 16, 32, 64, 128):
            with Sampler(container) as sampler:
                s = locust("GatewayUser", users, SECONDS, p.key, f"max-{users}")["gateway"]
            lines.append(f"| {users} | {fmt(s)} | {sampler.summary()} |")
            if s.fail_pct == 0 and s.rps > best[0]:
                best = (s.rps, users)
            print(
                f"{users} users: {s.rps:.0f} req/s p99 {s.p99:.0f} ms failed {s.fail_pct:.2f}%",
                flush=True,
            )
        lines.append(
            f"\nMost without a failed request: **{best[0]:,.0f} req/s** at {best[1]} users."
        )

        # 2b. what the per-request log line costs (one JSON line per request through Docker)
        lines += ["", "### Cost of a log line per request (32 users)", "",
                  "| Request log | req/s | p50 | p95 | p99 | failed |", "|---|---|---|---|---|---|"]  # fmt: skip
        for on in (True, False):
            await restart_gateway(client, p.key, log_requests=on)
            s = locust("GatewayUser", 32, SECONDS, p.key, f"log-{on}")["gateway"]
            lines.append(f"| {'on (default)' if on else 'off'} | {fmt(s)} |")
        await restart_gateway(client, p.key, log_requests=True)

        # 3. what storing the bodies costs (the traffic event carries them)
        lines += ["", "### Cost of the traffic log (32 users)", "",
                  "| Bodies | req/s | p50 | p95 | p99 | failed |", "|---|---|---|---|---|---|"]  # fmt: skip
        for on in (True, False):
            await set_bodies(admin, p.project_id, on)
            s = locust("GatewayUser", 32, SECONDS, p.key, f"bodies-{on}")["gateway"]
            lines.append(f"| {'stored' if on else 'not stored'} | {fmt(s)} |")
        await set_bodies(admin, p.project_id, True)

        # 4. hundreds of long-lived streams
        before = await gateway_metrics(client)
        with Sampler(container) as sampler:
            rows = locust("StreamUser", 300, 30, p.key, "streams")
        after = await gateway_metrics(client)
        st = rows["stream"]
        lines += ["", "### 300 concurrent streams (each held open about 3 s)", "",
                  "| Streams completed | failed | stream duration p50 / p95 (ms) | peak in flight | gateway container |", "|---|---|---|---|---|",
                  f"| {st.requests - st.failures} | {st.failures} | {st.p50:.0f} / {st.p95:.0f} | {max(sampler.in_flight or [0]):.0f} | {sampler.summary()} |"]  # fmt: skip
        dropped = metric(after, "switchyard_log_events_dropped_total") - metric(
            before, "switchyard_log_events_dropped_total"
        )
        lines.append(f"\nTraffic events dropped during the streams: {dropped:.0f}.")

        # 5. calibration speed
        await seed_distinct(client, p.key, 400)
        written, drain = await wait_for_log_drain(p.project_id)
        lines += ["", "### Traffic log catch-up", "",
                  f"After the load the worker still had {written:,} events to write: {max(drain, 0.1):.0f} s, "
                  f"about {written / max(drain, 0.1):,.0f} rows/s (one worker, 1 CPU)."]  # fmt: skip
        tiny = p.models["mock-tiny"]
        jobs, seconds = await calibration_speed(admin, p.project_id, tiny)
        lines += ["", "### Calibration speed (one worker, 1 CPU, instant mock)", "",
                  f"{jobs} replays in {seconds:.1f} s = **{jobs / seconds * 60:,.0f} replays per minute**."]  # fmt: skip

        host = f"{platform.system()} {platform.release()}, {os.cpu_count()} logical CPUs"
        report = "\n".join(
            [f"Measured {time.strftime('%Y-%m-%d')} on {host}. Load generator on the same machine. "
             f"Containers: gateway and worker 1 CPU / 512 MB, mock provider 1 CPU.", "", *lines]
        )  # fmt: skip
        out = RESULTS / "latest.md"
        out.write_text(report + "\n", encoding="utf-8")
        print("\n" + report)
        print(f"\nsaved to {out}")


if __name__ == "__main__":
    asyncio.run(main())
