"""Shadow mode end to end (gateway -> RabbitMQ -> worker -> candidate) and the recognition of
conversations in the traffic log."""

import uuid
from collections.abc import AsyncIterator

import pytest

from app.core.messaging import Consumer, Message
from app.core.messaging.topology import QueueNames
from app.modules.routing.shadow import ShadowExecutor
from app.modules.traffic.writer import RequestLogWriter
from app.worker.consumers import request_log_spec
from tests.integration.calibration_support import Env, Pipeline, build_env, sql
from tests.integration.conftest import MockServer, wait_until
from tests.integration.gateway_support import GatewayHandle, chat_body, run_gateway
from tests.integration.routing_support import go_live, set_mode
from tests.integration.test_auth_api import bearer, login

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
def settings(app_settings, queue_name):
    return app_settings.model_copy(
        update={
            "request_log_queue": f"{queue_name}_log",
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
    return await build_env(client, admin, api_app, mock_llm.base_url, requests=0)


@pytest.fixture
async def worker(api_app, settings, amqp, publisher) -> AsyncIterator[Pipeline]:
    """The request log writer, the outbox relay and the copy executor of the worker."""
    writer = RequestLogWriter(api_app.state.resources.session_factory, settings)
    consumer = Consumer(
        request_log_spec(writer, settings), amqp, publisher,
        retry_delays_ms=settings.retry_delays_ms,
    )  # fmt: skip
    await consumer.start()
    pipeline = await Pipeline(api_app, settings, amqp, publisher).start()
    yield pipeline
    await pipeline.stop()
    await consumer.stop(5)
    channel = await amqp.channel()
    names = QueueNames(settings.request_log_queue)
    for q in [names.work, names.dlq, *(names.retry(i) for i in (1, 2, 3))]:
        await channel.queue_delete(q)


@pytest.fixture
async def gw(settings, env, worker) -> AsyncIterator[GatewayHandle]:
    async for handle in run_gateway(settings, collect=False):
        yield handle


async def send(gw, env, messages=None, *, task="classification", **headers):
    body = chat_body("great product")
    if messages is not None:
        body["messages"] = messages
    return await gw.client.post(
        "/v1/chat/completions",
        json=body,
        headers={**gw.auth(env.stack.key), "x-task-type": task, **headers},
    )


async def shadow_rows(api_app, n: int | None = None):
    async def read():
        return await sql(
            api_app,
            "SELECT s.*, m.name AS model FROM shadow_results s JOIN models m ON m.id = s.model_id"
            " ORDER BY s.created_at",
        )

    if n is None:
        return await read()

    async def enough():
        rows = await read()
        return rows if len(rows) >= n else None

    return await wait_until(enough, timeout=20)


# ---- shadow mode ------------------------------------------------------------------------------
async def test_a_share_of_live_traffic_is_copied_to_the_candidates_but_the_client_sees_the_reference(
    client, env, api_app, gw
):
    r = await set_mode(
        client, env, "shadow", shadow_model_ids=[env.candidates["tiny"]], shadow_rate=1.0
    )
    assert r.status_code == 200, r.text
    await gw.refresh()

    answers = [await send(gw, env, [{"role": "user", "content": f"review {i}"}]) for i in range(5)]
    assert all(a.status_code == 200 for a in answers)
    assert {a.headers["x-switchyard-model"] for a in answers} == {"mock-strong"}
    assert {a.headers["x-switchyard-mode"] for a in answers} == {"shadow"}

    rows = await shadow_rows(api_app, 5)
    assert len(rows) == 5 and {r.model for r in rows} == {"mock-tiny"}
    assert {r.status for r in rows} == {"ok"}
    assert all(r.response_payload["choices"] and r.latency_ms is not None for r in rows)
    assert all(r.cost_usd is not None and r.prompt_tokens for r in rows)
    logged = await sql(api_app, "SELECT mode, model FROM requests ORDER BY started_at")
    assert {(r.mode, r.model) for r in logged} == {("shadow", "mock-strong")}


async def test_the_copy_answers_the_same_question_as_the_original(client, env, api_app, gw):
    await set_mode(client, env, "shadow", shadow_model_ids=[env.candidates["mid"]], shadow_rate=1.0)
    await gw.refresh()
    await send(gw, env, [{"role": "user", "content": "classify: it was wonderful"}])
    (row,) = await shadow_rows(api_app, 1)
    original = (
        await sql(
            api_app, "SELECT request_payload FROM requests WHERE request_id = :r", r=row.request_id
        )
    )[0].request_payload
    assert original["messages"][0]["content"] == "classify: it was wonderful"


async def test_record_mode_makes_no_copies(client, env, api_app, gw):
    for _ in range(3):
        await send(gw, env)

    async def logged():
        return len(await sql(api_app, "SELECT 1 FROM requests")) == 3

    await wait_until(logged)
    assert await shadow_rows(api_app) == []


async def test_nothing_is_copied_when_the_project_does_not_keep_bodies(client, env, api_app, gw):
    r = await client.patch(
        f"/projects/{env.project_id}", json={"store_bodies": False}, headers=env.admin
    )
    assert r.status_code == 200
    await set_mode(
        client, env, "shadow", shadow_model_ids=[env.candidates["tiny"]], shadow_rate=1.0
    )
    await gw.refresh()
    for _ in range(3):
        await send(gw, env)

    async def logged():
        return len(await sql(api_app, "SELECT 1 FROM requests")) == 3

    await wait_until(logged)
    assert await shadow_rows(api_app) == []  # there is no stored prompt to copy


async def test_a_streamed_request_is_copied_too(client, env, api_app, gw):
    await set_mode(
        client, env, "shadow", shadow_model_ids=[env.candidates["tiny"]], shadow_rate=1.0
    )
    await gw.refresh()
    body = chat_body("stream me", stream=True)
    r = await gw.client.post(
        "/v1/chat/completions", json=body,
        headers={**gw.auth(env.stack.key), "x-task-type": "classification"},
    )  # fmt: skip
    assert r.status_code == 200 and "[DONE]" in r.text
    (row,) = await shadow_rows(api_app, 1)
    assert row.model == "mock-tiny" and row.status == "ok"


async def test_a_repeated_copy_job_stores_one_result(client, env, api_app, gw, settings):
    await set_mode(
        client, env, "shadow", shadow_model_ids=[env.candidates["tiny"]], shadow_rate=1.0
    )
    await gw.refresh()
    await send(gw, env)
    (row,) = await shadow_rows(api_app, 1)
    started = (await sql(api_app, "SELECT started_at FROM requests"))[0].started_at
    job = {
        "request_id": str(row.request_id),
        "started_at": started.isoformat(),
        "project_id": env.project_id,
        "task_type": "classification",
        "model_id": env.candidates["tiny"],
    }
    executor = ShadowExecutor(api_app.state.resources, settings, api_app.state.keyring)
    await executor.handle(Message("shadow.execute", job))
    await executor.handle(Message("shadow.execute", job))
    assert len(await shadow_rows(api_app)) == 1


async def test_a_copy_of_a_prompt_that_does_not_fit_the_model_is_recorded_not_sent(
    client, admin, env, api_app, gw
):
    small = await client.patch(
        f"/models/{env.candidates['tiny']}", json={"context_window": 5}, headers=admin
    )
    assert small.status_code == 200
    await set_mode(
        client, env, "shadow", shadow_model_ids=[env.candidates["tiny"]], shadow_rate=1.0
    )
    await gw.refresh()
    await send(gw, env)
    (row,) = await shadow_rows(api_app, 1)
    assert row.status == "context_exceeded" and row.response_payload is None


async def test_switching_back_to_route_with_a_rule_stops_the_copies(client, env, api_app, gw):
    await set_mode(
        client, env, "shadow", shadow_model_ids=[env.candidates["tiny"]], shadow_rate=1.0
    )
    await go_live(client, env, "mid", "ref")  # a rule exists and route mode replaces shadow
    await gw.refresh()
    r = await send(gw, env)
    assert (
        r.headers["x-switchyard-mode"] == "route" and r.headers["x-switchyard-model"] == "mock-mid"
    )

    async def logged():
        return len(await sql(api_app, "SELECT 1 FROM requests")) == 1

    await wait_until(logged)
    assert await shadow_rows(api_app) == []


# ---- conversations in the traffic log ---------------------------------------------------------
OPENING = [
    {"role": "system", "content": "You are an agent."},
    {"role": "user", "content": "Find me a flight."},
]


async def test_the_steps_of_a_conversation_get_the_same_id_and_their_step_number(
    client, env, api_app, gw
):
    history = list(OPENING)
    for _ in range(3):
        r = await send(gw, env, history)
        assert r.status_code == 200
        history += [
            {"role": "assistant", "content": r.json()["choices"][0]["message"]["content"]},
            {"role": "user", "content": "and then?"},
        ]

    async def logged():
        return len(await sql(api_app, "SELECT 1 FROM requests")) == 3

    await wait_until(logged)
    rows = await sql(api_app, "SELECT session_id, step FROM requests ORDER BY step")
    assert [r.step for r in rows] == [0, 1, 2]
    assert len({r.session_id for r in rows}) == 1 and rows[0].session_id.startswith("auto-")


async def test_a_session_id_sent_by_the_client_is_used_as_it_is(client, env, api_app, gw):
    sid = f"conv-{uuid.uuid4().hex[:8]}"
    await send(gw, env, OPENING, **{"x-session-id": sid})
    await send(gw, env, [*OPENING, {"role": "assistant", "content": "x"}], **{"x-session-id": sid})

    async def logged():
        return len(await sql(api_app, "SELECT 1 FROM requests")) == 2

    await wait_until(logged)
    rows = await sql(api_app, "SELECT session_id, step FROM requests ORDER BY step")
    assert [(r.session_id, r.step) for r in rows] == [(sid, 0), (sid, 1)]
