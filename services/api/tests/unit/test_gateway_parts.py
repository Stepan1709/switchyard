import ipaddress
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core import ssrf
from app.core.snapshot import KeyInfo, ProjectInfo, RouteTarget, Snapshot
from app.modules.gateway.body import prepare_body
from app.modules.gateway.errors import GatewayError
from app.modules.gateway.sse import SseFilter


# ---- request body -------------------------------------------------------------------------------
def prep(body: dict, model="mock-strong"):
    return prepare_body(json.dumps(body).encode(), model)


def test_model_is_replaced_and_everything_else_is_kept():
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "héllo"}],
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "tool_choice": "auto",
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "unknown_extra": {"a": [1, 2]},
    }
    p = prep(body)
    sent = json.loads(p.content)
    assert sent.pop("model") == "mock-strong" and p.requested_model == "gpt-4o"
    body.pop("model")
    assert sent == body  # tools, response_format and unknown fields travel untouched
    assert not p.stream and not p.strip_usage_chunk
    assert "héllo".encode() in p.content  # not escaped to é


def test_streams_get_include_usage_and_it_is_marked_for_removal():
    p = prep({"model": "x", "messages": [], "stream": True})
    assert json.loads(p.content)["stream_options"] == {"include_usage": True}
    assert p.stream and p.strip_usage_chunk


def test_a_client_that_asked_for_usage_keeps_its_stream_unchanged():
    p = prep(
        {"model": "x", "messages": [], "stream": True, "stream_options": {"include_usage": True}}
    )
    assert p.stream and not p.strip_usage_chunk


def test_other_stream_options_are_preserved():
    p = prep({"model": "x", "stream": True, "stream_options": {"include_obfuscation": False}})
    assert json.loads(p.content)["stream_options"] == {
        "include_obfuscation": False,
        "include_usage": True,
    }


def test_include_usage_false_is_treated_as_not_asked():
    p = prep({"model": "x", "stream": True, "stream_options": {"include_usage": False}})
    assert json.loads(p.content)["stream_options"]["include_usage"] is True and p.strip_usage_chunk


def test_non_stream_requests_get_no_stream_options():
    assert "stream_options" not in json.loads(prep({"model": "x", "messages": []}).content)


@pytest.mark.parametrize(
    "raw",
    [b"", b"{broken", b"[1,2]", b'"text"', b"null", json.dumps({"stream": "yes"}).encode()],
)
def test_bad_bodies_are_400(raw):
    with pytest.raises(GatewayError) as caught:
        prepare_body(raw, "m")
    assert (
        caught.value.status == 400
        and caught.value.body()["error"]["type"] == "invalid_request_error"
    )


def test_missing_model_is_allowed():
    assert prep({"messages": []}).requested_model is None


# ---- SSE filter ----------------------------------------------------------------------------------
def event(data) -> bytes:
    payload = data if isinstance(data, str) else json.dumps(data)
    return f"data: {payload}\n\n".encode()


CONTENT = event({"choices": [{"delta": {"content": "hi"}}]})
USAGE_ONLY = event({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
DONE = event("[DONE]")
STREAM = (
    CONTENT
    + CONTENT
    + event({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    + USAGE_ONLY
    + DONE
)


def run(chunks, strip):
    f = SseFilter(strip)
    out = b"".join(b"".join(f.feed(c)) for c in chunks) + f.finish()
    return f, out


def split(data: bytes, size: int):
    return [data[i : i + size] for i in range(0, len(data), size)]


def test_without_stripping_the_bytes_are_identical():
    f, out = run([STREAM], strip=False)
    assert out == STREAM
    assert f.usage == {"prompt_tokens": 7, "completion_tokens": 3}


def test_the_injected_usage_event_is_removed_and_usage_is_still_read():
    f, out = run([STREAM], strip=True)
    assert out == STREAM.replace(USAGE_ONLY, b"")
    assert f.usage == {"prompt_tokens": 7, "completion_tokens": 3} and f.stripped == 1


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 64, 1000])
def test_chunk_boundaries_do_not_matter(size):
    expected = STREAM.replace(USAGE_ONLY, b"")
    f, out = run(split(STREAM, size), strip=True)
    assert out == expected and f.usage["prompt_tokens"] == 7
    f2, out2 = run(split(STREAM, size), strip=False)
    assert out2 == STREAM and f2.usage["prompt_tokens"] == 7


def test_crlf_separators_are_understood():
    stream = STREAM.replace(b"\n\n", b"\r\n\r\n")
    f, out = run([stream], strip=True)
    assert f.stripped == 1 and b"prompt_tokens" not in out


def test_usage_inside_a_content_chunk_is_read_but_the_chunk_is_kept():
    mixed = event(
        {
            "choices": [{"delta": {"content": "x"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        }
    )
    f, out = run([mixed, DONE], strip=True)
    assert out == mixed + DONE and f.usage == {"prompt_tokens": 1, "completion_tokens": 2}


def test_null_usage_is_ignored():
    chunk = event({"choices": [{"delta": {"content": "x"}}], "usage": None})
    f, out = run([chunk], strip=True)
    assert out == chunk and f.usage is None


def test_the_filter_does_not_choke_on_comments_and_garbage():
    messy = b': keep-alive\n\ndata: not json at all\n\nevent: ping\ndata: {"usage": 5}\n\n' + DONE
    f, out = run([messy], strip=True)
    assert out == messy and f.usage is None  # nothing was removed, nothing crashed


def test_an_incomplete_last_event_is_released_at_the_end():
    cut = CONTENT + b'data: {"choices": [{"delta"'
    _, out = run([cut], strip=True)
    assert out == cut  # held back while incomplete, handed over by finish()


@given(st.lists(st.binary(max_size=60), max_size=8))
def test_arbitrary_bytes_pass_through_unchanged_when_not_stripping(chunks):
    f = SseFilter(False)
    out = b"".join(b"".join(f.feed(c)) for c in chunks) + f.finish()
    assert out == b"".join(chunks)


@given(st.lists(st.binary(max_size=60), max_size=8))
def test_no_bytes_are_invented_or_lost_when_stripping_nothing_matches(chunks):
    """Random bytes never look like a usage event, so stripping must keep every byte."""
    f = SseFilter(True)
    out = b"".join(b"".join(f.feed(c)) for c in chunks) + f.finish()
    assert out == b"".join(chunks)


# ---- snapshot ----------------------------------------------------------------------------------------
def make_snapshot() -> Snapshot:
    target = RouteTarget(
        "m1", "mock-strong", "p1", "mock", "http://mock_llm:8003/v1", True, "none", None, 4096
    )
    return Snapshot(
        version="abc",
        created_at="2026-01-01T00:00:00+00:00",
        keys={"AAAAAAAA": KeyInfo("k1", "AAAAAAAA", "f" * 64, "proj1", "classification", 60)},
        projects={
            "proj1": ProjectInfo("proj1", "shop", target),
            "proj2": ProjectInfo("proj2", "empty", None),
        },
        model_names=("mock-strong",),
    )


def test_snapshot_survives_a_json_roundtrip():
    snap = make_snapshot()
    back = Snapshot.from_json(snap.to_json())
    assert back == snap
    assert back.projects["proj2"].reference is None


def test_the_redis_copy_holds_no_plaintext_provider_key():
    snap = make_snapshot()
    text = snap.to_json()
    assert "api_key_encrypted" in text and "Bearer" not in text


def test_snapshot_objects_are_immutable():
    snap = make_snapshot()
    with pytest.raises(AttributeError):
        snap.version = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        snap.keys["AAAAAAAA"].rate_limit_rpm = 5  # type: ignore[misc]


# ---- DNS cache ----------------------------------------------------------------------------------------
async def test_resolver_cache_serves_repeats_until_the_ttl_expires():
    calls = []
    now = [100.0]

    async def inner(host, port):
        calls.append(host)
        return [ipaddress.ip_address("93.184.216.34")]

    resolver = ssrf.CachingResolver(30.0, inner=inner, clock=lambda: now[0])
    await resolver("a.example", 443)
    await resolver("a.example", 443)
    assert len(calls) == 1
    await resolver("b.example", 443)
    assert len(calls) == 2  # another host is a separate entry
    now[0] += 31
    await resolver("a.example", 443)
    assert len(calls) == 3  # expired: asked again


async def test_cached_answers_are_still_checked_by_the_policy():
    async def inner(host, port):
        return [ipaddress.ip_address("169.254.169.254")]

    resolver = ssrf.CachingResolver(30.0, inner=inner)
    target = ssrf.parse_base_url("https://api.example.com/v1", is_local=False, local_hosts=[])
    for _ in range(2):  # the second call uses the cache and must be refused just the same
        with pytest.raises(ssrf.SSRFError):
            await ssrf.resolve_checked(target, is_local=False, resolver=resolver)
