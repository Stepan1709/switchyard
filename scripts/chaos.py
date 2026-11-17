"""Failure scenarios against a running stack: break a part, keep traffic flowing, check what
was promised.

    make up
    make chaos                    # all scenarios
    make chaos SCENARIOS=redis    # a part of them (names are matched as substrings)

What is promised (and checked):
- a client is never left without an answer because a dependency is down (Postgres, Redis,
  RabbitMQ): the gateway serves from its snapshot;
- every answered request reaches the traffic log after recovery, except events the gateway
  says it dropped (its buffer overflowed), which are counted in `log_events_dropped_total`;
- a worker killed in the middle of a calibration loses no job and duplicates no result;
- a provider that fails or hangs is bypassed by the next model of the rule.
"""

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
from stackkit import (
    API,
    GATEWAY,
    Admin,
    Provisioned,
    chat_body,
    compose,
    gateway_metrics,
    metric,
    psql,
    wait_for,
    wait_healthy,
)

RATE = float(os.environ.get("CHAOS_RATE", "20"))  # requests per second from the client


@dataclass
class Outcome:
    ok_ids: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    models: dict[str, int] = field(default_factory=dict)


class ClientLoad:
    """A steady stream of chat requests from one client, collecting what came back."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        p: Provisioned,
        task: str,
        rate: float | None = None,
        **extra: object,
    ) -> None:
        self.client, self.p, self.task, self.extra = client, p, task, extra
        self.rate = rate or RATE
        self.out = Outcome()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._pending: set[asyncio.Task[None]] = set()

    async def _one(self, n: int) -> None:
        headers = {"authorization": f"Bearer {self.p.key}", "x-task-type": self.task}
        headers.update({k: str(v) for k, v in self.extra.items() if k.startswith("x-")})
        try:
            r = await self.client.post(
                f"{GATEWAY}/v1/chat/completions",
                json=chat_body(f"Review {n}: it works. Labels: a, b, c"),
                headers=headers,
                timeout=120,
            )
        except httpx.HTTPError as exc:
            self.out.failures.append(type(exc).__name__)
            return
        if r.status_code == 200:
            self.out.ok_ids.append(r.headers["x-request-id"])
            model = r.headers.get("x-switchyard-model", "?")
            self.out.models[model] = self.out.models.get(model, 0) + 1
        else:
            self.out.failures.append(f"http {r.status_code}")

    async def _run(self) -> None:
        n, interval = 0, 1.0 / self.rate
        next_at = time.monotonic()
        while not self._stop.is_set():
            task = asyncio.create_task(self._one(n))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
            n += 1
            next_at += interval
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> Outcome:
        self._stop.set()
        if self._task is not None:
            await self._task
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        return self.out


@dataclass
class Ctx:
    client: httpx.AsyncClient
    admin: Admin
    p: Provisioned


Scenario = Callable[[Ctx], Awaitable[list[str]]]
RESULTS: list[tuple[str, bool, list[str]]] = []


def rows_since(project_id: str, since: str) -> int:
    return int(
        psql(
            f"SELECT count(*) FROM requests WHERE project_id = '{project_id}' AND started_at >= '{since}'"
        )
    )


def db_now() -> str:
    return psql("SELECT now()")


def settle(ctx: Ctx, since: str, expected: int, timeout: float = 150) -> int:
    """Wait until the traffic log holds the expected number of rows (the events catch up)."""
    wait_for(
        lambda: rows_since(ctx.p.project_id, since) >= expected, timeout, "the log to catch up", 2.0
    )
    return rows_since(ctx.p.project_id, since)


async def dropped(ctx: Ctx) -> float:
    return metric(await gateway_metrics(ctx.client), "switchyard_log_events_dropped_total")


# ---- scenarios ------------------------------------------------------------------------------
async def postgres_down(ctx: Ctx) -> list[str]:
    """The data plane has no database on its path: it answers from the snapshot."""
    since, notes = db_now(), []
    load = ClientLoad(ctx.client, ctx.p, "chaos")
    load.start()
    await asyncio.sleep(4)
    compose("stop", "postgres")
    await asyncio.sleep(15)
    ready = (await ctx.client.get(f"{GATEWAY}/health/ready")).status_code
    compose("start", "postgres")
    wait_healthy("postgres")
    await asyncio.sleep(3)
    out = await load.stop()
    notes.append(
        f"{len(out.ok_ids)} answered, {len(out.failures)} failed during a 15 s outage; ready={ready}"
    )
    caught = settle(ctx, since, len(out.ok_ids))
    notes.append(f"log rows after recovery: {caught} of {len(out.ok_ids)}")
    assert not out.failures, out.failures[:3]
    assert ready == 200
    assert caught >= len(out.ok_ids)
    return notes


async def rabbitmq_restart(ctx: Ctx) -> list[str]:
    """Events wait in the gateway's buffer while the broker is away and are sent afterwards."""
    since, notes = db_now(), []
    before = await dropped(ctx)
    load = ClientLoad(ctx.client, ctx.p, "chaos")
    load.start()
    await asyncio.sleep(4)
    compose("restart", "rabbitmq", timeout=120)
    wait_healthy("rabbitmq")
    await asyncio.sleep(8)
    out = await load.stop()
    lost = int(await dropped(ctx) - before)
    notes.append(
        f"{len(out.ok_ids)} answered, {len(out.failures)} failed; events dropped by the gateway: {lost}"
    )
    caught = settle(ctx, since, len(out.ok_ids) - lost)
    notes.append(f"log rows after recovery: {caught} of {len(out.ok_ids)} (dropped {lost})")
    assert not out.failures, out.failures[:3]
    assert caught >= len(out.ok_ids) - lost  # every missing row is a counted drop
    return notes


async def rabbitmq_down_long(ctx: Ctx) -> list[str]:
    """An outage longer than the gateway's buffer: the oldest events are dropped, and every
    drop is counted. Clients are still answered, and nothing else goes missing."""
    since, notes = db_now(), []
    before = await dropped(ctx)
    load = ClientLoad(ctx.client, ctx.p, "chaos", rate=120)
    load.start()
    await asyncio.sleep(3)
    compose("stop", "rabbitmq", timeout=120)
    await asyncio.sleep(30)
    compose("start", "rabbitmq", timeout=120)
    wait_healthy("rabbitmq")
    await asyncio.sleep(8)
    out = await load.stop()
    lost = int(await dropped(ctx) - before)
    notes.append(
        f"{len(out.ok_ids)} answered, {len(out.failures)} failed; events dropped by the gateway: {lost}"
    )
    caught = settle(ctx, since, len(out.ok_ids) - lost)
    notes.append(f"log rows after recovery: {caught} of {len(out.ok_ids)} (dropped {lost})")
    assert not out.failures, out.failures[:3]
    assert lost > 0, "the outage was meant to overflow the buffer"
    assert caught >= len(out.ok_ids) - lost  # every missing row is a counted drop
    return notes


async def redis_down(ctx: Ctx) -> list[str]:
    """Without Redis limits and budgets stop being enforced, but traffic still flows."""
    notes = []
    load = ClientLoad(ctx.client, ctx.p, "chaos")
    load.start()
    await asyncio.sleep(3)
    compose("stop", "redis")
    await asyncio.sleep(12)
    compose("start", "redis")
    wait_healthy("redis")
    await asyncio.sleep(3)
    out = await load.stop()
    notes.append(f"{len(out.ok_ids)} answered, {len(out.failures)} failed during a 12 s outage")
    assert not out.failures, out.failures[:3]
    # The admin API must work at once, too: its Redis connections died with the restart.
    again = await Admin.login(ctx.client)
    listed = await again.call("GET", "/projects")
    notes.append(f"admin api right after the restart: login ok, GET /projects {listed.status_code}")
    assert listed.status_code == 200
    return notes


async def worker_killed_in_a_calibration(ctx: Ctx) -> list[str]:
    """SIGKILL the worker mid-run: its unacknowledged jobs go to the next delivery, and a job
    that ran twice still stores one result (unique key)."""
    notes = []
    seed = ClientLoad(ctx.client, ctx.p, "calib")
    seed.start()
    await asyncio.sleep(8)  # recorded traffic to replay
    await seed.stop()
    created = await ctx.admin.call(
        "POST",
        f"/projects/{ctx.p.project_id}/calibration-runs",
        json={
            "task_type": "calib",
            "candidate_model_ids": [ctx.p.models["mock-tiny"]],
            "sample_size": 120,
            "seed": 7,
        },
    )
    created.raise_for_status()
    run = created.json()["id"]
    path = f"/projects/{ctx.p.project_id}/calibration-runs/{run}"

    async def state() -> dict:
        return (await ctx.admin.call("GET", path)).json()

    for _ in range(60):
        s = await state()
        if s["progress"] and s["progress"]["finished"] >= 10:
            break
        await asyncio.sleep(0.5)
    done_before_kill = (await state())["progress"]["finished"]
    compose("kill", "worker")
    await asyncio.sleep(4)
    compose("start", "worker")
    wait_healthy("worker")
    for _ in range(180):
        s = await state()
        if s["status"] == "completed":
            break
        await asyncio.sleep(1)
    s = await state()
    total = s["progress"]["total_jobs"]
    rows = int(psql(f"SELECT count(*) FROM run_results WHERE run_id = '{run}'"))
    distinct = int(
        psql(
            f"SELECT count(DISTINCT (request_id, model_id)) FROM run_results WHERE run_id = '{run}'"
        )
    )
    notes.append(
        f"killed after {done_before_kill} of {total} jobs; results {rows}, distinct {distinct}, status {s['status']}"
    )
    assert s["status"] == "completed", s["status"]
    assert rows == total == distinct
    return notes


async def a_failing_or_hanging_provider(ctx: Ctx) -> list[str]:
    """The rule is tiny first, strong as the fallback. tiny answers 503, then hangs."""
    notes = []
    warm = ClientLoad(ctx.client, ctx.p, "chaos")
    warm.start()
    await asyncio.sleep(2)
    await warm.stop()
    tasks = f"/projects/{ctx.p.project_id}/task-types"
    chain = [ctx.p.models["mock-tiny"], ctx.p.models["mock-strong"]]
    rules = (await ctx.admin.call("GET", f"{tasks}/chaos/rules")).json()
    active = next((r for r in rules if r["status"] == "active"), None)
    if active is None or active["chain"] != chain:  # a rerun finds the rule already live
        rule = await ctx.admin.call(
            "POST", f"{tasks}/chaos/rules", json={"chain": chain, "note": "chaos"}
        )
        rule.raise_for_status()
        activated = await ctx.admin.call(
            "POST",
            f"/projects/{ctx.p.project_id}/rules/{rule.json()['id']}/activate",
            json={"expected_active_version": active["version"] if active else None},
        )
        activated.raise_for_status()
    (await ctx.admin.call("PATCH", f"{tasks}/chaos", json={"mode": "route"})).raise_for_status()
    await asyncio.sleep(18)  # the gateway's snapshot refresh

    fail = ClientLoad(
        ctx.client, ctx.p, "chaos", **{"x-mock-fail": "503", "x-mock-fail-models": "mock-tiny"}
    )
    fail.start()
    await asyncio.sleep(10)
    out = await fail.stop()
    notes.append(
        f"503 from the first model: {len(out.ok_ids)} answered by {out.models}, {len(out.failures)} failed"
    )
    assert not out.failures and out.models.keys() == {"mock-strong"}, (out.failures[:3], out.models)

    # A hang is only noticed when the first-byte timeout (30 s) runs out, so a few requests.
    async def hung() -> httpx.Response:
        return await ctx.client.post(
            f"{GATEWAY}/v1/chat/completions",
            json=chat_body(stream=True),
            headers={"authorization": f"Bearer {ctx.p.key}", "x-task-type": "chaos",
                     "x-mock-fail": "timeout", "x-mock-fail-models": "mock-tiny"},
            timeout=120,
        )  # fmt: skip

    started = time.monotonic()
    answers = await asyncio.gather(*[hung() for _ in range(3)])
    took = time.monotonic() - started
    models = {a.headers.get("x-switchyard-model") for a in answers}
    notes.append(
        f"hanging first model: statuses {[a.status_code for a in answers]}, answered by {models} after {took:.0f} s"
    )
    assert all(a.status_code == 200 for a in answers) and models == {"mock-strong"}
    return notes


SCENARIOS: dict[str, Scenario] = {
    "postgres": postgres_down,
    "rabbitmq": rabbitmq_restart,
    "rabbitmq_long": rabbitmq_down_long,
    "redis": redis_down,
    "worker": worker_killed_in_a_calibration,
    "provider": a_failing_or_hanging_provider,
}


async def main() -> int:
    wanted = [w for w in os.environ.get("SCENARIOS", "").split() if w]
    async with httpx.AsyncClient(timeout=30) as client:
        admin = await Admin.login(client)
        p = await admin.provision("chaos")
        ctx = Ctx(client, admin, p)
        for name, scenario in SCENARIOS.items():
            if wanted and not any(w in name for w in wanted):
                continue
            print(f"\n== {name}", flush=True)
            try:
                notes = await scenario(ctx)
                RESULTS.append((name, True, notes))
            except Exception as exc:
                RESULTS.append((name, False, [f"{type(exc).__name__}: {exc}"]))
            finally:
                # Whatever failed, leave the stack as it was found.
                for service in ("postgres", "redis", "rabbitmq", "worker"):
                    compose("start", service, check=False)
            for line in RESULTS[-1][2]:
                print("   " + line, flush=True)
    print("\n== summary")
    for name, ok, _ in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"api={API} gateway={GATEWAY}")
    return 0 if all(ok for _, ok, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
