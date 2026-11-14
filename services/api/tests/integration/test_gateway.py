"""The gateway through its ASGI app, with a real mock provider, PostgreSQL and Redis."""

import json
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import event

from tests.integration.conftest import MOCK_KEY, PASSWORD, Endpoint, MockServer
from tests.integration.gateway_support import (
    GatewayHandle,
    Provisioned,
    chat_body,
    provision,
    run_gateway,
)
from tests.integration.test_auth_api import bearer, login

pytestmark = pytest.mark.integration

LABELS = {"positive", "negative", "neutral"}


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
async def stack(client, admin, mock_llm: MockServer) -> Provisioned:
    return await provision(client, admin, mock_llm.base_url)


@pytest.fixture
async def gw(app_settings, stack) -> AsyncIterator[GatewayHandle]:
    async for handle in run_gateway(app_settings):
        yield handle


async def post(gw: GatewayHandle, key: str, body=None, **kw):
    return await gw.client.post(
        "/v1/chat/completions", json=body or chat_body(), headers=gw.auth(key), **kw
    )


# ---- the happy path ----------------------------------------------------------------------------------
async def test_a_client_that_only_changed_base_url_gets_an_openai_answer(gw, stack):
    r = await post(gw, stack.key, chat_body(model="gpt-4o"))
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "mock-strong"  # the project's reference model answered
    assert data["choices"][0]["message"]["content"] in LABELS
    usage = data["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"] > 0
    # transparency headers
    assert uuid.UUID(r.headers["x-request-id"])
    assert r.headers["x-switchyard-model"] == "mock-strong"
    assert r.headers["x-switchyard-mode"] == "record"


async def test_the_request_is_recorded_with_metadata_only(gw, stack):
    r = (
        await post(gw, stack.key, chat_body("a secret prompt"), headers=None)
        if False
        else await gw.client.post(
            "/v1/chat/completions",
            json=chat_body("a secret prompt"),
            headers={**gw.auth(stack.key), "x-task-type": "classification"},
        )
    )
    rec = gw.recorder.last
    assert rec.request_id == r.headers["x-request-id"]
    assert (rec.status, rec.http_status, rec.stream) == ("ok", 200, False)
    assert (rec.model, rec.provider, rec.requested_model) == ("mock-strong", "mock", "gpt-4o")
    assert rec.project_id == stack.project["id"] and rec.key_id == stack.key_id
    assert rec.task_type == "classification" and rec.mode == "record"
    usage = r.json()["usage"]
    assert (rec.prompt_tokens, rec.completion_tokens) == (
        usage["prompt_tokens"],
        usage["completion_tokens"],
    )
    assert (
        rec.ttft_ms is not None and rec.duration_ms >= rec.ttft_ms and rec.overhead_ms is not None
    )
    assert "secret prompt" not in repr(rec)  # no content in the record


async def test_the_provider_key_is_sent_decrypted_and_never_returned(gw, stack):
    # the mock only answers with the right key, so a 200 proves it was decrypted and sent
    r = await post(gw, stack.key)
    assert r.status_code == 200
    assert MOCK_KEY not in r.text and MOCK_KEY not in str(dict(r.headers))


async def test_default_task_type_of_the_key_is_used(client, admin, app_settings, mock_llm):
    stack = await provision(
        client, admin, mock_llm.base_url, key_settings={"default_task_type": "extraction"}
    )
    async for gw in run_gateway(app_settings):
        await post(gw, stack.key)
        assert gw.recorder.last.task_type == "extraction"
        header = await gw.client.post(
            "/v1/chat/completions",
            json=chat_body(),
            headers={**gw.auth(stack.key), "x-task-type": "summarization"},
        )
        assert header.status_code == 200 and gw.recorder.last.task_type == "summarization"
        bad = await gw.client.post(
            "/v1/chat/completions",
            json=chat_body(),
            headers={**gw.auth(stack.key), "x-task-type": "Not Valid!"},
        )
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_task_type"


# ---- streaming ------------------------------------------------------------------------------------------
def parse_events(text: str) -> list[str]:
    return [line[6:] for line in text.splitlines() if line.startswith("data: ")]


async def test_stream_equals_the_non_stream_answer_and_hides_the_injected_usage(gw, stack):
    plain = (await post(gw, stack.key, chat_body("stream me"))).json()
    r = await post(gw, stack.key, chat_body("stream me", stream=True))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-switchyard-model"] == "mock-strong"
    events = parse_events(r.text)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == plain["choices"][0]["message"]["content"]
    assert all("usage" not in c for c in chunks)  # the client did not ask for it
    # ... but the gateway still knows the token counts
    rec = gw.recorder.last
    assert rec.stream and rec.status == "ok"
    assert rec.prompt_tokens == plain["usage"]["prompt_tokens"]
    assert rec.completion_tokens == plain["usage"]["completion_tokens"]


async def test_a_client_that_asked_for_usage_gets_it_in_the_stream(gw, stack):
    plain = (await post(gw, stack.key, chat_body("with usage"))).json()
    r = await post(
        gw, stack.key, chat_body("with usage", stream=True, stream_options={"include_usage": True})
    )
    events = parse_events(r.text)
    last = json.loads(events[-2])
    assert last["choices"] == [] and last["usage"] == plain["usage"]
    assert events[-1] == "[DONE]"


async def test_tool_calls_pass_through_in_both_modes(gw, stack):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]
    plain = (await post(gw, stack.key, chat_body("find cats", tools=tools))).json()
    call = plain["choices"][0]["message"]["tool_calls"][0]["function"]
    assert plain["choices"][0]["finish_reason"] == "tool_calls" and call["name"] == "search"

    r = await post(gw, stack.key, chat_body("find cats", tools=tools, stream=True))
    name, args = "", ""
    for e in parse_events(r.text)[:-1]:
        for c in json.loads(e)["choices"]:
            for tc in c["delta"].get("tool_calls", []):
                name += tc["function"].get("name", "")
                args += tc["function"].get("arguments", "")
    assert (name, args) == (call["name"], call["arguments"])


async def test_json_mode_request_fields_reach_the_provider(gw, stack):
    r = await post(gw, stack.key, chat_body(response_format={"type": "json_object"}))
    assert json.loads(r.json()["choices"][0]["message"]["content"])["label"] in LABELS


# ---- authentication and limits ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Basic abc"},
        {"authorization": "Bearer "},
        {"authorization": "Bearer not-a-switchyard-key"},
        {"authorization": "Bearer swy_AAAAAAAA_" + "b" * 32},  # unknown prefix
    ],
)
async def test_bad_credentials_are_401_in_openai_format(gw, headers):
    r = await gw.client.post("/v1/chat/completions", json=chat_body(), headers=headers)
    assert r.status_code == 401
    error = r.json()["error"]
    assert error["code"] == "invalid_api_key" and error["type"] == "invalid_request_error"
    assert r.headers["www-authenticate"] == "Bearer" and uuid.UUID(r.headers["x-request-id"])
    assert gw.recorder.last.status == "unauthorized"


async def test_a_wrong_secret_with_a_known_prefix_is_rejected(gw, stack):
    prefix = stack.key.split("_")[1]
    r = await post(gw, f"swy_{prefix}_" + "x" * 32)
    assert r.status_code == 401


async def test_revoked_keys_stop_working_after_the_snapshot_refresh(client, gw, stack, admin):
    assert (await post(gw, stack.key)).status_code == 200
    url = f"/projects/{stack.project['id']}/keys/{stack.key_id}/revoke"
    assert (await client.post(url, headers=admin)).status_code == 200
    assert (await post(gw, stack.key)).status_code == 200  # still in the snapshot ...
    await gw.refresh()
    assert (await post(gw, stack.key)).status_code == 401  # ... until it is refreshed


async def test_a_new_key_works_after_the_refresh(client, gw, stack, admin):
    created = await client.post(
        f"/projects/{stack.project['id']}/keys", json={"name": "second"}, headers=admin
    )
    new_key = created.json()["key"]
    assert (await post(gw, new_key)).status_code == 401  # not known yet
    await gw.refresh()
    assert (await post(gw, new_key)).status_code == 200


async def test_rate_limit_per_key_with_headers(client, admin, app_settings, mock_llm):
    stack = await provision(client, admin, mock_llm.base_url, key_settings={"rate_limit_rpm": 3})
    async for gw in run_gateway(app_settings):
        remaining = []
        for _ in range(3):
            r = await post(gw, stack.key)
            assert r.status_code == 200 and r.headers["x-ratelimit-limit-requests"] == "3"
            remaining.append(int(r.headers["x-ratelimit-remaining-requests"]))
        assert remaining == [2, 1, 0]
        blocked = await post(gw, stack.key)
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limit_exceeded"
        assert 1 <= int(blocked.headers["retry-after"]) <= 60
        assert gw.recorder.last.status == "rate_limited"


async def test_default_rate_limit_applies_to_keys_without_their_own(
    client, admin, app_settings, mock_llm
):
    stack = await provision(client, admin, mock_llm.base_url)
    async for gw in run_gateway(app_settings, gateway_default_rpm=2):
        assert (await post(gw, stack.key)).status_code == 200
        assert (await post(gw, stack.key)).status_code == 200
        assert (await post(gw, stack.key)).status_code == 429


async def test_oversized_and_malformed_bodies(client, admin, app_settings, mock_llm):
    stack = await provision(client, admin, mock_llm.base_url)
    async for gw in run_gateway(app_settings, gateway_max_body_bytes=2000):
        big = await post(gw, stack.key, chat_body("x" * 5000))
        assert big.status_code == 413 and big.json()["error"]["code"] == "request_too_large"

        async def chunks():  # no Content-Length: the limit is enforced while reading
            for _ in range(10):
                yield b"x" * 500

        streamed = await gw.client.post(
            "/v1/chat/completions", content=chunks(), headers=gw.auth(stack.key)
        )
        assert streamed.status_code == 413

        for raw in (b"{broken", b"[1]", b""):
            bad = await gw.client.post(
                "/v1/chat/completions", content=raw, headers=gw.auth(stack.key)
            )
            assert bad.status_code == 400 and bad.json()["error"]["type"] == "invalid_request_error"


async def test_unknown_routes_answer_in_openai_format(gw):
    r = await gw.client.get("/v1/does-not-exist")
    assert r.status_code == 404 and "error" in r.json()


# ---- models and project configuration ------------------------------------------------------------------------
async def test_models_list_needs_a_key_and_offers_auto(gw, stack):
    assert (await gw.client.get("/v1/models")).status_code == 401
    r = await gw.client.get("/v1/models", headers=gw.auth(stack.key))
    ids = [m["id"] for m in r.json()["data"]]
    assert r.json()["object"] == "list" and ids == ["auto", "mock-strong"]


async def test_a_project_without_a_reference_model_gets_503(client, admin, app_settings, mock_llm):
    stack = await provision(client, admin, mock_llm.base_url, reference=False)
    async for gw in run_gateway(app_settings):
        r = await post(gw, stack.key)
        assert r.status_code == 503 and r.json()["error"]["code"] == "no_reference_model"


async def test_disabling_the_reference_model_takes_effect_after_the_refresh(
    client, gw, stack, admin
):
    assert (await post(gw, stack.key)).status_code == 200
    await client.patch(f"/models/{stack.model['id']}", json={"enabled": False}, headers=admin)
    await gw.refresh()
    assert (await post(gw, stack.key)).status_code == 503


# ---- provider failures ----------------------------------------------------------------------------------------
async def test_provider_429_is_passed_through_with_retry_after(gw, stack):
    r = await gw.client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={**gw.auth(stack.key), "x-mock-fail": "429"},
    )
    assert r.status_code == 429 and r.headers["retry-after"] == "1"
    assert r.json()["error"]["code"] == "rate_limit_exceeded"  # the provider's own error body
    assert gw.recorder.last.status == "rate_limited"


@pytest.mark.parametrize("code", ["500", "503"])
async def test_provider_5xx_becomes_502_without_leaking_its_body(gw, stack, code):
    r = await gw.client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={**gw.auth(stack.key), "x-mock-fail": code},
    )
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error" and "failing on purpose" not in r.text
    assert gw.recorder.last.status == "upstream_error"


async def test_provider_4xx_about_the_request_is_passed_through(
    client, admin, app_settings, mock_llm
):
    stack = await provision(
        client, admin, mock_llm.base_url, model="mock-tiny", context_window=4096
    )
    async for gw in run_gateway(app_settings):
        r = await post(gw, stack.key, chat_body("x" * 30000))
        assert r.status_code == 400  # too long for mock-tiny: the client's problem
        assert r.json()["error"]["code"] == "context_length_exceeded"


async def test_a_rejected_provider_key_is_not_reported_as_the_clients_401(
    client, admin, app_settings, mock_llm
):
    stack = await provision(client, admin, mock_llm.base_url, provider_key="wrong-provider-key")
    async for gw in run_gateway(app_settings):
        r = await post(gw, stack.key)
        assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_error"


async def test_unreachable_provider_is_502(client, admin, app_settings, mock_llm):
    dead = f"http://localhost:{mock_llm.port + 1}/v1"
    stack = await provision(client, admin, dead)
    async for gw in run_gateway(app_settings):
        r = await post(gw, stack.key)
        assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unreachable"


async def test_slow_provider_times_out_with_504(client, admin, app_settings, mock_llm):
    stack = await provision(client, admin, mock_llm.base_url)
    async for gw in run_gateway(
        app_settings, gateway_total_timeout=0.4, gateway_first_byte_timeout=0.4
    ):
        slow = {**gw.auth(stack.key), "x-mock-fail": "timeout"}
        plain = await gw.client.post("/v1/chat/completions", json=chat_body(), headers=slow)
        assert plain.status_code == 504 and plain.json()["error"]["code"] == "upstream_timeout"
        streamed = await gw.client.post(
            "/v1/chat/completions", json=chat_body(stream=True), headers=slow
        )
        assert streamed.status_code == 504
        assert gw.recorder.last.http_status == 504


async def test_too_many_concurrent_requests_to_a_provider_get_503(
    client, admin, app_settings, mock_llm
):
    import asyncio

    stack = await provision(client, admin, mock_llm.base_url)
    async for gw in run_gateway(
        app_settings,
        gateway_provider_max_concurrency=1,
        gateway_provider_queue_timeout=0.2,
        gateway_total_timeout=1.5,
    ):
        hanging = asyncio.create_task(
            gw.client.post(
                "/v1/chat/completions",
                json=chat_body(),
                headers={**gw.auth(stack.key), "x-mock-fail": "timeout"},
            )
        )
        await asyncio.sleep(0.3)  # the first request now holds the only slot
        r = await post(gw, stack.key)
        assert r.status_code == 503 and r.json()["error"]["code"] == "provider_overloaded"
        assert (await hanging).status_code == 504  # it ends by the total timeout and frees the slot
        assert (await post(gw, stack.key)).status_code == 200  # the slot was released


# ---- the database is not on the hot path --------------------------------------------------------------------------
async def test_requests_cause_no_database_statements(gw, stack):
    import asyncio

    # stop the periodic refresh: it legitimately reads the database, the requests must not
    gw.app.state.proxy.store.stop()
    await asyncio.sleep(0.2)
    statements: list[str] = []
    event.listen(
        gw.app.state.engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cur, stmt, params, ctx, many: statements.append(stmt),
    )
    for i in range(10):
        assert (await post(gw, stack.key, chat_body(f"q{i}"))).status_code == 200
        assert (await post(gw, stack.key, chat_body(f"q{i}", stream=True))).status_code == 200
    assert (await gw.client.get("/v1/models", headers=gw.auth(stack.key))).status_code == 200
    assert (await gw.client.get("/health/ready")).status_code == 200
    assert statements == []  # keys, projects, routes and keys come from memory


# ---- the snapshot --------------------------------------------------------------------------------------------------
async def test_health_reports_the_snapshot(gw):
    assert (await gw.client.get("/health/live")).json() == {"status": "ok"}
    r = await gw.client.get("/health/ready")
    body = r.json()
    assert r.status_code == 200 and body["checks"] == {"snapshot": "ok"}
    assert body["snapshot"]["source"] == "database" and body["snapshot"]["version"]


async def test_redis_copy_of_the_snapshot_has_no_plaintext_provider_key(
    gw, stack, redis_ep: Endpoint
):
    redis = Redis(host=redis_ep.host, port=redis_ep.port, password=PASSWORD, decode_responses=True)
    try:
        raw = await redis.get("snapshot:current")
    finally:
        await redis.aclose()
    assert raw and "v1.k2." in raw  # the ciphertext of the provider key
    assert MOCK_KEY not in raw and "mock-provider-key" not in raw
    assert stack.key not in raw  # client keys are stored as prefix + hash only


async def test_a_gateway_started_without_the_database_uses_the_redis_copy(
    gw, stack, app_settings, pg_ep: Endpoint
):
    # `gw` already wrote the snapshot to Redis; now start another gateway with a dead database
    broken = app_settings.model_copy(update={"postgres_port": pg_ep.port + 1})
    async for second in run_gateway(broken):
        ready = (await second.client.get("/health/ready")).json()
        assert ready["status"] == "ok" and ready["snapshot"]["source"] == "redis"
        r = await post(second, stack.key)
        assert r.status_code == 200 and r.json()["model"] == "mock-strong"


async def test_a_gateway_with_neither_database_nor_redis_copy_is_not_ready(
    app_settings, pg_ep: Endpoint, redis_ep: Endpoint
):
    redis = Redis(host=redis_ep.host, port=redis_ep.port, password=PASSWORD)
    await redis.flushall()
    await redis.aclose()
    broken = app_settings.model_copy(update={"postgres_port": pg_ep.port + 1})
    async for gw in run_gateway(broken):
        r = await gw.client.get("/health/ready")
        assert r.status_code == 503 and r.json()["checks"] == {"snapshot": "missing"}
        denied = await post(gw, "swy_AAAAAAAA_" + "b" * 32)
        assert denied.status_code == 503  # no snapshot: no service, but a clean error


async def test_the_gateway_keeps_serving_when_the_database_fails_later(gw, stack, monkeypatch):
    store = gw.app.state.proxy.store
    version = store.current.snapshot.version

    class DeadSession:
        def __call__(self):
            raise ConnectionError("database is gone")

    monkeypatch.setattr(store, "_session_factory", DeadSession())
    assert await store.refresh() is False  # the refresh fails ...
    assert store.current.snapshot.version == version  # ... the old snapshot stays in use
    assert (await post(gw, stack.key)).status_code == 200


async def test_redis_outage_does_not_stop_the_gateway(
    client, admin, app_settings, mock_llm, redis_ep
):
    stack = await provision(client, admin, mock_llm.base_url)
    dead_redis = app_settings.model_copy(update={"redis_port": redis_ep.port + 1})
    async for gw in run_gateway(dead_redis):
        r = await post(
            gw, stack.key
        )  # the rate limiter fails open, the snapshot comes from the database
        assert r.status_code == 200 and "x-ratelimit-limit-requests" not in r.headers


async def test_each_request_gets_its_own_id(gw, stack):
    ids = {(await post(gw, stack.key)).headers["x-request-id"] for _ in range(5)}
    assert len(ids) == 5
    # a client-supplied id is not trusted
    r = await gw.client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={**gw.auth(stack.key), "x-request-id": "attacker-chosen"},
    )
    assert r.headers["x-request-id"] != "attacker-chosen"
    assert isinstance(httpx.Headers(r.headers), httpx.Headers)
