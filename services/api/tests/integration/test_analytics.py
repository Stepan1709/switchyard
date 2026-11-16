"""Reports: daily summaries, savings, the load of own servers, the signal on a rule."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.worker.runtime import _periodic
from tests.integration.calibration_support import Env, add_model, build_env, set_price, sql
from tests.integration.conftest import MockServer, wait_until
from tests.integration.routing_support import go_live
from tests.integration.test_auth_api import GOOD_PASSWORD, bearer, login, make_admin
from tests.integration.test_traffic_api import insert_rows

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)
TODAY = NOW.replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
async def env(client, admin, api_app, mock_llm: MockServer) -> Env:
    env = await build_env(client, admin, api_app, mock_llm.base_url, requests=0)
    await set_price(client, admin, env.reference_id, "3.00", "15.00")
    return env


def request(env, model, at, **kw):
    base = {
        "task_type": "classification",
        "model_id": uuid.UUID(model),
        "started_at": at,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "duration_ms": 40.0,
        "cost_usd": Decimal("0.001"),
        "reference_cost_usd": Decimal("0.01"),
    }
    return {**base, **kw}


async def recompute(client, env, since, until=None):
    r = await client.post(
        f"/projects/{env.project_id}/analytics/recompute",
        json={"since": since.date().isoformat(), "until": (until or NOW).date().isoformat()},
        headers=env.admin,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def usage_rows(api_app):
    rows = await sql(
        api_app,
        "SELECT day, project_id, model_id, task_type, requests, ok, errors, prompt_tokens,"
        " completion_tokens, cost_usd, reference_cost_usd, duration_sum_ms, latency_hist"
        " FROM usage_daily ORDER BY day, model_id, task_type",
    )
    return [tuple(r) for r in rows]


# ---- the daily summaries ----------------------------------------------------------------------
async def test_the_summaries_follow_the_log_and_computing_them_again_changes_nothing(
    client, env, api_app
):
    tiny, mid = env.candidates["tiny"], env.candidates["mid"]
    day1, day2 = TODAY - timedelta(days=2), TODAY - timedelta(days=1)
    await insert_rows(
        api_app,
        env.project_id,
        [
            *(request(env, tiny, day1 + timedelta(hours=h), duration_ms=d)
              for h, d in ((1, 30.0), (2, 80.0), (3, 300.0), (4, 40.0))),
            request(env, tiny, day1 + timedelta(hours=5), status="error", prompt_tokens=None,
                    completion_tokens=None, cost_usd=None, reference_cost_usd=None, duration_ms=200.0),
            request(env, mid, day2 + timedelta(hours=1), task_type="extraction", duration_ms=1500.0),
            request(env, mid, day2 + timedelta(hours=2), task_type="extraction", duration_ms=600.0),
        ],
    )  # fmt: skip
    await recompute(client, env, day1)
    first = await usage_rows(api_app)
    assert len(first) == 2  # (day1, tiny, classification) and (day2, mid, extraction)

    a = next(r for r in first if str(r.model_id if hasattr(r, "model_id") else r[2]) == tiny)
    _, _, _, task, n, ok, errors, pt, ct, cost, ref, dur, hist = a
    assert (task, n, ok, errors) == ("classification", 5, 4, 1)
    assert (pt, ct) == (400, 200)  # tokens of the failed request are unknown: counted as none
    assert cost == Decimal("0.004") and ref == Decimal("0.04")
    assert dur == pytest.approx(30 + 80 + 300 + 40 + 200)
    # cumulative buckets: how many requests took at most this long
    assert (hist["le_50"], hist["le_100"], hist["le_250"], hist["le_500"], hist["total"]) == (
        2,
        3,
        4,
        5,
        5,
    )

    await recompute(client, env, day1)  # again, and for a longer range
    await recompute(client, env, day1 - timedelta(days=5))
    assert await usage_rows(api_app) == first  # nothing changed

    await insert_rows(api_app, env.project_id, [request(env, tiny, day1 + timedelta(hours=6))])
    await recompute(client, env, day1)
    changed = await usage_rows(api_app)
    assert next(r for r in changed if r[2] == uuid.UUID(tiny))[4] == 6  # the new request is counted

    await sql(api_app, "DELETE FROM requests")  # the month of the log is dropped by retention
    await recompute(client, env, day1)
    assert await usage_rows(api_app) == changed  # the summary outlives the log


async def test_a_request_without_a_model_still_counts_under_no_model(client, env, api_app):
    await insert_rows(
        api_app, env.project_id,
        [{"model_id": None, "status": "error", "started_at": TODAY + timedelta(hours=1)}],
    )  # fmt: skip
    await recompute(client, env, TODAY)
    (row,) = await usage_rows(api_app)
    assert row[2] == uuid.UUID(int=0) and row[4:7] == (1, 0, 1)


async def test_usage_can_be_read_by_day_model_and_task_type(client, env, api_app):
    tiny, mid = env.candidates["tiny"], env.candidates["mid"]
    yesterday = TODAY - timedelta(days=1)
    await insert_rows(
        api_app,
        env.project_id,
        [
            request(env, tiny, yesterday + timedelta(hours=1)),
            request(env, tiny, TODAY + timedelta(hours=1)),
            request(env, mid, TODAY + timedelta(hours=2), task_type="extraction"),
        ],
    )
    await recompute(client, env, yesterday)
    base = f"/projects/{env.project_id}/analytics/usage"

    by_day = (await client.get(base, params={"group_by": "day"}, headers=env.admin)).json()
    assert [(r["day"], r["requests"]) for r in by_day["rows"]] == [
        (yesterday.date().isoformat(), 1),
        (TODAY.date().isoformat(), 2),
    ]
    by_model = (await client.get(base, params={"group_by": "model"}, headers=env.admin)).json()
    assert {r["model"]: r["requests"] for r in by_model["rows"]} == {"mock-tiny": 2, "mock-mid": 1}
    by_task = (await client.get(base, params={"group_by": "task_type"}, headers=env.admin)).json()
    assert {r["task_type"]: r["requests"] for r in by_task["rows"]} == {
        "classification": 2,
        "extraction": 1,
    }
    assert by_day["rows"][0]["avg_latency_ms"] == 40.0
    narrow = await client.get(
        base, params={"since": TODAY.date().isoformat(), "until": TODAY.date().isoformat()},
        headers=env.admin,
    )  # fmt: skip
    assert [r["requests"] for r in narrow.json()["rows"]] == [2]
    assert (
        await client.get(
            base, params={"since": "2026-02-01", "until": "2026-01-01"}, headers=env.admin
        )
    ).status_code == 422
    assert (
        await client.get(base, params={"group_by": "color"}, headers=env.admin)
    ).status_code == 422


async def test_recomputing_is_limited_to_a_reasonable_range(client, env):
    url = f"/projects/{env.project_id}/analytics/recompute"
    far = await client.post(
        url, json={"since": (TODAY - timedelta(days=200)).date().isoformat()}, headers=env.admin
    )
    assert far.status_code == 422
    backwards = await client.post(
        url, json={"since": TODAY.date().isoformat(), "until": (TODAY - timedelta(days=1)).date().isoformat()},
        headers=env.admin,
    )  # fmt: skip
    assert backwards.status_code == 422


# ---- the month: costs and savings -------------------------------------------------------------
async def test_the_month_shows_requests_by_model_costs_and_savings_with_a_servers_share(
    client, admin, env, api_app
):
    tiny = env.candidates["tiny"]
    server = await add_model(
        client, admin, env.stack.provider["id"], "mock-judge", cost_type="fixed_monthly"
    )
    price = await client.post(
        f"/models/{server}/prices", json={"monthly_cost_usd": "10.00"}, headers=admin
    )
    assert price.status_code == 201, price.text
    other = (await client.post("/projects", json={"name": "other"}, headers=admin)).json()

    at = TODAY + timedelta(hours=1)
    mine = [
        *(request(env, tiny, at + timedelta(minutes=i)) for i in range(10)),
        *(request(env, server, at + timedelta(minutes=i), cost_usd=Decimal(0),
                  reference_cost_usd=Decimal("1.0")) for i in range(30)),
    ]  # fmt: skip
    await insert_rows(api_app, env.project_id, mine)
    await insert_rows(  # another project uses the same server: it shares the monthly price
        api_app, other["id"],
        [request(env, server, at + timedelta(minutes=i), cost_usd=Decimal(0),
                 reference_cost_usd=Decimal("1.0")) for i in range(60)],
    )  # fmt: skip
    await recompute(client, env, TODAY)

    r = await client.get(f"/projects/{env.project_id}/analytics/overview", headers=env.admin)
    assert r.status_code == 200, r.text
    body = r.json()
    by_name = {m["name"]: m for m in body["models"]}
    t = body["totals"]
    assert t["requests"] == 40 and t["ok"] == 40
    assert Decimal(t["cost_usd"]) == Decimal("0.01")
    # the server is paid by the month: this project's 30 of its 90 requests carry a third
    assert Decimal(t["fixed_cost_usd"]).quantize(Decimal("0.0001")) == Decimal("3.3333")
    assert Decimal(t["effective_cost_usd"]).quantize(Decimal("0.0001")) == Decimal("3.3433")
    assert Decimal(t["reference_cost_usd"]) == Decimal("30.1")
    assert Decimal(t["savings_usd"]).quantize(Decimal("0.01")) == Decimal("26.76")
    assert t["savings_pct"] == pytest.approx(88.9, abs=0.1)

    assert by_name["mock-tiny"]["share"] == 0.25 and by_name["mock-judge"]["share"] == 0.75
    assert by_name["mock-judge"]["cost_type"] == "fixed_monthly"
    per_request = Decimal(by_name["mock-judge"]["effective_cost_per_request_usd"])
    assert per_request.quantize(Decimal("0.0001")) == Decimal("0.1111")  # 10 / 90 requests
    assert [tt["task_type"] for tt in body["task_types"]] == ["classification"]


async def test_a_server_that_costs_more_than_it_saves_shows_negative_savings(
    client, admin, env, api_app
):
    server = await add_model(
        client, admin, env.stack.provider["id"], "mock-judge", cost_type="fixed_monthly"
    )
    await client.post(
        f"/models/{server}/prices", json={"monthly_cost_usd": "500.00"}, headers=admin
    )
    await insert_rows(
        api_app, env.project_id,
        [request(env, server, TODAY + timedelta(minutes=i), cost_usd=Decimal(0)) for i in range(5)],
    )  # fmt: skip
    await recompute(client, env, TODAY)
    body = (
        await client.get(f"/projects/{env.project_id}/analytics/overview", headers=env.admin)
    ).json()
    assert Decimal(body["totals"]["savings_usd"]) < 0  # honest: the whole price on 5 requests


async def test_an_empty_month_and_bad_months(client, env):
    base = f"/projects/{env.project_id}/analytics/overview"
    body = (await client.get(base, params={"month": "2020-01"}, headers=env.admin)).json()
    assert (
        body["totals"]["requests"] == 0
        and body["models"] == []
        and body["totals"]["savings_pct"] is None
    )
    for bad in ("2020-13", "2020-1", "january"):
        assert (await client.get(base, params={"month": bad}, headers=env.admin)).status_code == 422


# ---- the load of own servers -----------------------------------------------------------------
async def own_server(client, admin, env, monthly="300.00", name="mock-judge", **extra):
    server = await add_model(
        client, admin, env.stack.provider["id"], name, cost_type="fixed_monthly", **extra
    )
    await client.post(f"/models/{server}/prices", json={"monthly_cost_usd": monthly}, headers=admin)
    return server


def find(report, server):
    return next(s for s in report["servers"] if s["model_id"] == server)


async def test_parallel_requests_are_not_added_up_and_idle_servers_are_marked(
    client, admin, env, api_app
):
    server = await own_server(client, admin, env)
    start = TODAY + timedelta(hours=1)
    # four requests at once for ten seconds, then one for five seconds a minute later
    await insert_rows(
        api_app, env.project_id,
        [request(env, server, start, duration_ms=10_000.0) for _ in range(4)]
        + [request(env, server, start + timedelta(seconds=60), duration_ms=5_000.0)],
    )  # fmt: skip
    await recompute(client, env, TODAY)
    report = (await client.get("/analytics/servers", headers=env.admin)).json()
    s = find(report, server)
    assert s["busy_seconds"] == pytest.approx(15.0)  # not 45
    assert s["busy_by_source_seconds"]["live"] == pytest.approx(15.0)
    assert s["requests"] == 5 and 0.0 <= s["utilization_pct"] <= 100.0
    assert s["idle"] is True  # 15 seconds of a day or more is far below 10%
    assert s["idle_threshold_pct"] == 10.0
    month_price = Decimal("300.00")
    idle_cost = Decimal(s["idle_cost_usd"])
    assert (
        Decimal(0)
        < idle_cost
        <= month_price * Decimal(s["window_seconds"]) / Decimal(30 * 86400) + Decimal("0.01")
    )
    assert Decimal(s["effective_cost_per_request_usd"]) == Decimal("60")  # 300 / 5


async def test_the_load_adds_the_copies_of_shadow_mode_and_keeps_the_sources_apart(
    client, admin, env, api_app
):
    server = await own_server(client, admin, env)
    start = TODAY + timedelta(hours=2)
    await insert_rows(api_app, env.project_id, [request(env, server, start, duration_ms=10_000.0)])
    # a copy that ran from 5 s to 15 s after the start: it overlaps the live request by 5 s
    ends = start + timedelta(seconds=15)
    await sql(
        api_app,
        "INSERT INTO shadow_results (id, request_id, request_started_at, project_id, model_id, status,"
        " latency_ms, created_at) VALUES (gen_random_uuid(), gen_random_uuid(), :s,"
        " CAST(:p AS uuid), CAST(:m AS uuid), 'ok', 10000, :e)",
        s=start, p=env.project_id, m=server, e=ends,
    )  # fmt: skip
    await recompute(client, env, TODAY)
    s = find((await client.get("/analytics/servers", headers=env.admin)).json(), server)
    assert s["busy_seconds"] == pytest.approx(15.0)  # the union, not 20
    assert s["busy_by_source_seconds"] == pytest.approx(
        {"live": 10.0, "calibration": 0.0, "shadow": 10.0}
    )


async def test_a_server_above_its_threshold_is_not_idle_and_the_threshold_is_per_model(
    client, admin, env, api_app
):
    server = await own_server(client, admin, env, idle_threshold_pct="0.00")
    await insert_rows(
        api_app,
        env.project_id,
        [request(env, server, TODAY + timedelta(hours=1), duration_ms=1000.0)],
    )
    await recompute(client, env, TODAY)
    s = find((await client.get("/analytics/servers", headers=env.admin)).json(), server)
    assert s["idle_threshold_pct"] == 0.0 and s["idle"] is False  # busy more than 0%
    await client.patch(f"/models/{server}", json={"idle_threshold_pct": "50.00"}, headers=admin)
    s = find((await client.get("/analytics/servers", headers=env.admin)).json(), server)
    assert s["idle_threshold_pct"] == 50.0 and s["idle"] is True


async def test_a_server_nobody_used_is_idle_and_costs_its_share_of_the_month(client, admin, env):
    server = await own_server(client, admin, env, monthly="3000.00", name="mock-judge")
    s = find((await client.get("/analytics/servers", headers=env.admin)).json(), server)
    assert s["busy_seconds"] == 0 and s["utilization_pct"] == 0 and s["idle"] is True
    assert Decimal(s["idle_cost_usd"]) > 0 and s["effective_cost_per_request_usd"] is None


async def test_only_servers_paid_by_the_month_are_reported(client, env):
    report = (await client.get("/analytics/servers", headers=env.admin)).json()
    assert report["servers"] == []  # the models of the stack are all paid per token
    assert (
        await client.get("/analytics/servers", params={"month": "nope"}, headers=env.admin)
    ).status_code == 422


async def test_an_idle_server_shows_among_the_problems_of_the_month(client, admin, env, api_app):
    server = await own_server(client, admin, env)
    await insert_rows(
        api_app,
        env.project_id,
        [request(env, server, TODAY + timedelta(hours=1), duration_ms=100.0)],
    )
    await recompute(client, env, TODAY)
    body = (
        await client.get(f"/projects/{env.project_id}/analytics/overview", headers=env.admin)
    ).json()
    assert [(p["kind"], p["subject"]) for p in body["problems"]] == [("idle_server", "mock-judge")]


# ---- feedback and the rule ---------------------------------------------------------------------
async def rate(api_app, env, model, n_good, n_bad, at, task="classification"):
    for i in range(n_good + n_bad):
        await sql(
            api_app,
            "INSERT INTO feedback (id, request_id, project_id, request_started_at, model_id, task_type,"
            " rating) VALUES (gen_random_uuid(), gen_random_uuid(), CAST(:p AS uuid), :t,"
            " CAST(:m AS uuid), :task, :r)",
            p=env.project_id, t=at + timedelta(seconds=i), m=model, task=task,
            r=1 if i < n_good else -1,
        )  # fmt: skip


async def activate_rule(client, env, api_app, days_ago=5):
    await go_live(client, env, "tiny", "ref")
    when = NOW - timedelta(days=days_ago)
    await sql(api_app, "UPDATE routing_rules SET activated_at = :t WHERE status = 'active'", t=when)
    return when


async def test_a_rise_of_dislikes_after_a_rule_raises_the_signal_to_roll_it_back(
    client, env, api_app
):
    when = await activate_rule(client, env, api_app)
    await rate(api_app, env, env.reference_id, 95, 5, when - timedelta(days=10))  # 5% before
    await rate(api_app, env, env.candidates["tiny"], 14, 26, when + timedelta(hours=1))  # 65% now
    url = f"/projects/{env.project_id}/task-types/classification/rule-health"
    body = (await client.get(url, headers=env.admin)).json()
    assert body["suggest_rollback"] is True and body["rule_version"] == 1
    assert (body["ratings_since"], body["dislikes_since"]) == (40, 26)
    assert body["dislike_rate_before"] == pytest.approx(0.05)
    assert "rose from 5% to 65%" in body["reason"]

    overview = (
        await client.get(f"/projects/{env.project_id}/analytics/overview", headers=env.admin)
    ).json()
    assert ("dislikes_rose", "classification") in [
        (p["kind"], p["subject"]) for p in overview["problems"]
    ]


async def test_a_few_or_ordinary_ratings_do_not_raise_the_signal(client, env, api_app):
    when = await activate_rule(client, env, api_app)
    await rate(api_app, env, env.reference_id, 90, 10, when - timedelta(days=10))  # 10% before
    url = f"/projects/{env.project_id}/task-types/classification/rule-health"

    await rate(api_app, env, env.candidates["tiny"], 2, 8, when + timedelta(hours=1))
    few = (await client.get(url, headers=env.admin)).json()
    assert few["suggest_rollback"] is False and "not enough" in few["reason"]

    await rate(api_app, env, env.candidates["tiny"], 70, 8, when + timedelta(hours=2))  # ~ 11% now
    normal = (await client.get(url, headers=env.admin)).json()
    assert normal["ratings_since"] == 88 and normal["suggest_rollback"] is False


async def test_ratings_of_answers_before_the_rule_or_of_other_models_are_not_held_against_it(
    client, admin, env, api_app
):
    when = await activate_rule(client, env, api_app)
    await rate(
        api_app, env, env.candidates["mid"], 0, 50, when + timedelta(hours=1)
    )  # not in the rule
    await rate(api_app, env, env.candidates["tiny"], 0, 50, when - timedelta(days=1))  # before it
    body = (
        await client.get(
            f"/projects/{env.project_id}/task-types/classification/rule-health", headers=env.admin
        )
    ).json()
    assert body["ratings_since"] == 0 and body["suggest_rollback"] is False


async def test_a_task_type_without_an_active_rule_has_no_health(client, env):
    r = await client.get(
        f"/projects/{env.project_id}/task-types/classification/rule-health", headers=env.admin
    )
    assert r.status_code == 409


async def test_dislikes_by_model_and_task_type_with_their_comments(client, env, api_app):
    tiny, mid = env.candidates["tiny"], env.candidates["mid"]
    at = NOW - timedelta(days=1)
    await rate(api_app, env, tiny, 6, 4, at)
    await rate(api_app, env, mid, 9, 1, at)
    await rate(api_app, env, tiny, 3, 0, at, task="extraction")
    await sql(
        api_app,
        "UPDATE feedback SET comment = 'wrong label' WHERE rating = -1 AND model_id = CAST(:m AS uuid)",
        m=tiny,
    )
    base = f"/projects/{env.project_id}"
    report = (await client.get(f"{base}/analytics/feedback", headers=env.admin)).json()
    assert (report["ratings"], report["dislikes"]) == (23, 5)
    groups = {(g["model"], g["task_type"]): g for g in report["groups"]}
    g = groups[("mock-tiny", "classification")]
    assert (g["ratings"], g["dislikes"], g["dislike_rate"]) == (10, 4, 0.4)
    assert g["dislike_rate_low"] < 0.4 < g["dislike_rate_high"]
    assert groups[("mock-tiny", "extraction")]["dislikes"] == 0

    bad = (await client.get(f"{base}/feedback", params={"rating": -1}, headers=env.admin)).json()
    assert len(bad["items"]) == 5 and all(i["rating"] == -1 for i in bad["items"])
    assert {i["comment"] for i in bad["items"] if i["model"] == "mock-tiny"} == {"wrong label"}
    page = (await client.get(f"{base}/feedback", params={"limit": 7}, headers=env.admin)).json()
    assert len(page["items"]) == 7
    assert (
        await client.get(f"{base}/feedback", params={"rating": 5}, headers=env.admin)
    ).status_code == 422


# ---- access ------------------------------------------------------------------------------------
async def test_members_read_only_admins_recompute_and_strangers_see_nothing(client, admin, env):
    base = f"/projects/{env.project_id}"
    await make_admin(client, admin, "stranger@example.com", "admin")
    stranger = bearer((await login(client, "stranger@example.com", GOOD_PASSWORD)).json())
    for path in ("/analytics/overview", "/analytics/usage", "/analytics/feedback", "/feedback"):
        assert (await client.get(base + path, headers=stranger)).status_code == 404, path
    assert (
        await client.post(
            f"{base}/analytics/recompute", json={"since": "2026-01-01"}, headers=stranger
        )
    ).status_code == 404

    await make_admin(client, admin, "viewer@example.com", "viewer")
    await client.post(f"{base}/members", json={"email": "viewer@example.com"}, headers=admin)
    viewer = bearer((await login(client, "viewer@example.com", GOOD_PASSWORD)).json())
    assert (await client.get(f"{base}/analytics/overview", headers=viewer)).status_code == 200
    assert (await client.get("/analytics/servers", headers=viewer)).status_code == 200
    assert (
        await client.post(
            f"{base}/analytics/recompute", json={"since": "2026-01-01"}, headers=viewer
        )
    ).status_code == 403


# ---- the scheduled jobs ------------------------------------------------------------------------
async def test_a_periodic_job_runs_again_after_a_failure_and_stops_when_told():
    import asyncio

    calls = []
    stop = asyncio.Event()

    async def job():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the database blinked")

    task = asyncio.create_task(_periodic("test", 0.05, job, stop))
    await wait_until(lambda: len(calls) >= 3, timeout=5)  # a failed run did not end the loop
    stop.set()
    await asyncio.wait_for(task, 2)
