"""The gateway routes by the active rule: modes, pinned models, fallbacks, hot swap, resilience."""

import json

import httpx
import pytest

from tests.integration.calibration_support import Env, add_check, add_model, build_env
from tests.integration.conftest import MOCK_KEY, Endpoint, MockServer, wait_until
from tests.integration.gateway_support import GatewayHandle, chat_body, run_gateway
from tests.integration.routing_support import (
    BackgroundRelay,
    activate,
    go_live,
    mock_calls,
    propose,
    rollback,
    set_mode,
)
from tests.integration.tcp_proxy import TcpProxy
from tests.integration.test_auth_api import bearer, login

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
async def env(client, admin, api_app, mock_llm: MockServer) -> Env:
    return await build_env(client, admin, api_app, mock_llm.base_url, requests=0)


@pytest.fixture
async def gw(app_settings, env):
    async for handle in run_gateway(app_settings):
        yield handle


async def ask(gw: GatewayHandle, env: Env, *, model=None, task="classification", stream=False, **h):
    body = chat_body("great product", **({"stream": True} if stream else {}))
    if model:
        body["model"] = model
    headers = {**gw.auth(env.stack.key), **({"x-task-type": task} if task else {}), **h}
    return await gw.client.post("/v1/chat/completions", json=body, headers=headers)


def answered_by(response) -> str:
    return str(response.headers["x-switchyard-model"])


# ---- modes and names --------------------------------------------------------------------------
async def test_without_a_rule_the_reference_answers_as_before(client, env, gw):
    r = await ask(gw, env)
    assert r.status_code == 200
    assert (answered_by(r), r.headers["x-switchyard-mode"]) == ("mock-strong", "record")


async def test_a_task_type_in_route_mode_is_answered_by_the_first_model_of_the_rule(
    client, env, gw
):
    await go_live(client, env, "tiny", "ref")
    await gw.refresh()
    r = await ask(gw, env)
    assert r.status_code == 200
    assert (answered_by(r), r.headers["x-switchyard-mode"]) == ("mock-tiny", "route")
    assert gw.recorder.last.mode == "route" and gw.recorder.last.model == "mock-tiny"
    assert gw.recorder.last.fallbacks == 0


@pytest.mark.parametrize("asked", ["auto", "mock-strong", "gpt-4o", "anything-unregistered"])
async def test_the_reference_name_auto_and_unknown_names_all_mean_route_by_the_rules(
    client, env, gw, asked
):
    """The app keeps its old model name and only changes base_url: routing must still apply."""
    await go_live(client, env, "tiny", "ref")
    await gw.refresh()
    r = await ask(gw, env, model=asked)
    assert answered_by(r) == "mock-tiny" and r.headers["x-switchyard-mode"] == "route"


async def test_a_registered_model_that_is_not_the_reference_is_pinned(client, env, gw, mock_llm):
    await go_live(client, env, "tiny", "ref")
    await gw.refresh()
    r = await ask(gw, env, model="mock-mid")
    assert (answered_by(r), r.headers["x-switchyard-mode"]) == ("mock-mid", "pinned")
    assert gw.recorder.last.requested_model == "mock-mid"


async def test_other_task_types_and_requests_without_one_stay_on_the_reference(client, env, gw):
    await go_live(client, env, "tiny", "ref")
    await gw.refresh()
    for task in ("extraction", None):
        r = await ask(gw, env, task=task)
        assert (answered_by(r), r.headers["x-switchyard-mode"]) == ("mock-strong", "record")


async def test_back_to_record_mode_the_rule_stops_applying(client, env, gw):
    await go_live(client, env, "tiny", "ref")
    await gw.refresh()
    assert answered_by(await ask(gw, env)) == "mock-tiny"
    await set_mode(client, env, "record")
    await gw.refresh()
    assert answered_by(await ask(gw, env)) == "mock-strong"


async def test_a_streamed_answer_is_routed_too(client, env, gw):
    await go_live(client, env, "mid", "ref")
    await gw.refresh()
    r = await ask(gw, env, stream=True)
    assert r.status_code == 200 and answered_by(r) == "mock-mid"
    assert "data: [DONE]" in r.text


# ---- hot swap ---------------------------------------------------------------------------------
async def test_a_change_of_the_rule_reaches_a_running_gateway_without_a_restart(
    client, env, api_app, gw, publisher
):
    await go_live(client, env, "tiny", "ref")
    await gw.refresh()
    assert answered_by(await ask(gw, env)) == "mock-tiny"
    listener = gw.app.state.rules_listener
    await wait_until(lambda: listener.connected)

    async with BackgroundRelay(api_app, publisher):
        second = await propose(client, env, "mid", "ref")
        assert (await activate(client, env, second, 1)).status_code == 200

        async def switched():
            return answered_by(await ask(gw, env)) == "mock-mid"

        await wait_until(switched, timeout=15)  # no gw.refresh(): the event did it
        assert (await rollback(client, env)).status_code == 200

        async def switched_back():
            return answered_by(await ask(gw, env)) == "mock-tiny"

        await wait_until(switched_back, timeout=15)
    assert listener.events_seen >= 2


async def test_a_gateway_that_starts_while_the_database_is_down_still_routes_by_the_rules(
    client, env, gw, app_settings
):
    await go_live(client, env, "tiny", "ref")
    await gw.refresh()  # installs the snapshot and keeps a copy in Redis
    broken = app_settings.model_copy(update={"postgres_port": 1})  # nothing listens there
    async for other in run_gateway(broken):
        assert other.app.state.proxy.store.source == "redis"
        r = await ask(other, env)
        assert r.status_code == 200 and answered_by(r) == "mock-tiny"


async def test_a_running_gateway_keeps_routing_while_postgres_and_rabbitmq_are_down(
    client, env, app_settings, pg_ep: Endpoint, rabbit_ep: Endpoint
):
    pg = await TcpProxy(pg_ep.host, pg_ep.port).start()
    mq = await TcpProxy(rabbit_ep.host, rabbit_ep.port).start()
    through = app_settings.model_copy(
        update={
            "postgres_host": "127.0.0.1",
            "postgres_port": pg.port,
            "rabbitmq_host": "127.0.0.1",
            "rabbitmq_port": mq.port,
        }
    )
    try:
        await go_live(client, env, "tiny", "ref")
        async for gw in run_gateway(through):
            assert answered_by(await ask(gw, env)) == "mock-tiny"

            await pg.stop()
            await mq.stop()  # both are "down" now
            assert await gw.app.state.proxy.store.refresh() is False  # the refresh fails ...
            for _ in range(3):  # ... and the last good snapshot keeps serving
                r = await ask(gw, env)
                assert r.status_code == 200 and answered_by(r) == "mock-tiny"
            assert (await gw.client.get("/health/ready")).json()["status"] == "ok"

            # a rule changed meanwhile (the API talks to the database directly) is not seen yet
            second = await propose(client, env, "mid", "ref")
            assert (await activate(client, env, second, 1)).status_code == 200
            assert answered_by(await ask(gw, env)) == "mock-tiny"

            await pg.start()
            await mq.start()  # they are back: the next refresh picks the change up

            async def caught_up(handle=gw):
                await handle.refresh()
                return answered_by(await ask(handle, env)) == "mock-mid"

            await wait_until(caught_up, timeout=20)
    finally:
        await pg.stop()
        await mq.stop()


# ---- fallbacks --------------------------------------------------------------------------------
async def test_an_unreachable_first_model_hands_over_to_the_next_one(
    client, admin, env, gw, mock_llm
):
    dead = await client.post(
        "/providers",
        json={"name": "dead", "base_url": "http://localhost:9/v1", "is_local": True,
              "auth_type": "none"},
        headers=admin,
    )  # fmt: skip
    assert dead.status_code == 201, dead.text
    env.candidates["ghost"] = await add_model(client, admin, dead.json()["id"], "ghost")
    await go_live(client, env, "ghost", "mid", "ref")
    await gw.refresh()
    before = await mock_calls(mock_llm.base_url)
    r = await ask(gw, env)
    assert r.status_code == 200 and answered_by(r) == "mock-mid"
    assert gw.recorder.last.fallbacks == 1 and gw.recorder.last.model == "mock-mid"
    after = await mock_calls(mock_llm.base_url)
    assert after.get("mock-mid", 0) - before.get("mock-mid", 0) == 1


@pytest.mark.parametrize("failure", ["500", "503", "429"])
async def test_a_failure_before_any_answer_goes_to_the_next_model(
    client, env, gw, mock_llm, failure
):
    await go_live(client, env, "mid", "tiny", "ref")
    await gw.refresh()
    r = await ask(gw, env, **{"x-mock-fail": failure, "x-mock-fail-models": "mock-mid"})
    assert r.status_code == 200 and answered_by(r) == "mock-tiny"
    assert gw.recorder.last.fallbacks == 1


async def test_when_every_model_fails_the_client_gets_the_last_error(client, env, gw):
    await go_live(client, env, "mid", "tiny")
    await gw.refresh()
    r = await ask(gw, env, **{"x-mock-fail": "503"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_error"
    assert gw.recorder.last.fallbacks == 1  # the second model was tried before giving up
    assert gw.recorder.last.model == "mock-tiny"


async def test_a_client_mistake_is_not_retried_on_another_model(client, env, gw):
    await go_live(client, env, "mid", "tiny")
    await gw.refresh()
    r = await gw.client.post(
        "/v1/chat/completions",
        json={"messages": "this is not a list of messages"},
        headers={**gw.auth(env.stack.key), "x-task-type": "classification"},
    )
    assert r.status_code in (400, 422)
    assert gw.recorder.last.fallbacks == 0


async def test_after_a_stream_has_started_the_gateway_never_switches_models(
    client, env, gw, mock_llm
):
    await go_live(client, env, "mid", "tiny", "ref")
    await gw.refresh()
    before = await mock_calls(mock_llm.base_url)
    broken = {"x-mock-output-tokens": "100", "x-mock-drop-after-chunks": "3"}
    r = await ask(gw, env, stream=True, **broken)
    assert r.status_code == 200  # the client had already been given the start of the answer
    assert "upstream" in r.text and answered_by(r) == "mock-mid"
    after = await mock_calls(mock_llm.base_url)
    assert after.get("mock-tiny", 0) == before.get("mock-tiny", 0)  # no second model was asked
    assert after.get("mock-strong", 0) == before.get("mock-strong", 0)
    last = gw.recorder.last
    assert last.fallbacks == 0 and last.status != "ok"


async def test_a_pinned_model_has_no_fallbacks(client, env, gw, mock_llm):
    await go_live(client, env, "tiny", "ref")
    await gw.refresh()
    before = await mock_calls(mock_llm.base_url)
    r = await ask(gw, env, model="mock-mid", **{"x-mock-fail": "503"})
    assert r.status_code == 502
    assert (await mock_calls(mock_llm.base_url)).get("mock-tiny", 0) == before.get("mock-tiny", 0)
    assert gw.recorder.last.fallbacks == 0


# ---- validation fallback ----------------------------------------------------------------------
async def label_of(mock: MockServer, model: str, prompt: str) -> str:
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"{mock.base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            headers={"authorization": f"Bearer {MOCK_KEY}"},
        )
    return str(json.loads(r.json()["choices"][0]["message"]["content"])["label"])


async def prompt_where_they_differ(mock: MockServer) -> tuple[str, str, str]:
    for i in range(80):
        prompt = f"Classify text number {i}, which is about product {i}"
        mid, tiny = (
            await label_of(mock, "mock-mid", prompt),
            await label_of(mock, "mock-tiny", prompt),
        )
        if mid != tiny:
            return prompt, mid, tiny
    raise AssertionError("the two mock models never differed")


async def ask_json(gw, env, prompt, **h):
    body = chat_body(prompt, response_format={"type": "json_object"})
    return await gw.client.post(
        "/v1/chat/completions",
        json=body,
        headers={**gw.auth(env.stack.key), "x-task-type": "classification", **h},
    )


async def test_an_answer_that_breaks_the_task_types_schema_goes_to_the_next_model(
    client, env, gw, mock_llm
):
    prompt, mid_label, tiny_label = await prompt_where_they_differ(mock_llm)
    schema = {"type": "object", "required": ["label"],
              "properties": {"label": {"enum": [tiny_label]}}}  # fmt: skip
    await add_check(client, env, "json_schema", {"schema": schema})
    await go_live(client, env, "mid", "tiny")
    await gw.refresh()

    r = await ask_json(gw, env, prompt)
    assert r.status_code == 200
    assert answered_by(r) == "mock-tiny"  # mock-mid answered `mid_label`, which the schema forbids
    assert json.loads(r.json()["choices"][0]["message"]["content"])["label"] == tiny_label
    assert gw.recorder.last.fallbacks == 1
    assert mid_label != tiny_label


async def test_a_valid_answer_is_not_second_guessed(client, env, gw, mock_llm):
    prompt, mid_label, _ = await prompt_where_they_differ(mock_llm)
    schema = {"type": "object", "required": ["label"],
              "properties": {"label": {"enum": [mid_label]}}}  # fmt: skip
    await add_check(client, env, "json_schema", {"schema": schema})
    await go_live(client, env, "mid", "tiny")
    await gw.refresh()
    r = await ask_json(gw, env, prompt)
    assert answered_by(r) == "mock-mid" and gw.recorder.last.fallbacks == 0


async def test_the_last_model_of_the_chain_is_never_second_guessed(client, env, gw, mock_llm):
    prompt, mid_label, tiny_label = await prompt_where_they_differ(mock_llm)
    schema = {"type": "object", "required": ["label"], "properties": {"label": {"enum": ["nope"]}}}
    await add_check(client, env, "json_schema", {"schema": schema})
    await go_live(client, env, "mid", "tiny")
    await gw.refresh()
    r = await ask_json(gw, env, prompt)
    assert r.status_code == 200 and answered_by(r) == "mock-tiny"  # an answer is still an answer
    assert gw.recorder.last.fallbacks == 1
    assert mid_label and tiny_label


async def test_a_streamed_request_is_not_validated(client, env, gw, mock_llm):
    schema = {"type": "object", "required": ["label"], "properties": {"label": {"enum": ["nope"]}}}
    await add_check(client, env, "json_schema", {"schema": schema})
    await go_live(client, env, "mid", "tiny")
    await gw.refresh()
    r = await ask(gw, env, stream=True)
    assert answered_by(r) == "mock-mid" and gw.recorder.last.fallbacks == 0
