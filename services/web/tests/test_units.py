"""Pure helpers: formatting, forms, payload text, month arithmetic, the matrix shape."""

import pytest

from app import charts, forms
from app import format as fmt
from app.api_client import ApiError
from app.payloads import answer_text, clip, prompt_text
from app.routes.analytics import month_bounds, shift_month
from app.routes.calibration import keeps_changing
from app.routes.matrix import build_matrix
from app.ui import safe_local_path


def test_money_gets_more_digits_for_small_amounts():
    assert fmt.money("1234.5") == "$1,234.50"
    assert fmt.money("1.2") == "$1.2000"
    assert fmt.money("0.000123") == "$0.000123"
    assert fmt.money("-8.4") == "-$8.4000"
    assert fmt.money(None) == fmt.DASH and fmt.money("oops") == fmt.DASH


def test_shares_durations_and_dates_read_naturally():
    assert fmt.pct(0.9321) == "93.2%" and fmt.pct(None) == fmt.DASH
    assert fmt.pct_points(87.456) == "87.5%"
    assert fmt.ms(842) == "842 ms" and fmt.ms(12_500) == "12.5 s"
    assert fmt.num(1234567) == "1,234,567"
    assert fmt.dt("2026-11-17T09:05:00Z") == "2026-11-17 09:05" and fmt.dt(None) == fmt.DASH
    assert fmt.short("abcdef1234567890") == "abcdef12"


def test_checkbox_and_text_helpers():
    assert (
        forms.checked("1")
        and forms.checked("true")
        and not forms.checked("")
        and not forms.checked(None)
    )
    assert forms.optional_text("  ") is None and forms.optional_text(" a ") == "a"
    assert forms.optional_int("") is None and forms.optional_int("7") == 7
    with pytest.raises(ApiError):
        forms.optional_int("seven")


def test_numbers_accept_a_decimal_comma_and_reject_text():
    assert forms.number("0,15") == "0.15"
    with pytest.raises(ApiError):
        forms.number("cheap")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/matrix?x=1", "/matrix?x=1"),
        ("https://evil.example", "/"),
        ("//evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("", "/"),
        (None, "/"),
        ("relative", "/"),
    ],
)
def test_only_addresses_on_this_site_are_followed(value, expected):
    assert safe_local_path(value) == expected


def test_month_arithmetic_crosses_year_boundaries():
    assert shift_month("2026-01", -1) == "2025-12"
    assert shift_month("2026-12", 1) == "2027-01"
    label, first, last = month_bounds("2025-02")
    assert label == "2025-02" and first.day == 1 and last.day == 28


def test_prompt_and_answer_text_from_stored_payloads():
    request = {"messages": [{"role": "user", "content": "first"}, {"role": "assistant", "content": "x"},
                            {"role": "user", "content": [{"type": "text", "text": "last"}, {"type": "image_url"}]}]}  # fmt: skip
    assert prompt_text(request) == "last\n[image_url]"
    assert prompt_text(None) == "" and prompt_text({}) == ""
    answer = {"choices": [{"message": {"content": "ok", "tool_calls": [
        {"function": {"name": "lookup", "arguments": '{"id": 7}'}}]}}]}  # fmt: skip
    assert answer_text(answer) == 'ok\n→ lookup({"id":7})'
    assert answer_text({"choices": []}) == "" and answer_text(None) == ""
    assert len(clip("x" * 5000)) <= 701


def test_polling_stops_when_nothing_more_will_change():
    run = {"status": "running", "evaluation": {"status": "pending"}}
    assert keeps_changing(run)
    assert keeps_changing({"status": "completed", "evaluation": {"status": "running"}})
    assert keeps_changing({"status": "completed", "evaluation": {"status": "pending"}})
    assert not keeps_changing({"status": "completed", "evaluation": {"status": "done"}})
    assert not keeps_changing({"status": "paused", "evaluation": {"status": "pending"}})
    assert not keeps_changing({"status": "cancelled", "evaluation": {"status": "pending"}})


def test_build_matrix_gives_every_model_a_cell_or_none_per_task_type():
    raw = {
        "models": [{"id": "m1", "name": "a", "tier": 1, "cost_type": "per_token", "enabled": True, "is_reference": False},
                   {"id": "m2", "name": "b", "tier": 2, "cost_type": "per_token", "enabled": True, "is_reference": False}],
        "columns": [{"task_type": "t1", "mode": "route", "run_id": "r", "run_finished_at": None, "criteria": None,
                     "primary": "m1", "active_version": 3, "active_chain": ["m1"]},
                    {"task_type": "t2", "mode": "record", "run_id": None, "run_finished_at": None, "criteria": None,
                     "primary": None, "active_version": None, "active_chain": []}],
        "cells": [{"task_type": "t1", "model_id": "m1", "pass_rate": 0.9}],
    }  # fmt: skip
    matrix = build_matrix(raw)
    assert [t["name"] for t in matrix["tasks"]] == ["t1", "t2"]
    first, second = matrix["models"]
    assert (
        first["cells"]["t1"]["pass_rate"] == 0.9
        and first["cells"]["t2"] is None
        and first["measured"]
    )
    assert second["cells"] == {"t1": None, "t2": None} and not second["measured"]


def test_chart_configs_are_plain_json_with_floats():
    config = charts.line(["d1", "d2"], [("Cost", [1.0, charts.num("2.50")], "#000")], money=True)
    assert config["type"] == "line" and config["data"]["datasets"][0]["data"] == [1.0, 2.5]
    assert charts.num(None) == 0.0
    assert charts.doughnut(["a"], [3])["data"]["datasets"][0]["data"] == [3]
    assert charts.bars(["a"], [("x", [1], "#fff")], stacked=True)["options"]["scales"]["x"][
        "stacked"
    ]
