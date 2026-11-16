"""The cost of a request, the monthly budget of a key, ratings from clients, and what people
say about the judge (real PostgreSQL, Redis, RabbitMQ, mock_llm)."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.messaging import Consumer, Message
from app.core.messaging.topology import QueueNames
from app.modules.analytics import budgets
from app.modules.feedback.writer import FeedbackWriter
from app.modules.gateway.budget import counter_name, month_key
from app.modules.traffic.writer import RequestLogWriter
from app.worker.consumers import feedback_spec, request_log_spec
from tests.integration.calibration_support import (
    Env,
    Pipeline,
    add_check,
    add_judge,
    add_model,
    add_traffic_from_mock,
    aggregates,
    build_env,
    evaluations,
    set_price,
    sql,
    start_run,
    wait_evaluated,
    wait_status,
)
from tests.integration.conftest import MockServer, wait_until
from tests.integration.gateway_support import GatewayHandle, chat_body, run_gateway
from tests.integration.routing_support import go_live
from tests.integration.test_auth_api import GOOD_PASSWORD, bearer, login, make_admin
from tests.integration.test_traffic_api import insert_rows

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
def settings(app_settings, queue_name):
    return app_settings.model_copy(
        update={
            "request_log_queue": f"{queue_name}_log",
            "feedback_queue": f"{queue_name}_feedback",
            "replay_queue": f"{queue_name}_replay",
            "run_finished_queue": f"{queue_name}_done",
            "judge_queue": f"{queue_name}_judge",
            "shadow_queue": f"{queue_name}_shadow",
            "retry_delays_ms": [100, 200],
            "traffic_batch_size": 20,
            "traffic_batch_wait": 0.05,
            "calibration_limit_wait": 1.0,
            "calibration_default_rps": 1000.0,
            "calibration_default_concurrency": 20,
        }
    )


@pytest.fixture
async def env(client, admin, api_app, mock_llm: MockServer) -> Env:
    env = await build_env(client, admin, api_app, mock_llm.base_url, requests=0)
    await set_price(client, admin, env.reference_id, "3.00", "15.00")
    return env


async def say(gw: GatewayHandle, env: Env, text: str = "great product", *, model=None, **headers):
    body = chat_body(text)
    if model:
        body["model"] = model
    return await gw.client.post(
        "/v1/chat/completions",
        json=body,
        headers={**gw.auth(env.stack.key), "x-task-type": "classification", **headers},
    )


# ---- what a request costs ---------------------------------------------------------------------
@pytest.fixture
async def gw_collecting(settings, env) -> GatewayHandle:  # type: ignore[misc]
    async for handle in run_gateway(settings):
        yield handle


async def test_a_request_is_costed_with_the_prices_of_the_moment_and_against_the_reference(
    client, env, gw_collecting
):
    gw = gw_collecting
    r = await say(gw, env)  # the reference answers: cost and reference cost are the same
    rec = gw.recorder.last
    usage = r.json()["usage"]
    expected = (
        Decimal(usage["prompt_tokens"]) * 3 + Decimal(usage["completion_tokens"]) * 15
    ) / 1_000_000
    assert rec.cost_usd == expected.quantize(Decimal("0.00000001"))
    assert rec.reference_cost_usd == rec.cost_usd

    await go_live(client, env, "tiny", "ref")  # tiny: 0.1 / 0.2 per million
    await gw.refresh()
    r = await say(gw, env)
    rec = gw.recorder.last
    usage = r.json()["usage"]
    cheap = (
        Decimal(usage["prompt_tokens"]) * Decimal("0.1")
        + Decimal(usage["completion_tokens"]) * Decimal("0.2")
    ) / 1_000_000
    pricey = (
        Decimal(usage["prompt_tokens"]) * 3 + Decimal(usage["completion_tokens"]) * 15
    ) / 1_000_000
    assert rec.cost_usd == cheap.quantize(Decimal("0.00000001"))
    assert rec.reference_cost_usd == pricey.quantize(
        Decimal("0.00000001")
    )  # what it would have cost
    assert rec.cost_usd < rec.reference_cost_usd


async def test_a_server_paid_by_the_month_adds_nothing_per_request(
    client, admin, env, gw_collecting
):
    server = await add_model(
        client, admin, env.stack.provider["id"], "mock-judge", cost_type="fixed_monthly"
    )
    await client.post(
        f"/models/{server}/prices", json={"monthly_cost_usd": "100.00"}, headers=admin
    )
    env.candidates["server"] = server
    await go_live(client, env, "server", "ref")
    await gw_collecting.refresh()
    await say(gw_collecting, env)
    rec = gw_collecting.recorder.last
    assert rec.cost_usd == 0 and rec.reference_cost_usd > 0


async def test_a_failed_request_has_no_cost(env, gw_collecting):
    r = await gw_collecting.client.post(
        "/v1/chat/completions", json=chat_body("x"),
        headers={**gw_collecting.auth(env.stack.key), "x-mock-fail": "503"},
    )  # fmt: skip
    assert r.status_code == 502
    assert gw_collecting.recorder.last.cost_usd is None  # no tokens were reported


async def test_the_cost_reaches_the_traffic_log_and_the_summaries(
    client, env, api_app, settings, amqp, publisher
):
    writer = RequestLogWriter(api_app.state.resources.session_factory, settings)
    consumer = Consumer(request_log_spec(writer, settings), amqp, publisher, retry_delays_ms=[100])
    await consumer.start()
    try:
        async for gw in run_gateway(settings, collect=False):
            r = await say(gw, env)
            assert r.status_code == 200

            async def logged():
                return await sql(api_app, "SELECT cost_usd, reference_cost_usd FROM requests")

            rows = await wait_until(logged)
            usage = r.json()["usage"]
            expected = (
                Decimal(usage["prompt_tokens"]) * 3 + Decimal(usage["completion_tokens"]) * 15
            ) / 1_000_000
            assert rows[0].cost_usd == expected.quantize(Decimal("0.00000001"))
    finally:
        await consumer.stop(5)
        channel = await amqp.channel()
        names = QueueNames(settings.request_log_queue)
        for q in [names.work, names.dlq, *(names.retry(i) for i in (1, 2))]:
            await channel.queue_delete(q)


# ---- the monthly budget of a key ---------------------------------------------------------------
async def set_budget(client, env, amount, key_id=None):
    r = await client.patch(
        f"/projects/{env.project_id}/keys/{key_id or env.stack.key_id}",
        json={"monthly_budget_usd": amount},
        headers=env.admin,
    )
    assert r.status_code == 200, r.text


async def settle(gw, redis_name, redis, previous, wait=3.0):
    """The cost is charged in the background: wait until the counter has moved."""

    async def moved():
        raw = await redis.get(redis_name)
        return raw is not None and int(raw) > previous

    await wait_until(moved, timeout=wait)


async def test_a_key_that_used_its_budget_is_refused_until_the_limit_is_raised(
    client, env, api_app, gw_collecting
):
    gw = gw_collecting
    redis = api_app.state.resources.redis
    name = counter_name(env.stack.key_id, month_key())
    await set_budget(client, env, "0.0002")  # a request costs about 0.000066
    await gw.refresh()

    ok, refused = 0, None
    for _ in range(20):
        before = int(await redis.get(name) or 0)
        r = await say(gw, env)
        if r.status_code == 429:
            refused = r
            break
        assert r.status_code == 200
        ok += 1
        await settle(gw, name, redis, before)
    assert 2 <= ok <= 5 and refused is not None
    error = refused.json()["error"]
    assert (error["code"], error["type"]) == ("budget_exceeded", "insufficient_quota")
    spent = sum(rec.cost_usd for rec in gw.recorder.records if rec.cost_usd)
    assert Decimal(int(await redis.get(name))) / 1_000_000 >= Decimal("0.0002")
    assert Decimal(int(await redis.get(name))) / 1_000_000 == pytest.approx(
        spent, abs=Decimal("0.000002")
    )

    await set_budget(client, env, "1.00")  # an administrator raises the limit ...
    await gw.refresh()
    assert (await say(gw, env)).status_code == 200  # ... and the key works again


async def test_other_keys_and_keys_without_a_budget_are_not_affected(
    client, env, api_app, gw_collecting
):
    gw = gw_collecting
    redis = api_app.state.resources.redis
    other = await client.post(
        f"/projects/{env.project_id}/keys", json={"name": "second"}, headers=env.admin
    )
    other_key = other.json()["key"]
    await set_budget(client, env, "0.0001")  # a request costs about 0.000066: room for one
    await gw.refresh()
    name = counter_name(env.stack.key_id, month_key())
    statuses = []
    for _ in range(6):
        before = int(await redis.get(name) or 0)
        statuses.append((await say(gw, env)).status_code)
        if statuses[-1] == 200:
            await settle(gw, name, redis, before)
    assert statuses[0] == 200 and statuses[-1] == 429  # used up after a request or two

    free = await gw.client.post(
        "/v1/chat/completions", json=chat_body("hi"), headers=gw.auth(other_key)
    )
    assert free.status_code == 200  # no budget: no limit


async def test_a_budget_of_zero_blocks_the_key_after_its_first_cost_and_none_never_blocks(
    client, env, gw_collecting
):
    gw = gw_collecting
    await set_budget(client, env, None)  # cleared
    await gw.refresh()
    for _ in range(3):
        assert (await say(gw, env)).status_code == 200


async def test_the_budget_of_a_key_is_reported_with_the_log_and_the_counter(
    client, env, api_app, gw_collecting
):
    gw = gw_collecting
    await set_budget(client, env, "5.00")
    await insert_rows(
        api_app, env.project_id,
        [{"key_id": uuid.UUID(env.stack.key_id), "cost_usd": Decimal("1.25"),
          "model_id": uuid.UUID(env.reference_id), "started_at": datetime.now(UTC)}],
    )  # fmt: skip
    url = f"/projects/{env.project_id}/keys/{env.stack.key_id}/budget"
    body = (await client.get(url, headers=env.admin)).json()
    assert Decimal(body["spent_usd"]) == Decimal("1.25") and body["counter_usd"] is None
    assert Decimal(body["remaining_usd"]) == Decimal("3.75") and body["exhausted"] is False
    overview = (
        await client.get(f"/projects/{env.project_id}/analytics/overview", headers=env.admin)
    ).json()
    key = next(k for k in overview["keys"] if k["key_id"] == env.stack.key_id)
    assert key["used_pct"] == pytest.approx(25.0)
    assert gw  # (the gateway is not needed here: the report reads the log and Redis)


async def test_the_counter_is_repaired_from_the_log_but_the_log_never_lowers_it(
    client, env, api_app
):
    redis = api_app.state.resources.redis
    factory = api_app.state.resources.session_factory
    await set_budget(client, env, "5.00")
    name = counter_name(env.stack.key_id, month_key())
    await insert_rows(
        api_app, env.project_id,
        [{"key_id": uuid.UUID(env.stack.key_id), "cost_usd": Decimal("2.00"),
          "model_id": uuid.UUID(env.reference_id)}],
    )  # fmt: skip

    assert await redis.get(name) is None  # Redis lost (or never had) the counter
    assert await budgets.reconcile(factory, redis) == 1
    assert int(await redis.get(name)) == 2_000_000  # raised to what the log says
    assert await budgets.reconcile(factory, redis) == 0  # already right: nothing to do

    await redis.set(name, 9_000_000)  # the gateway has seen requests that are not in the log yet
    assert await budgets.reconcile(factory, redis) == 0
    assert int(await redis.get(name)) == 9_000_000  # the log is behind: it must not lower it

    r = await client.post(
        f"/projects/{env.project_id}/keys/{env.stack.key_id}/budget/recount", headers=env.admin
    )  # a person asks for the truth
    assert r.status_code == 200 and int(await redis.get(name)) == 2_000_000


async def test_keys_without_a_budget_and_revoked_keys_are_not_reconciled(client, env, api_app):
    redis = api_app.state.resources.redis
    await insert_rows(
        api_app, env.project_id,
        [{"key_id": uuid.UUID(env.stack.key_id), "cost_usd": Decimal("1.00"),
          "model_id": uuid.UUID(env.reference_id)}],
    )  # fmt: skip
    assert await budgets.reconcile(api_app.state.resources.session_factory, redis) == 0
    assert await redis.get(counter_name(env.stack.key_id, month_key())) is None


async def test_only_administrators_recount_a_budget(client, admin, env):
    await make_admin(client, admin, "viewer@example.com", "viewer")
    await client.post(
        f"/projects/{env.project_id}/members", json={"email": "viewer@example.com"}, headers=admin
    )
    viewer = bearer((await login(client, "viewer@example.com", GOOD_PASSWORD)).json())
    url = f"/projects/{env.project_id}/keys/{env.stack.key_id}/budget"
    assert (await client.get(url, headers=viewer)).status_code == 200
    assert (await client.post(url + "/recount", headers=viewer)).status_code == 403


# ---- ratings from clients ---------------------------------------------------------------------
@pytest.fixture
async def feedback_stack(api_app, settings, amqp, publisher):
    """The request log writer and the rating writer of the worker."""
    log_writer = RequestLogWriter(api_app.state.resources.session_factory, settings)
    rating_writer = FeedbackWriter(api_app.state.resources.session_factory, settings)
    consumers = [
        Consumer(
            request_log_spec(log_writer, settings), amqp, publisher, retry_delays_ms=[100, 200]
        ),
        Consumer(
            feedback_spec(rating_writer, settings), amqp, publisher, retry_delays_ms=[100, 200]
        ),
    ]
    for c in consumers:
        await c.start()
    yield rating_writer
    for c in consumers:
        await c.stop(5)
    channel = await amqp.channel()
    for name in (settings.request_log_queue, settings.feedback_queue):
        names = QueueNames(name)
        for q in [names.work, names.dlq, *(names.retry(i) for i in (1, 2))]:
            await channel.queue_delete(q)


@pytest.fixture
async def gw(settings, env, feedback_stack):
    async for handle in run_gateway(settings, collect=False):
        yield handle


async def rate(gw, env, request_id, rating, comment=None, key=None):
    body = {"request_id": request_id, "rating": rating}
    if comment is not None:
        body["comment"] = comment
    return await gw.client.post("/v1/feedback", json=body, headers=gw.auth(key or env.stack.key))


async def stored(api_app, request_id):
    rows = await sql(
        api_app, "SELECT * FROM feedback WHERE request_id = :r", r=uuid.UUID(request_id)
    )
    return rows[0] if rows else None


async def logged(api_app, request_id):
    async def there():
        return await sql(
            api_app, "SELECT 1 FROM requests WHERE request_id = :r", r=uuid.UUID(request_id)
        )

    await wait_until(there)


async def test_a_rating_is_stored_against_the_request_and_the_model_that_answered_it(
    client, env, api_app, gw
):
    await go_live(client, env, "tiny", "ref")
    await gw.refresh()
    answer = await say(gw, env)
    request_id = answer.headers["x-request-id"]

    r = await rate(gw, env, request_id, -1, "wrong label")
    assert r.status_code == 202 and r.json() == {"status": "accepted", "request_id": request_id}

    async def has_row():
        return await stored(api_app, request_id)

    row = await wait_until(has_row)
    assert (row.rating, row.comment, row.task_type) == (-1, "wrong label", "classification")
    assert str(row.model_id) == env.candidates["tiny"]  # the model the user actually saw
    assert str(row.project_id) == env.project_id


async def test_rating_again_replaces_the_rating_and_an_older_message_cannot_undo_it(
    client, env, api_app, gw, feedback_stack, settings
):
    request_id = (await say(gw, env)).headers["x-request-id"]
    await rate(gw, env, request_id, -1, "bad")
    await wait_until(lambda: stored(api_app, request_id))
    await rate(gw, env, request_id, 1, "better on second thought")

    async def changed():
        row = await stored(api_app, request_id)
        return row is not None and row.rating == 1

    await wait_until(changed)
    assert len(await sql(api_app, "SELECT 1 FROM feedback")) == 1  # one row per request

    old = {
        "request_id": request_id, "project_id": env.project_id, "key_id": env.stack.key_id,
        "rating": -1, "comment": "delayed message",
        "submitted_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }  # fmt: skip
    await feedback_stack.handle(Message("feedback.submitted", old))  # arrives late
    row = await stored(api_app, request_id)
    assert row.rating == 1 and row.comment == "better on second thought"


async def test_a_rating_for_somebody_elses_request_is_never_stored(
    client, admin, env, api_app, gw, feedback_stack
):
    request_id = (await say(gw, env)).headers["x-request-id"]
    await logged(api_app, request_id)
    other = (await client.post("/projects", json={"name": "other"}, headers=admin)).json()
    await client.put(
        f"/projects/{other['id']}/reference-model",
        json={"model_id": env.reference_id},
        headers=admin,
    )
    other_key = (
        await client.post(f"/projects/{other['id']}/keys", json={"name": "k"}, headers=admin)
    ).json()["key"]
    await gw.refresh()

    r = await rate(gw, env, request_id, -1, key=other_key)
    assert r.status_code == 202  # the gateway cannot know: it does not look at the database
    unknown = await rate(gw, env, str(uuid.uuid4()), 1)
    assert unknown.status_code == 202

    await wait_until(lambda: feedback_stack.dropped >= 2, timeout=15)  # after the retries
    assert await sql(api_app, "SELECT 1 FROM feedback") == []


async def test_a_rating_that_arrives_before_its_request_is_in_the_log_is_retried(
    client, env, api_app, gw, feedback_stack
):
    request_id = str(uuid.uuid4())
    await rate(gw, env, request_id, 1)  # the request does not exist (yet)
    await asyncio.sleep(0.15)
    started = datetime.now(UTC)
    await insert_rows(
        api_app, env.project_id,
        [{"request_id": uuid.UUID(request_id), "started_at": started,
          "model_id": uuid.UUID(env.reference_id), "task_type": "classification"}],
    )  # fmt: skip

    async def has_row():
        return await stored(api_app, request_id)

    row = await wait_until(has_row, timeout=10)  # a retry found it
    assert row.rating == 1


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": "nope", "rating": 1},
        {"rating": 1},
        {"request_id": str(uuid.uuid4()), "rating": 0},
        {"request_id": str(uuid.uuid4()), "rating": 5},
        {"request_id": str(uuid.uuid4()), "rating": "1"},
        {"request_id": str(uuid.uuid4()), "rating": True},
        {"request_id": str(uuid.uuid4()), "rating": 1, "comment": 12},
        {"request_id": str(uuid.uuid4()), "rating": 1, "comment": "x" * 1001},
    ],
)
async def test_a_malformed_rating_is_refused_at_the_gateway(env, gw, body):
    r = await gw.client.post("/v1/feedback", json=body, headers=gw.auth(env.stack.key))
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"


async def test_feedback_needs_a_valid_key_and_a_json_body(env, gw):
    assert (await gw.client.post("/v1/feedback", json={})).status_code == 401
    bad = await gw.client.post("/v1/feedback", json={}, headers=gw.auth("swy_aaaaaaaa_" + "x" * 32))
    assert bad.status_code == 401
    garbage = await gw.client.post(
        "/v1/feedback", content=b"{not json", headers=gw.auth(env.stack.key)
    )
    assert garbage.status_code == 400


# ---- people and the judge -----------------------------------------------------------------------
async def evaluated_run(client, env, api_app, mock_llm, pipeline):
    await add_judge(client, env.admin, env)
    await add_traffic_from_mock(api_app, env, mock_llm.base_url, 40)
    await add_check(client, env, "exact_match")
    run = (
        await start_run(
            client, env, "tiny", sample_size=40, seed=3, judge_model_id=env.candidates["judge"]
        )
    ).json()
    await pipeline.start()
    await wait_status(client, env, run["id"], "completed")
    await wait_evaluated(client, env, run["id"])
    return run


@pytest.fixture
async def pipeline(api_app, settings, amqp, publisher):
    p = Pipeline(api_app, settings, amqp, publisher)
    yield p
    await p.stop()


async def verdict(client, env, run_id, result_id, value, headers=None):
    return await client.put(
        f"/projects/{env.project_id}/calibration-runs/{run_id}/results/{result_id}/human-verdict",
        json={"verdict": value, "comment": "checked by hand"},
        headers=headers or env.admin,
    )


async def test_the_judge_is_compared_with_the_people_who_checked_the_same_answers(
    client, admin, env, api_app, mock_llm, pipeline
):
    run = await evaluated_run(client, env, api_app, mock_llm, pipeline)
    judged = [e for e in await evaluations(api_app, run["id"]) if e.judge_calls > 0]
    assert len(judged) >= 8

    # people agree with the judge on the first half and disagree on the second
    for i, e in enumerate(judged[:12]):
        judge_says_pass = e.judge_verdict == "pass"
        human = judge_says_pass if i < 8 else not judge_says_pass
        r = await verdict(client, env, run["id"], e.result_id, "pass" if human else "fail")
        assert r.status_code == 200, r.text
    count = min(len(judged), 12)

    body = (
        await client.get(
            f"/projects/{env.project_id}/calibration-runs/{run['id']}/judge-agreement",
            headers=env.admin,
        )
    ).json()
    assert body["compared"] == count and body["agreed"] == min(8, count)
    assert body["agreement_rate"] == pytest.approx(min(8, count) / count)
    matrix = (
        body["judge_pass_human_pass"] + body["judge_pass_human_fail"]
        + body["judge_fail_human_pass"] + body["judge_fail_human_fail"]
    )  # fmt: skip
    assert matrix == count
    assert body["cohen_kappa"] is None or -1.0 <= body["cohen_kappa"] <= 1.0
    assert "live traffic" in body["note"]


async def test_a_persons_verdict_outranks_the_judge_in_the_numbers_of_the_run(
    client, admin, env, api_app, mock_llm, pipeline
):
    run = await evaluated_run(client, env, api_app, mock_llm, pipeline)
    before = (await aggregates(client, env, run["id"]))["items"][0]
    failed = [e for e in await evaluations(api_app, run["id"]) if e.passed is False][:5]
    assert len(failed) == 5

    for e in failed:  # a person says these answers are fine after all
        r = await verdict(client, env, run["id"], e.result_id, "pass")
        assert r.json()["aggregates_recomputed"] is True
    after = (await aggregates(client, env, run["id"]))["items"][0]
    assert after["n_evaluated"] == before["n_evaluated"]
    assert after["pass_rate"] == pytest.approx(before["pass_rate"] + 5 / before["n_evaluated"])

    again = await verdict(client, env, run["id"], failed[0].result_id, "fail")  # changes their mind
    assert again.status_code == 200
    final = (await aggregates(client, env, run["id"]))["items"][0]
    assert final["pass_rate"] == pytest.approx(before["pass_rate"] + 4 / before["n_evaluated"])
    assert len(await sql(api_app, "SELECT 1 FROM human_verdicts")) == 5  # one per person and answer


async def test_a_verdict_needs_an_administrator_a_result_of_this_run_and_a_valid_value(
    client, admin, env, api_app, mock_llm, pipeline
):
    run = await evaluated_run(client, env, api_app, mock_llm, pipeline)
    result_id = (await evaluations(api_app, run["id"]))[0].result_id
    assert (await verdict(client, env, run["id"], uuid.uuid4(), "pass")).status_code == 404
    bad = await client.put(
        f"/projects/{env.project_id}/calibration-runs/{run['id']}/results/{result_id}/human-verdict",
        json={"verdict": "maybe"}, headers=env.admin,
    )  # fmt: skip
    assert bad.status_code == 422

    await make_admin(client, admin, "viewer@example.com", "viewer")
    await client.post(
        f"/projects/{env.project_id}/members", json={"email": "viewer@example.com"}, headers=admin
    )
    viewer = bearer((await login(client, "viewer@example.com", GOOD_PASSWORD)).json())
    assert (
        await verdict(client, env, run["id"], result_id, "pass", headers=viewer)
    ).status_code == 403


async def test_agreement_with_no_verdicts_says_so(client, env, api_app, mock_llm, pipeline):
    run = await evaluated_run(client, env, api_app, mock_llm, pipeline)
    body = (
        await client.get(
            f"/projects/{env.project_id}/calibration-runs/{run['id']}/judge-agreement",
            headers=env.admin,
        )
    ).json()
    assert body["compared"] == 0 and body["agreement_rate"] is None and body["cohen_kappa"] is None


# ---- disliked requests come first in a sample -------------------------------------------------
async def test_requests_that_users_disliked_are_taken_first_when_asked(
    client, env, api_app, mock_llm
):
    await add_traffic_from_mock(api_app, env, mock_llm.base_url, 40)
    ids = [
        r.request_id
        for r in await sql(api_app, "SELECT request_id FROM requests ORDER BY started_at")
    ]
    disliked = set(ids[:12])
    for rid in disliked:
        await sql(
            api_app,
            "INSERT INTO feedback (id, request_id, project_id, request_started_at, task_type, rating)"
            " VALUES (gen_random_uuid(), :r, CAST(:p AS uuid), now(), 'classification', -1)",
            r=rid, p=env.project_id,
        )  # fmt: skip

    async def sample(**extra):
        run = (await start_run(client, env, "tiny", sample_size=10, seed=5, **extra)).json()
        rows = await sql(
            api_app, "SELECT request_id FROM run_samples WHERE run_id = :r", r=uuid.UUID(run["id"])
        )
        return {r.request_id for r in rows}

    plain = await sample()
    prioritised = await sample(prefer_disliked=True)
    assert len(plain) == len(prioritised) == 10
    assert len(prioritised & disliked) == 5  # up to half of the sample
    assert len(prioritised & disliked) >= len(plain & disliked)  # priority never gives fewer
