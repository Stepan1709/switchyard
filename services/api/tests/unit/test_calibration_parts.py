"""Pure parts of calibration: sampling, prices, replay request/answer handling, import format."""

import json
import uuid
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.messaging import PermanentError
from app.core.messaging.errors import Deferred, TemporaryError
from app.core.messaging.retry import decide
from app.modules.calibration import pricing, replay, sampling
from app.modules.calibration.executor import parse_job
from app.modules.calibration.sampling import PoolItem
from app.modules.calibration.service import content_hash, parse_labeled_line


# ---- sampling -------------------------------------------------------------------------------
def item(n: int, *, dup: int | None = None, chars: int = 100, tools: bool = False, src="traffic"):
    return PoolItem(uuid.UUID(int=n), src, f"d{dup if dup is not None else n}", chars, tools)


pool_items = st.builds(
    PoolItem,
    st.integers(0, 10_000).map(lambda n: uuid.UUID(int=n)),
    st.sampled_from(["traffic", "labeled"]),
    st.integers(0, 40).map(lambda n: f"d{n}"),
    st.integers(1, 5000),
    st.booleans(),
)
pools = st.lists(pool_items, max_size=80, unique_by=lambda i: i.request_id)
strategies = st.sampled_from(["random", "stratified"])


@given(pools, st.integers(1, 100), strategies, st.integers(0, 2**31 - 1))
def test_a_sample_is_a_duplicate_free_subset_of_the_right_size(items, n, strategy, seed):
    picked = sampling.choose(items, n, strategy, seed)
    unique_keys = {i.dedup_key for i in items}
    assert len(picked) == min(n, len(unique_keys))
    assert len({p.dedup_key for p in picked}) == len(picked)  # no duplicate prompts
    assert {p.request_id for p in picked} <= {i.request_id for i in items}


@given(pools, st.integers(1, 100), strategies, st.integers(0, 2**31 - 1))
def test_the_same_seed_gives_the_same_sample_whatever_the_input_order(items, n, strategy, seed):
    a = sampling.choose(items, n, strategy, seed)
    b = sampling.choose(list(reversed(items)), n, strategy, seed)
    assert a == b


@given(pools, st.integers(0, 2**31 - 1))
def test_stratified_keeps_every_kind_of_request_when_it_can(items, seed):
    pool = sampling.deduplicate(items)
    if not pool:
        return
    low, high = sampling._terciles([i.prompt_chars for i in pool])
    kinds = {(i.has_tools, sampling._length_bucket(i.prompt_chars, low, high)) for i in pool}
    picked = sampling.pick_stratified(pool, len(kinds), seed)
    got = {(i.has_tools, sampling._length_bucket(i.prompt_chars, low, high)) for i in picked}
    assert got == kinds  # as many picks as kinds: each kind is represented once


def test_dedup_prefers_labelled_examples_and_the_smallest_id():
    a = item(5, dup=1)
    b = item(3, dup=1)
    labelled = item(9, dup=1, src="labeled")
    assert sampling.deduplicate([a, b]) == [b]
    assert sampling.deduplicate([a, b, labelled]) == [labelled]


def test_a_sample_larger_than_the_pool_returns_the_whole_pool():
    items = [item(i) for i in range(5)]
    assert len(sampling.choose(items, 50, "random", 1)) == 5
    assert len(sampling.choose(items, 50, "stratified", 1)) == 5
    assert sampling.choose([], 3, "stratified", 1) == []


def test_stratified_follows_the_mix_of_the_pool():
    small = [item(i, chars=10) for i in range(0, 60)]  # lots of short requests without tools
    tools = [item(i, chars=10, tools=True) for i in range(100, 110)]  # a few with tools
    picked = sampling.pick_stratified(small + tools, 12, seed=7)
    with_tools = [p for p in picked if p.has_tools]
    assert len(picked) == 12
    assert 1 <= len(with_tools) <= 4  # represented, but not more than their share


# ---- pricing --------------------------------------------------------------------------------
def test_cost_of_a_per_token_request():
    price = pricing.Price("per_token", Decimal("0.50"), Decimal("1.50"))
    assert pricing.cost_usd(price, 1_000_000, 0) == Decimal("0.50000000")
    assert pricing.cost_usd(price, 1000, 2000) == Decimal("0.00350000")


def test_a_server_paid_by_the_month_has_no_marginal_cost():
    assert pricing.cost_usd(pricing.Price("fixed_monthly"), 10**6, 10**6) == 0


@given(st.integers(0, 10**7), st.integers(0, 10**7), st.integers(0, 10**7), st.integers(0, 10**7))
def test_cost_grows_with_tokens(p1, c1, extra_p, extra_c):
    price = pricing.Price("per_token", Decimal("3.25"), Decimal("9.75"))
    assert pricing.cost_usd(price, p1 + extra_p, c1 + extra_c) >= pricing.cost_usd(price, p1, c1)


def test_micro_dollars_round_up_so_a_reservation_is_never_too_small():
    assert pricing.to_micro(Decimal("0.0000001")) == 1
    assert pricing.to_micro(Decimal("0.000001")) == 1
    assert pricing.to_micro(Decimal("1.5")) == 1_500_000
    assert pricing.to_micro(Decimal(0)) == 0


def test_token_estimates():
    request = {"messages": [{"role": "user", "content": "x" * 350}]}
    assert pricing.estimate_prompt_tokens(request) >= 100
    assert pricing.estimate_prompt_tokens({"messages": []}) >= 1
    with_tools = {**request, "tools": [{"type": "function", "function": {"name": "f"}}]}
    assert pricing.estimate_prompt_tokens(with_tools) > pricing.estimate_prompt_tokens(request)
    assert pricing.requested_max_tokens({"max_tokens": 50}) == 50
    assert pricing.requested_max_tokens({"max_completion_tokens": 70, "max_tokens": 50}) == 70
    assert pricing.requested_max_tokens({"max_tokens": True}) is None
    # the recorded answer guides the guess, the request's own limit caps it
    assert pricing.estimate_completion_tokens({"max_tokens": 100}, 10, 1024) == 64
    assert pricing.estimate_completion_tokens({"max_tokens": 30}, 500, 1024) == 30
    assert pricing.estimate_completion_tokens({}, None, 1024) == 1024


# ---- replay ---------------------------------------------------------------------------------
TARGET = replay.Target("http://x/v1", True, "bearer", "k", "cand-model")


def test_the_replayed_request_keeps_everything_but_the_model_and_asks_for_a_stream():
    recorded = {
        "model": "reference",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "tool_choice": "auto",
        "temperature": 0,
    }
    body = replay.build_body(recorded, TARGET)
    assert body["model"] == "cand-model"
    assert body["messages"] == recorded["messages"]
    assert body["tools"] == recorded["tools"] and body["tool_choice"] == "auto"
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert recorded["model"] == "reference"  # the recorded request itself is untouched


def test_a_candidate_without_streaming_gets_a_plain_request():
    target = replay.Target("http://x/v1", True, "none", None, "m", supports_stream=False)
    body = replay.build_body({"messages": [], "stream": True, "stream_options": {"x": 1}}, target)
    assert body["stream"] is False and "stream_options" not in body


def test_stream_options_of_the_client_are_kept_and_usage_is_forced_on():
    body = replay.build_body({"messages": [], "stream_options": {"foo": 1}}, TARGET)
    assert body["stream_options"] == {"foo": 1, "include_usage": True}


@pytest.mark.parametrize(
    ("status", "body", "kind", "code"),
    [
        (429, b"", "temporary", "rate_limited"),
        (500, b"", "temporary", "http_500"),
        (503, b"", "temporary", "http_503"),
        (401, b"", "error", "authentication_failed"),
        (404, b"", "error", "model_not_found"),
        (
            400,
            json.dumps(
                {"error": {"code": "context_length_exceeded", "message": "too long"}}
            ).encode(),
            "context_exceeded",
            "context_length_exceeded",
        ),
        (
            400,
            b'{"error":{"message":"maximum context length is 4096 tokens"}}',
            "context_exceeded",
            None,
        ),
        (
            400,
            b'{"error":{"message":"content policy violation"}}',
            "refused",
            "rejected_by_provider",
        ),
        (422, b"not json at all", "refused", "rejected_by_provider"),
        (418, b"", "error", "http_418"),
    ],
)
def test_provider_errors_are_classified(status, body, kind, code):
    outcome = replay.classify_status(status, body)
    assert outcome.kind == kind
    if code:
        assert outcome.error_code == code


def test_a_good_answer_with_usage_keeps_the_providers_numbers():
    response = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
    }
    out = replay.finish_outcome(
        response,
        {"prompt_tokens": 7, "completion_tokens": 3},
        {"messages": []},
        ttft_ms=12.0,
        latency_ms=40.0,
        http_status=200,
    )
    assert (out.kind, out.prompt_tokens, out.completion_tokens) == ("ok", 7, 3)
    assert not out.tokens_estimated and not out.has_tool_calls and out.finish_reason == "stop"


def test_missing_usage_is_estimated_and_flagged():
    calls = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    response = {
        "choices": [
            {"message": {"role": "assistant", "content": None, "tool_calls": calls},
             "finish_reason": "tool_calls"}
        ]
    }  # fmt: skip
    out = replay.finish_outcome(
        response, None, {"messages": [{"role": "user", "content": "hello"}]},
        ttft_ms=None, latency_ms=5.0, http_status=200,
    )  # fmt: skip
    assert out.tokens_estimated and out.has_tool_calls
    assert out.prompt_tokens and out.completion_tokens


# ---- messaging / jobs -----------------------------------------------------------------------
def test_deferral_does_not_use_up_a_retry():
    deferred = decide(Deferred("busy"), attempt=2, max_retries=3)
    assert (deferred.action, deferred.next_attempt) == ("defer", 2)
    failed = decide(TemporaryError("x"), attempt=2, max_retries=3)
    assert (failed.action, failed.next_attempt) == ("retry", 3)


def test_a_job_payload_is_validated():
    ids = {
        "run_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4()),
        "model_id": str(uuid.uuid4()),
    }
    assert parse_job(ids).run_id == uuid.UUID(ids["run_id"])
    for bad in ({}, {**ids, "run_id": "nope"}, None, {"run_id": 1}):
        with pytest.raises(PermanentError):
            parse_job(bad)


# ---- labelled import ------------------------------------------------------------------------
def line(**kw):
    return json.dumps({"messages": [{"role": "user", "content": "q"}], "expected": "a", **kw})


def test_a_labelled_line_becomes_a_request_and_an_expected_answer():
    parsed = parse_labeled_line(1, line(model="ignored", stream=True, temperature=0))
    assert parsed["expected"] == {"content": "a"}
    assert parsed["request_payload"] == {
        "messages": [{"role": "user", "content": "q"}],
        "temperature": 0,
    }  # `model` and `stream` belong to the replay, not to the example


def test_expected_may_be_a_tool_call():
    parsed = parse_labeled_line(
        1, line(expected={"tool_calls": [{"name": "f", "arguments": {"a": 1}}]})
    )
    assert parsed["expected"]["tool_calls"][0]["name"] == "f"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        json.dumps({"expected": "a"}),
        json.dumps({"messages": [], "expected": "a"}),
        json.dumps({"messages": ["x"], "expected": "a"}),
        json.dumps({"messages": [{"role": "user", "content": "q"}]}),
        json.dumps({"messages": [{"role": "user", "content": "q"}], "expected": 5}),
        json.dumps({"messages": [{"role": "user", "content": "q"}], "expected": {"x": 1}}),
    ],
)
def test_malformed_labelled_lines_are_rejected_with_their_number(raw):
    with pytest.raises(ValueError, match="line 7"):
        parse_labeled_line(7, raw)


def test_equal_requests_have_equal_hashes_whatever_the_key_order():
    a = {"messages": [{"role": "user", "content": "q"}], "temperature": 0}
    b = {"temperature": 0, "messages": [{"content": "q", "role": "user"}]}
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash({**a, "temperature": 1})
