import json
import uuid
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.messaging import PermanentError, TemporaryError
from app.core.messaging.envelope import H_RETRY, H_SCHEMA, InvalidMessage, Message
from app.core.messaging.retry import decide
from app.core.messaging.topology import QueueNames


# ---- failure decisions ----
def test_temporary_error_retries_with_the_next_queue():
    d = decide(TemporaryError("busy"), attempt=0, max_retries=3)
    assert (d.action, d.next_attempt) == ("retry", 1)
    assert decide(TemporaryError(), attempt=2, max_retries=3).next_attempt == 3


def test_temporary_error_is_dead_lettered_when_retries_run_out():
    d = decide(TemporaryError(), attempt=3, max_retries=3)
    assert (d.action, d.reason) == ("dead_letter", "retries_exhausted")


def test_permanent_error_goes_straight_to_the_dlq():
    assert decide(PermanentError("bad"), 0, 3).reason == "permanent"
    assert decide(PermanentError("bad"), 0, 3).action == "dead_letter"


def test_invalid_message_and_unknown_exceptions_are_dead_lettered():
    assert decide(InvalidMessage("x"), 0, 3).reason == "invalid_message"
    for exc in (ValueError("x"), KeyError("k"), RuntimeError("boom"), ZeroDivisionError()):
        d = decide(exc, 0, 3)
        assert (d.action, d.reason) == ("dead_letter", "unexpected")


@given(st.integers(min_value=0, max_value=20), st.integers(min_value=0, max_value=10))
def test_a_temporary_error_never_retries_beyond_the_configured_queues(attempt, max_retries):
    d = decide(TemporaryError(), attempt, max_retries)
    if d.action == "retry":
        assert 1 <= d.next_attempt <= max_retries
        assert d.next_attempt == attempt + 1
    else:
        assert attempt >= max_retries and d.reason == "retries_exhausted"


@given(st.integers(min_value=0, max_value=20), st.integers(min_value=0, max_value=10))
def test_the_retry_chain_always_ends_in_the_dlq(start, max_retries):
    """Following the decisions from any attempt terminates: no endless retry loop."""
    attempt, steps = min(start, max_retries), 0
    while True:
        d = decide(TemporaryError(), attempt, max_retries)
        if d.action == "dead_letter":
            break
        attempt = d.next_attempt
        steps += 1
        assert steps <= max_retries


# ---- envelope ----
def incoming(**kw):
    defaults = {
        "message_id": str(uuid.uuid4()),
        "body": b'{"a": 1}',
        "type": "request.completed",
        "correlation_id": "corr-1",
        "headers": {H_SCHEMA: 1},
    }
    return SimpleNamespace(**{**defaults, **kw})


def test_message_roundtrip_keeps_identity_and_content():
    original = Message(type="rules.changed", payload={"version": 3}, correlation_id="c-9")
    amqp = original.to_amqp()
    assert amqp.message_id == original.message_id and amqp.correlation_id == "c-9"
    assert amqp.type == "rules.changed" and amqp.delivery_mode == 2  # persistent
    assert json.loads(amqp.body) == {"version": 3}
    assert amqp.headers[H_SCHEMA] == 1

    back = Message.from_incoming(
        incoming(
            message_id=amqp.message_id,
            body=amqp.body,
            type=amqp.type,
            correlation_id=amqp.correlation_id,
            headers=amqp.headers,
        )
    )
    assert (back.message_id, back.type, back.payload) == (
        original.message_id,
        "rules.changed",
        {"version": 3},
    )


def test_message_ids_are_unique_uuids():
    ids = {Message(type="t", payload={}).message_id for _ in range(100)}
    assert len(ids) == 100 and all(uuid.UUID(i) for i in ids)


def test_attempt_counter_comes_from_the_header():
    assert Message(type="t", payload={}).attempt == 0
    assert Message(type="t", payload={}, headers={H_RETRY: 2}).attempt == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"message_id": None},
        {"message_id": "not-a-uuid"},
        {"body": b"{broken"},
        {"body": b""},
    ],
)
def test_undecodable_messages_are_rejected_as_invalid(bad):
    with pytest.raises(InvalidMessage):
        Message.from_incoming(incoming(**bad))


def test_extra_headers_are_added_without_changing_the_message():
    msg = Message(type="t", payload={})
    amqp = msg.to_amqp({H_RETRY: 2, "x-error-reason": "permanent"})
    assert amqp.headers[H_RETRY] == 2 and amqp.headers["x-error-reason"] == "permanent"
    assert msg.headers == {}  # the original object is untouched


# ---- queue names ----
def test_queue_names():
    names = QueueNames("worker.request_log")
    assert names.dlq == "worker.request_log.dlq"
    assert names.retry(2) == "worker.request_log.retry.2"
