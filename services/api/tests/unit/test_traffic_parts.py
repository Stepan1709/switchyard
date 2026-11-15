import json
import uuid
from datetime import UTC, date, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.core.events import RequestCompleted
from app.modules.gateway.events import TrafficEventPublisher, build_event
from app.modules.gateway.pii import CARD, EMAIL, PHONE, luhn_valid, mask_json, mask_text
from app.modules.gateway.record import RequestRecord
from app.modules.gateway.sse import SseFilter, StreamAssembler
from app.modules.traffic import partitions
from app.modules.traffic.service import InvalidCursor, decode_cursor, encode_cursor


# ---- PII masking -----------------------------------------------------------------------------------
def test_emails_are_masked():
    assert mask_text("write to john.doe+tag@example.co.uk, thanks") == f"write to {EMAIL}, thanks"
    assert mask_text("a@b.io and c@d.org") == f"{EMAIL} and {EMAIL}"


@pytest.mark.parametrize(
    "text",
    [
        "+7 (999) 123-45-67",
        "8-800-555-35-35",
        "+1 415 555 2671",
        "79991234567",
        "(415) 555-2671",
        "+44 20 7946 0958",
    ],
)
def test_phone_numbers_are_masked(text):
    assert mask_text(f"call {text} now") == f"call {PHONE} now"


@pytest.mark.parametrize(
    "text",
    ["4111 1111 1111 1111", "4111-1111-1111-1111", "4111111111111111", "5500 0000 0000 0004"],
)
def test_valid_card_numbers_are_masked(text):
    assert mask_text(f"card {text}.") == f"card {CARD}."


@pytest.mark.parametrize(
    "text",
    [
        "4111 1111 1111 1112",  # fails the Luhn check
        "order 1234567890123456",  # 16 digits, not Luhn-valid
        "ts 1731700000000",  # a millisecond timestamp
        "id 20261115123456",
        "date 2026-11-15 at 12:30",
        "logged 2026-11-15 12:30:45 UTC",
        "price 1 234 567.89 USD",
        "version 3.12.16",
        "room 404",
    ],
)
def test_ordinary_numbers_are_left_alone(text):
    assert mask_text(text) == text


def test_cards_are_found_before_phone_patterns_can_claim_them():
    assert mask_text("pay with 4111 1111 1111 1111 or +7 999 123 45 67") == (
        f"pay with {CARD} or {PHONE}"
    )


def test_json_values_are_masked_but_keys_and_numbers_are_not():
    data = {
        "email": "a@b.io",
        "n": 42,
        "ok": True,
        "none": None,
        "messages": [{"role": "user", "content": "I am bob@mail.com"}],
        "args": json.dumps({"phone": "+7 999 123 45 67"}),
    }
    out = mask_json(data)
    assert out["email"] == EMAIL and out["n"] == 42 and out["ok"] is True and out["none"] is None
    assert out["messages"][0]["content"] == f"I am {EMAIL}" and "email" in out
    assert PHONE in out["args"]
    assert data["email"] == "a@b.io"  # the original is not modified


@given(st.text(max_size=200))
def test_masking_is_idempotent_and_never_crashes(text):
    once = mask_text(text)
    assert mask_text(once) == once


@given(st.integers(min_value=10**12, max_value=10**15 - 1))
def test_every_luhn_valid_card_number_is_masked(base):
    digits = str(base)
    # compute the check digit that makes the number Luhn-valid
    check = next(d for d in range(10) if luhn_valid(digits + str(d)))
    number = digits + str(check)
    assert mask_text(f"x {number} y") == f"x {CARD} y"


def test_luhn_reference_values():
    assert luhn_valid("4111111111111111") and luhn_valid("79927398713")
    assert not luhn_valid("4111111111111112")


# ---- stream assembly --------------------------------------------------------------------------------------
def chunk(**delta):
    finish = delta.pop("finish", None)
    index = delta.pop("index", 0)
    return {
        "id": "c1",
        "model": "m",
        "created": 1,
        "choices": [{"index": index, "delta": delta, "finish_reason": finish}],
    }


def test_content_pieces_are_joined_into_one_message():
    a = StreamAssembler(10_000)
    for c in (
        chunk(role="assistant", content=""),
        chunk(content="Hel"),
        chunk(content="lo"),
        chunk(finish="stop"),
    ):
        a.add(c)
    out = a.result({"prompt_tokens": 3, "completion_tokens": 2})
    assert out["object"] == "chat.completion" and out["model"] == "m" and out["id"] == "c1"
    assert out["choices"][0]["message"] == {"role": "assistant", "content": "Hello"}
    assert out["choices"][0]["finish_reason"] == "stop" and out["usage"]["completion_tokens"] == 2


def test_tool_call_deltas_are_joined_by_index():
    a = StreamAssembler(10_000)
    a.add(
        chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "sea", "arguments": ""},
                }
            ]
        )
    )
    a.add(chunk(tool_calls=[{"index": 0, "function": {"name": "rch", "arguments": '{"q"'}}]))
    a.add(chunk(tool_calls=[{"index": 0, "function": {"arguments": ': "cats"}'}}]))
    a.add(
        chunk(
            tool_calls=[
                {"index": 1, "id": "call_2", "function": {"name": "calc", "arguments": "{}"}}
            ]
        )
    )
    a.add(chunk(finish="tool_calls"))
    message = a.result(None)["choices"][0]["message"]
    assert message["content"] is None
    first, second = message["tool_calls"]
    assert first == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "search", "arguments": '{"q": "cats"}'},
    }
    assert second["function"]["name"] == "calc"


def test_several_choices_are_kept_apart():
    a = StreamAssembler(10_000)
    a.add(chunk(index=0, content="a"))
    a.add(chunk(index=1, content="b"))
    out = a.result(None)
    assert [c["message"]["content"] for c in out["choices"]] == ["a", "b"]


def test_an_answer_over_the_limit_is_not_stored_at_all():
    a = StreamAssembler(10)
    a.add(chunk(content="x" * 6))
    a.add(chunk(content="y" * 6))
    assert a.overflow and a.result(None) is None


def test_nothing_received_gives_no_result():
    assert StreamAssembler(100).result(None) is None


def test_the_filter_feeds_the_assembler_whatever_the_chunk_boundaries():
    events = (
        b"".join(
            f"data: {json.dumps(c)}\n\n".encode()
            for c in (chunk(content="Hi "), chunk(content="there"), chunk(finish="stop"))
        )
        + b"data: [DONE]\n\n"
    )
    for size in (1, 7, 1000):
        assembler = StreamAssembler(1000)
        f = SseFilter(False, assembler)
        for i in range(0, len(events), size):
            f.feed(events[i : i + size])
        assert assembler.result(None)["choices"][0]["message"]["content"] == "Hi there"


# ---- the event --------------------------------------------------------------------------------------------------
def make_record(**kw):
    defaults = {
        "request_id": str(uuid.uuid4()),
        "started_at": datetime(2026, 11, 15, 12, 0, tzinfo=UTC),
        "project_id": str(uuid.uuid4()),
        "key_id": str(uuid.uuid4()),
        "status": "ok",
        "http_status": 200,
        "model": "mock-strong",
    }
    return RequestRecord(**{**defaults, **kw})


def test_event_carries_the_record_and_survives_json():
    rec = make_record(
        prompt_tokens=5,
        completion_tokens=2,
        request_payload={"messages": [{"role": "user", "content": "hi"}]},
        response_payload={"choices": []},
    )
    payload = build_event(rec)
    parsed = RequestCompleted.model_validate(json.loads(json.dumps(payload)))
    assert str(parsed.request_id) == rec.request_id and parsed.prompt_tokens == 5
    assert parsed.request_payload == rec.request_payload and not parsed.payload_masked


def test_masking_is_applied_only_when_the_project_asked_and_is_flagged():
    body = {"messages": [{"role": "user", "content": "mail me: a@b.io"}]}
    plain = build_event(make_record(request_payload=body, mask_pii=False))
    assert "a@b.io" in json.dumps(plain) and plain["payload_masked"] is False
    masked = build_event(
        make_record(request_payload=body, response_payload={"x": "+7 999 123 45 67"}, mask_pii=True)
    )
    text = json.dumps(masked)
    assert "a@b.io" not in text and "999" not in text and masked["payload_masked"] is True
    assert body["messages"][0]["content"] == "mail me: a@b.io"  # the live request is untouched


def test_nothing_to_mask_means_not_flagged_as_masked():
    assert build_event(make_record(mask_pii=True))["payload_masked"] is False


def test_unknown_fields_are_ignored_and_required_ones_are_checked():
    payload = build_event(make_record()) | {"added_by_a_newer_gateway": 1}
    RequestCompleted.model_validate(payload)  # extra fields are fine
    for missing in ("request_id", "started_at", "project_id", "status"):
        broken = {k: v for k, v in build_event(make_record()).items() if k != missing}
        with pytest.raises(ValidationError):
            RequestCompleted.model_validate(broken)


# ---- the gateway's event buffer -----------------------------------------------------------------------------------
class _Settings:
    traffic_buffer_events = 3
    traffic_buffer_bytes = 10_000
    traffic_drain_timeout = 0.1
    service_name = "gateway"


def test_unauthenticated_requests_are_not_logged():
    p = TrafficEventPublisher(_Settings())  # type: ignore[arg-type]
    p.submit(make_record(project_id=None))
    assert p.buffered == 0


def test_the_oldest_events_are_dropped_and_counted_when_the_buffer_is_full():
    p = TrafficEventPublisher(_Settings())  # type: ignore[arg-type]
    records = [make_record() for _ in range(5)]
    for r in records:
        p.submit(r)
    assert p.buffered == 3 and p.dropped_total == 2
    assert [x.record.request_id for x in p._buffer] == [r.request_id for r in records[2:]]


def test_the_byte_limit_drops_old_events_too():
    p = TrafficEventPublisher(_Settings())  # type: ignore[arg-type]
    for _ in range(3):
        p.submit(make_record(payload_bytes=4000))  # 3 x 4800 > 10 000
    assert p.dropped_total >= 1 and p.buffered <= 2


# ---- partitions ------------------------------------------------------------------------------------------------------
def test_month_arithmetic_crosses_year_boundaries():
    assert partitions.add_months(date(2026, 11, 1), 2) == date(2027, 1, 1)
    assert partitions.add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
    assert partitions.add_months(date(2026, 3, 1), 0) == date(2026, 3, 1)


@given(st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 12, 31)), st.integers(-60, 60))
def test_adding_and_subtracting_months_round_trips(d, n):
    m = partitions.month_start(d)
    assert partitions.add_months(partitions.add_months(m, n), -n) == m


def test_partition_names_round_trip_and_foreign_names_are_rejected():
    m = date(2026, 3, 1)
    assert partitions.partition_name(m) == "requests_2026_03"
    assert partitions.month_of_name("requests_2026_03") == m
    for other in (
        "requests",
        "requests_backup",
        "requests_2026_3",
        "x_requests_2026_03",
        "requests_2026_03_old",
    ):
        assert partitions.month_of_name(other) is None


def test_partition_ddl_has_exact_utc_bounds():
    sql = partitions.create_partition_sql(date(2026, 12, 1))
    assert "requests_2026_12" in sql
    assert "FROM ('2026-12-01 00:00:00+00') TO ('2027-01-01 00:00:00+00')" in sql


# ---- cursors ----------------------------------------------------------------------------------------------------------
def test_cursor_round_trip():
    t, i = datetime(2026, 11, 15, 12, 30, 1, 123456, tzinfo=UTC), uuid.uuid4()
    assert decode_cursor(encode_cursor(t, i)) == (t, i)


@pytest.mark.parametrize("bad", ["", "not-base64!!", "e30", "bm90LWpzb24", "eyJ0IjogIngifQ"])
def test_garbage_cursors_are_rejected(bad):
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)
