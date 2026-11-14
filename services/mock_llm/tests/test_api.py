import asyncio
import json
import time

import httpx
import pytest

from app.config import Settings
from app.main import create_app


def make_client(**overrides):
    settings = Settings(**{"time_scale": 0, **overrides})
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://mock"
    )


@pytest.fixture
async def client():
    async with make_client() as c:
        yield c


def body(**kw):
    return {"model": "mock-strong", "messages": [{"role": "user", "content": "great stuff"}], **kw}


async def sse_events(response):
    events = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(line[6:])
    return events


async def test_health(client):
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    assert (await client.get("/health/ready")).status_code == 200


async def test_models_list(client):
    data = (await client.get("/v1/models")).json()
    assert data["object"] == "list"
    ids = {m["id"] for m in data["data"]}
    assert ids == {"mock-tiny", "mock-mid", "mock-strong", "mock-judge"}


async def test_non_stream_completion_shape_and_usage(client):
    r = await client.post("/v1/chat/completions", json=body())
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion" and data["model"] == "mock-strong"
    choice = data["choices"][0]
    assert choice["message"]["role"] == "assistant" and choice["finish_reason"] == "stop"
    usage = data["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"] > 0


async def test_stream_matches_non_stream_and_ends_with_done(client):
    payload = body()
    full = (await client.post("/v1/chat/completions", json=payload)).json()
    async with client.stream("POST", "/v1/chat/completions", json={**payload, "stream": True}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        events = await sse_events(r)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == full["choices"][0]["message"]["content"]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert all("usage" not in c for c in chunks)  # no usage unless requested


async def test_stream_usage_only_with_include_usage(client):
    payload = body(stream=True, stream_options={"include_usage": True})
    async with client.stream("POST", "/v1/chat/completions", json=payload) as r:
        events = await sse_events(r)
    last = json.loads(events[-2])
    assert last["choices"] == [] and last["usage"]["completion_tokens"] > 0


async def test_long_stream_has_many_chunks(client):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json=body(stream=True),
        headers={"x-mock-output-tokens": "60"},
    ) as r:
        events = await sse_events(r)
    assert len(events) > 20


TOOLS = [
    {
        "type": "function",
        "function": {"name": "search", "parameters": {"properties": {"query": {"type": "string"}}}},
    }
]


async def test_tool_calls_non_stream_and_stream_agree(client):
    payload = body(tools=TOOLS)
    full = (await client.post("/v1/chat/completions", json=payload)).json()
    msg = full["choices"][0]["message"]
    assert full["choices"][0]["finish_reason"] == "tool_calls" and msg["content"] is None
    expected = msg["tool_calls"][0]["function"]

    async with client.stream("POST", "/v1/chat/completions", json={**payload, "stream": True}) as r:
        events = await sse_events(r)
    name, args = "", ""
    for e in events[:-1]:
        for c in json.loads(e)["choices"]:
            for tc in c["delta"].get("tool_calls", []):
                name += tc["function"].get("name", "")
                args += tc["function"].get("arguments", "")
    assert (name, args) == (expected["name"], expected["arguments"])


async def test_context_length_exceeded_is_an_openai_style_400(client):
    huge = {"model": "mock-tiny", "messages": [{"role": "user", "content": "x" * 30000}]}
    r = await client.post("/v1/chat/completions", json=huge)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "context_length_exceeded"
    # the same prompt fits the larger model
    ok = await client.post("/v1/chat/completions", json={**huge, "model": "mock-strong"})
    assert ok.status_code == 200


async def test_unknown_model_and_invalid_body(client):
    r = await client.post("/v1/chat/completions", json=body(model="gpt-nope"))
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    bad = await client.post("/v1/chat/completions", json={"model": "mock-tiny"})
    assert bad.status_code == 400 and bad.json()["error"]["type"] == "invalid_request_error"


async def test_unknown_request_fields_are_ignored(client):
    r = await client.post("/v1/chat/completions", json=body(temperature=0.2, user="u1", seed=1))
    assert r.status_code == 200


async def test_api_key_is_enforced_when_configured():
    async with make_client(api_key="sk-secret") as c:
        assert (await c.post("/v1/chat/completions", json=body())).status_code == 401
        wrong = await c.get("/v1/models", headers={"authorization": "Bearer nope"})
        assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "invalid_api_key"
        ok = await c.post(
            "/v1/chat/completions", json=body(), headers={"authorization": "Bearer sk-secret"}
        )
        assert ok.status_code == 200
        assert (await c.get("/health/live")).status_code == 200  # probes need no key


@pytest.mark.parametrize(("code", "status"), [("500", 500), ("503", 503), ("429", 429)])
async def test_failure_injection(client, code, status):
    r = await client.post("/v1/chat/completions", json=body(), headers={"x-mock-fail": code})
    assert r.status_code == status
    assert "error" in r.json()
    if code == "429":
        assert r.headers["retry-after"] == "1"


async def test_timeout_injection_hangs_until_the_client_gives_up():
    async with make_client(timeout_seconds=30) as c:
        call = c.post("/v1/chat/completions", json=body(), headers={"x-mock-fail": "timeout"})
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(call, 0.3)


async def test_delay_header_is_scaled_by_time_scale():
    async with make_client(time_scale=1.0) as c:
        started = time.perf_counter()
        r = await c.post("/v1/chat/completions", json=body(), headers={"x-mock-delay-ms": "250"})
        assert r.status_code == 200 and time.perf_counter() - started >= 0.25


async def test_stream_can_be_cut_in_the_middle(client):
    received = []
    with pytest.raises(Exception):  # noqa: B017 - any abort of the stream counts
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json=body(stream=True),
            headers={"x-mock-output-tokens": "60", "x-mock-drop-after-chunks": "3"},
        ) as r:
            async for line in r.aiter_lines():
                received.append(line)
    assert "data: [DONE]" not in received


async def test_max_tokens_gives_finish_reason_length(client):
    r = await client.post(
        "/v1/chat/completions", json=body(max_tokens=3), headers={"x-mock-output-tokens": "100"}
    )
    assert r.json()["choices"][0]["finish_reason"] == "length"
    assert r.json()["usage"]["completion_tokens"] <= 3


async def test_stats_count_completed_and_aborted_streams(client):
    async with client.stream("POST", "/v1/chat/completions", json=body(stream=True)) as r:
        async for _ in r.aiter_lines():
            pass
    before = (await client.get("/_stats")).json()
    assert before["streams_started"] == 1 and before["streams_completed"] == 1

    with pytest.raises(Exception):  # noqa: B017 - the stream is cut on purpose
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json=body(stream=True),
            headers={"x-mock-output-tokens": "60", "x-mock-drop-after-chunks": "2"},
        ) as r:
            async for _ in r.aiter_lines():
                pass
    after = (await client.get("/_stats")).json()
    assert after["streams_aborted"] == 1 and after["requests"] >= 2
