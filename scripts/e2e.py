"""The whole path of the product on a running stack, through the public interfaces:

    make up && make e2e

project and key -> traffic recorded on the reference -> calibration -> recommendation -> rule
proposed and activated -> traffic routed to the cheaper model -> savings visible in the
analytics. The web interface and the metrics are checked on the way.
"""

import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx

from stackkit import (
    API,
    GATEWAY,
    Admin,
    chat_body,
    psql,
    wait_for,
)

WEB = "http://localhost:8002"
TASK = "classification"


def step(text: str) -> None:
    print(f"- {text}", flush=True)


async def send(client: httpx.AsyncClient, key: str, n: int, tag: str) -> list[httpx.Response]:
    """n requests with different prompts (the calibration sample drops duplicates)."""
    sem = asyncio.Semaphore(8)

    async def one(i: int) -> httpx.Response:
        async with sem:
            return await client.post(
                f"{GATEWAY}/v1/chat/completions",
                json=chat_body(f"Review {tag}-{i}: it works as described. Labels: a, b, c"),
                headers={"authorization": f"Bearer {key}", "x-task-type": TASK},
            )

    return await asyncio.gather(*[one(i) for i in range(n)])


async def main() -> int:
    async with httpx.AsyncClient(timeout=60) as client:
        # 0. the services are up and the interface answers
        for name, url in (("api", f"{API}/health/ready"), ("gateway", f"{GATEWAY}/health/ready")):
            r = await client.get(url)
            assert r.status_code == 200, f"{name} is not ready: {r.status_code}"
        web = await client.get(f"{WEB}/login")
        assert web.status_code == 200 and "Sign in" in web.text, "the web interface does not answer"
        step("api, gateway and web answer")

        admin = await Admin.login(client)
        name = f"e2e-{int(time.time())}"
        p = await admin.provision(name)
        pid = p.project_id
        step(f"project {name} with a reference model and a key")

        # 1. record: everything goes to the reference model
        sent = await send(client, p.key, 80, "record")
        assert all(r.status_code == 200 for r in sent), [r.status_code for r in sent if r.status_code != 200]
        assert {r.headers["x-switchyard-model"] for r in sent} == {"mock-strong"}
        assert {r.headers["x-switchyard-mode"] for r in sent} == {"record"}
        wait_for(
            lambda: int(psql(f"SELECT count(*) FROM requests WHERE project_id = '{pid}' AND task_type = '{TASK}'")) >= 80,
            120, "the traffic log to hold 80 requests", 2.0,
        )  # fmt: skip
        step("80 requests recorded on the reference model")

        # 2. calibrate: replay on the cheaper model, check the answers against the reference
        check = await admin.call("POST", f"/projects/{pid}/task-types/{TASK}/checks", json={"kind": "exact_match"})
        check.raise_for_status()
        created = await admin.call(
            "POST",
            f"/projects/{pid}/calibration-runs",
            json={
                "task_type": TASK,
                "candidate_model_ids": [p.models["mock-tiny"]],
                "sample_size": 60,
                "seed": 5,
                "thresholds": {"min_pass_rate": 0.4},
            },
            headers={"idempotency-key": name},
        )
        created.raise_for_status()
        run = created.json()["id"]
        path = f"/projects/{pid}/calibration-runs/{run}"

        async def finished() -> bool:
            s = (await admin.call("GET", path)).json()
            return s["status"] == "completed" and s["evaluation"]["status"] == "done"

        deadline = time.monotonic() + 240
        while not await finished():
            assert time.monotonic() < deadline, "the calibration did not finish"
            await asyncio.sleep(2)
        step("calibration finished and evaluated")

        # 3. recommend, propose, activate (a person's decision), switch the mode
        rec = (await admin.call("GET", f"{path}/recommendation")).json()
        assert rec["primary"] == p.models["mock-tiny"], rec["notes"]
        proposed = await admin.call("POST", f"{path}/recommendation/propose", json={"note": "e2e"})
        proposed.raise_for_status()
        rule = proposed.json()
        assert rule["status"] == "proposed"
        activated = await admin.call(
            "POST", f"/projects/{pid}/rules/{rule['id']}/activate", json={"expected_active_version": None}
        )
        activated.raise_for_status()
        mode = await admin.call("PATCH", f"/projects/{pid}/task-types/{TASK}", json={"mode": "route"})
        mode.raise_for_status()
        step("the recommendation was proposed and activated; the task type routes")

        # 4. route: the gateway applies the rule without a restart
        async def routed() -> bool:
            r = (await send(client, p.key, 1, "probe"))[0]
            return r.headers.get("x-switchyard-mode") == "route" and r.headers.get("x-switchyard-model") == "mock-tiny"

        deadline = time.monotonic() + 60
        while not await routed():
            assert time.monotonic() < deadline, "the gateway did not pick up the rule"
            await asyncio.sleep(2)
        after = await send(client, p.key, 50, "route")
        assert all(r.status_code == 200 for r in after)
        assert {r.headers["x-switchyard-model"] for r in after} == {"mock-tiny"}
        step("traffic is now answered by the cheaper model")

        # 5. savings are visible
        wait_for(
            lambda: int(psql(f"SELECT count(*) FROM requests WHERE project_id = '{pid}' AND model_id = '{p.models['mock-tiny']}'")) >= 50,
            120, "the routed requests to reach the log", 2.0,
        )  # fmt: skip
        today = datetime.now(UTC).date()
        recompute = await admin.call(
            "POST",
            f"/projects/{pid}/analytics/recompute",
            json={"since": (today - timedelta(days=1)).isoformat(), "until": today.isoformat()},
        )
        recompute.raise_for_status()
        overview = (await admin.call("GET", f"/projects/{pid}/analytics/overview")).json()
        totals = overview["totals"]
        assert totals["requests"] >= 130, totals
        assert float(totals["savings_usd"]) > 0, totals
        step(f"overview: {totals['requests']} requests, savings ${totals['savings_usd']} ({totals['savings_pct']}%)")

        # 6. the metrics endpoints answer
        for service, url in (("gateway", GATEWAY), ("api", API)):
            text = (await client.get(f"{url}/metrics")).text
            assert "switchyard_http_requests_total" in text or "switchyard_gateway_requests_total" in text, service
        step("metrics are exposed")
    print("\nE2E PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except AssertionError as exc:
        print(f"\nE2E FAILED: {exc}")
        sys.exit(1)
