"""Pages render the numbers of the api; empty and failing states are explained."""

import json
import re
from typing import Any

from tests.conftest import (
    MODELS,
    OVERVIEW,
    PROJECT_ID,
    REFERENCE_ID,
    RUN_ID,
    TINY_ID,
    USAGE_DAY,
    FakeApi,
    csrf_header,
    grab,
    sign_in,
    world,
)

P = f"/projects/{PROJECT_ID}"


def overview_routes(api: FakeApi) -> None:
    api.on("GET", f"{P}/analytics/overview", OVERVIEW)
    api.on("GET", f"{P}/analytics/usage", USAGE_DAY)


async def test_the_overview_shows_money_savings_and_problems(session, api):
    overview_routes(api)
    r = await session.get("/overview")
    assert r.status_code == 200
    html = r.text
    assert "Support bot" in html  # the project switcher
    assert "$1.2000" in html and "$9.6000" in html and "$8.4000" in html
    assert "tiny-model" in html and "90.0%" in html
    assert "idle server" in html and "llama-local" in html
    assert 'data-chart="' in html  # the chart configuration travels with the canvas
    assert "90%" in html  # the key budget bar


async def test_the_overview_asks_for_the_month_in_the_url(session, api):
    overview_routes(api)
    await session.get("/overview?month=2026-09")
    call = api.calls_to("GET", f"{P}/analytics/overview")[-1]
    assert call.url.params["month"] == "2026-09"


async def test_a_bad_month_is_rejected_before_the_api_is_asked(session, api):
    overview_routes(api)
    r = await session.get("/overview?month=nonsense")
    assert r.status_code in (400, 422)
    assert not api.calls_to("GET", f"{P}/analytics/overview")


async def test_an_empty_month_has_an_explanation(session, api):
    empty = {
        **OVERVIEW,
        "totals": {**OVERVIEW["totals"], "requests": 0},
        "models": [],
        "problems": [],
    }
    api.on("GET", f"{P}/analytics/overview", empty)
    api.on("GET", f"{P}/analytics/usage", {**USAGE_DAY, "rows": []})
    r = await session.get("/overview")
    assert "No traffic in" in r.text and "base_url" in r.text


async def test_without_a_project_the_page_says_what_to_do(client, api):
    api.on("GET", "/projects", [])
    await sign_in(client)
    r = await client.get("/overview")
    assert r.status_code == 404 and "Create a project first" in r.text
    projects = await client.get("/projects")
    assert projects.status_code == 200 and "No project yet" in projects.text


async def test_an_api_error_becomes_a_readable_page_not_a_crash(session, api):
    api.on("GET", f"{P}/analytics/overview", {"title": "Forbidden", "detail": "not yours"}, 403)
    api.on("GET", f"{P}/analytics/usage", USAGE_DAY)
    r = await session.get("/overview")
    assert r.status_code == 403 and "not yours" in r.text
    assert "Traceback" not in r.text


async def test_an_unreachable_api_is_reported(session, api):
    def boom(request):
        import httpx

        raise httpx.ConnectError("refused")

    api.on_call("GET", f"{P}/analytics/overview", boom)
    api.on("GET", f"{P}/analytics/usage", USAGE_DAY)
    r = await session.get("/overview")
    assert r.status_code == 503 and "not reachable" in r.text


async def test_an_unknown_address_is_a_404_page(session):
    r = await session.get("/no-such-page")
    assert r.status_code == 404 and "Not found" in r.text


async def test_a_failing_fragment_is_shown_in_its_place(session, api):
    api.on("GET", f"{P}/calibration-runs/{RUN_ID}", {"title": "Not found", "detail": "gone"}, 404)
    r = await session.get(f"/runs/{RUN_ID}/progress", headers={"hx-request": "true"})
    assert r.status_code == 200 and "gone" in r.text and "<html" not in r.text


async def test_security_headers_are_set(client):
    r = await client.get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    script_rule = re.search(r"script-src[^;]*", r.headers["content-security-policy"])
    assert script_rule and "'unsafe-inline'" not in script_rule.group(0)
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_static_files_are_served_without_a_login(client):
    for path in (
        "/static/vendor/htmx.min.js",
        "/static/vendor/alpine.min.js",
        "/static/vendor/chart.umd.js",
    ):
        r = await client.get(path)
        assert r.status_code == 200 and len(r.content) > 10_000, path


async def test_health_endpoints(client, api):
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    assert (await client.get("/health/ready")).status_code == 200
    api.on("GET", "/health/ready", {"status": "down"}, 503)
    assert (await client.get("/health/ready")).status_code == 503


# ---- matrix ------------------------------------------------------------------------------------
def matrix_payload() -> dict[str, Any]:
    cell = {
        "task_type": "classification", "model_id": TINY_ID, "run_id": RUN_ID, "eligible": True, "reasons": [],
        "is_primary": True, "in_active_rule": False, "insufficient_data": False, "n_total": 50, "n_evaluated": 50,
        "quality_mean": 0.9, "pass_rate": 0.94, "pass_rate_low": 0.84, "pass_rate_high": 0.98,
        "prompt_tokens_avg": 120.0, "completion_tokens_avg": 12.0, "latency_p50_ms": 300.0,
        "latency_p95_ms": 900.0, "error_rate": 0.0, "cost_per_request_usd": "0.0001",
    }  # fmt: skip
    return {
        "models": [
            {"id": REFERENCE_ID, "name": "big-model", "tier": 5, "cost_type": "per_token", "enabled": True, "is_reference": True},
            {"id": TINY_ID, "name": "tiny-model", "tier": 1, "cost_type": "per_token", "enabled": True, "is_reference": False},
        ],
        "columns": [
            {"task_type": "classification", "mode": "record", "run_id": RUN_ID, "run_finished_at": None,
             "criteria": None, "primary": TINY_ID, "active_version": None, "active_chain": []},
            {"task_type": "summary", "mode": "record", "run_id": None, "run_finished_at": None,
             "criteria": None, "primary": None, "active_version": None, "active_chain": []},
        ],
        "cells": [cell],
    }  # fmt: skip


async def test_the_matrix_embeds_every_model_and_every_task_type_for_the_browser(session, api):
    api.on("GET", f"{P}/matrix", matrix_payload())
    r = await session.get("/matrix")
    assert r.status_code == 200
    raw = grab(r'<script type="application/json" id="matrix-data">(.*?)</script>', r.text, re.S)
    data = json.loads(raw.replace("\\u0026", "&"))
    assert [t["name"] for t in data["tasks"]] == ["classification", "summary"]
    assert [t["measured"] for t in data["tasks"]] == [True, False]
    tiny = next(m for m in data["models"] if m["name"] == "tiny-model")
    assert tiny["cells"]["classification"]["pass_rate"] == 0.94
    assert tiny["cells"]["summary"] is None  # not calibrated: an empty cell, not an error
    assert tiny["measured"] is True
    assert "matrix()" in r.text


async def test_the_matrix_json_cannot_break_out_of_its_script_tag(session, api):
    payload = matrix_payload()
    payload["models"][1]["name"] = "</script><script>alert(1)</script>"
    api.on("GET", f"{P}/matrix", payload)
    r = await session.get("/matrix")
    assert "</script><script>alert(1)" not in r.text


def cell_routes(api: FakeApi) -> None:
    base = f"{P}/calibration-runs/{RUN_ID}"
    aggregate = {
        "model_id": TINY_ID, "task_type": "classification", "n_total": 50, "n_ok": 50, "n_error": 0,
        "n_context_exceeded": 0, "n_evaluated": 50, "n_judge_failed": 0, "quality_mean": 0.9,
        "quality_p10": 0.5, "pass_rate": 0.94, "pass_rate_low": 0.84, "pass_rate_high": 0.98, "valid_rate": 1.0,
        "valid_args_rate": None, "n_sessions": 0, "n_sessions_passed": 0, "session_pass_rate": None,
        "session_pass_low": None, "session_pass_high": None, "prompt_tokens_avg": 120.0,
        "completion_tokens_avg": 12.0, "latency_p50_ms": 300.0, "latency_p95_ms": 900.0, "ttft_p50_ms": None,
        "ttft_p95_ms": None, "error_rate": 0.0, "cost_per_request_usd": "0.0001", "judge_cost_usd": None,
        "insufficient_data": False, "basis_counts": {}, "computed_at": "2026-11-17T10:00:00Z",
    }  # fmt: skip
    api.on(
        "GET",
        f"{base}/aggregates",
        {"run_id": RUN_ID, "evaluation_status": "done", "min_samples": 30, "items": [aggregate]},
    )
    api.on("GET", f"{base}/recommendation", {
        "run_id": RUN_ID, "task_type": "classification", "primary": TINY_ID, "chain": [TINY_ID], "chain_names": ["tiny-model"],
        "criteria": {"min_pass_rate": 0.9, "min_quality": None, "max_p95_ms": None, "max_error_rate": 0.05},
        "verdicts": [{"model_id": TINY_ID, "name": "tiny-model", "eligible": True, "reasons": [], "fixed_cost": False,
                      "cost_per_request_usd": "0.0001", "pass_rate": 0.94, "pass_rate_low": 0.84,
                      "pass_rate_high": 0.98, "quality_mean": 0.9, "latency_p95_ms": 900.0, "error_rate": 0.0,
                      "n_evaluated": 50, "insufficient_data": False}],
        "notes": [],
    })  # fmt: skip

    def evaluations(request):
        import httpx

        failed = request.url.params.get("passed") == "false"
        items = [{
            "id": "e1" if failed else "e2", "result_id": "r1" if failed else "r2", "model_id": TINY_ID,
            "request_id": "q1" if failed else "q2", "status": "evaluated", "basis": "judge", "quality": 0.2 if failed else 1.0,
            "passed": not failed, "equal_to_truth": False, "checks": [{"kind": "exact_match", "passed": not failed}],
            "valid_schema": None, "valid_args": None, "judge_mode": "grade", "judge_score": 2 if failed else 5,
            "judge_verdict": "fail" if failed else "pass", "judge_rationale": "wrong label" if failed else "ok",
            "judge_error": None, "judge_calls": 1, "judge_prompt_version": 1, "judge_cost_usd": None,
        }]  # fmt: skip
        return httpx.Response(200, json={"items": items, "next_cursor": None})

    api.on_call("GET", f"{base}/evaluations", evaluations)

    def results(request):
        import httpx

        rid = request.url.params["request_id"]
        item = {"id": "r1" if rid == "q1" else "r2", "request_id": rid, "model_id": TINY_ID, "status": "ok",
                "error_code": None,
                "response_payload": {"choices": [{"message": {"content": "billing" if rid == "q1" else "refund"}}]}}  # fmt: skip
        return httpx.Response(200, json={"items": [item], "next_cursor": None})

    api.on_call("GET", f"{base}/results", results)

    def request_detail(request):
        import httpx

        return httpx.Response(200, json={
            "request_id": request.url.path.rsplit("/", 1)[1],
            "request_payload": {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "I was charged twice"}]},
            "response_payload": {"choices": [{"message": {"content": "refund"}}]},
        })  # fmt: skip

    api.on_call("GET", f"{P}/requests/q1", request_detail)
    api.on_call("GET", f"{P}/requests/q2", request_detail)


async def test_a_cell_shows_numbers_reasons_and_examples_failed_first(session, api):
    cell_routes(api)
    r = await session.get(
        "/matrix/cell",
        params={"run_id": RUN_ID, "model_id": TINY_ID, "task_type": "classification"},
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200
    html = r.text
    assert "<html" not in html  # a fragment
    assert "tiny-model" in html and "94.0%" in html and "$0.0001" in html
    assert "I was charged twice" in html and "billing" in html and "wrong label" in html
    assert html.index("failed") < html.index("passed")
    assert "/matrix/verdict" in html  # an admin can give a verdict


async def test_a_viewer_sees_the_examples_but_no_verdict_buttons(client, api, redis):
    world(api, role="viewer")
    cell_routes(api)
    await sign_in(client)
    r = await client.get(
        "/matrix/cell",
        params={"run_id": RUN_ID, "model_id": TINY_ID, "task_type": "classification"},
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200 and "wrong label" in r.text and "/matrix/verdict" not in r.text


async def test_a_verdict_is_sent_to_the_api(session, api, redis):
    api.on("PUT", f"{P}/calibration-runs/{RUN_ID}/results/r1/human-verdict",
           {"result_id": "r1", "verdict": "fail", "comment": None, "aggregates_recomputed": True})  # fmt: skip
    r = await session.post(
        "/matrix/verdict",
        data={"run_id": RUN_ID, "result_id": "r1", "verdict": "fail"},
        headers={**csrf_header(session, redis), "hx-request": "true"},
    )
    assert r.status_code == 200 and "Saved: fail" in r.text
    sent = api.calls_to("PUT", f"{P}/calibration-runs/{RUN_ID}/results/r1/human-verdict")[0]
    assert json.loads(sent.content) == {"verdict": "fail"}


# ---- usage and feedback -------------------------------------------------------------------------
async def test_usage_shows_idle_servers_with_their_cost(session, api):
    api.on("GET", f"{P}/analytics/usage", USAGE_DAY)
    api.on("GET", "/analytics/servers", {"month": "2026-11", "servers": [{
        "model_id": "m", "name": "llama-local", "monthly_cost_usd": "120.00", "window_seconds": 100.0,
        "busy_seconds": 3600.0, "utilization_pct": 3.2,
        "busy_by_source_seconds": {"live": 3000.0, "calibration": 600.0, "shadow": 0.0},
        "idle_threshold_pct": 20.0, "idle": True, "idle_cost_usd": "116.00", "requests": 40,
        "effective_cost_per_request_usd": "3.00"}]})  # fmt: skip
    r = await session.get("/usage")
    assert r.status_code == 200
    assert "llama-local" in r.text and "idle" in r.text and "$116.00" in r.text and "3.2%" in r.text


async def test_feedback_shows_dislike_shares_and_a_rollback_hint(session, api):
    api.on("GET", f"{P}/analytics/feedback", {
        "since": "2026-10-17T00:00:00Z", "until": "2026-11-17T00:00:00Z", "ratings": 40, "dislikes": 10,
        "groups": [{"model_id": TINY_ID, "model": "tiny-model", "task_type": "classification", "ratings": 40,
                    "dislikes": 10, "dislike_rate": 0.25, "dislike_rate_low": 0.14, "dislike_rate_high": 0.40}]})  # fmt: skip
    api.on("GET", f"{P}/feedback", {"items": [{"request_id": "abcdef12-0000", "model": "tiny-model", "task_type": "classification",
                                               "rating": -1, "comment": "wrong answer", "updated_at": "2026-11-17T09:00:00Z"}]})  # fmt: skip
    api.on("GET", f"{P}/task-types", [{"name": "classification", "mode": "route", "shadow_model_ids": [], "shadow_rate": 0.0,
                                       "auto_registered": False, "created_at": "2026-11-01T10:00:00Z"}])  # fmt: skip
    api.on("GET", f"{P}/task-types/classification/rule-health", {
        "task_type": "classification", "rule_version": 2, "activated_at": "2026-11-10T10:00:00Z", "suggest_rollback": True,
        "reason": "dislikes rose from 5% to 25%", "ratings_since": 40, "dislikes_since": 10, "dislike_rate_since": 0.25,
        "dislike_rate_low": 0.14, "ratings_before": 60, "dislikes_before": 3, "dislike_rate_before": 0.05})  # fmt: skip
    r = await session.get("/feedback")
    assert r.status_code == 200
    assert "25.0%" in r.text and "wrong answer" in r.text and "consider a rollback" in r.text


async def test_a_task_type_without_a_live_rule_is_skipped_in_the_feedback_hints(session, api):
    api.on("GET", f"{P}/analytics/feedback", {"since": "2026-10-17T00:00:00Z", "until": "2026-11-17T00:00:00Z",
                                              "ratings": 1, "dislikes": 0, "groups": []})  # fmt: skip
    api.on("GET", f"{P}/feedback", {"items": []})
    api.on("GET", f"{P}/task-types", [{"name": "classification", "mode": "record", "shadow_model_ids": [], "shadow_rate": 0.0,
                                       "auto_registered": False, "created_at": "2026-11-01T10:00:00Z"}])  # fmt: skip
    api.on(
        "GET",
        f"{P}/task-types/classification/rule-health",
        {"title": "Conflict", "detail": "no active rule"},
        409,
    )
    r = await session.get("/feedback")
    assert r.status_code == 200 and "consider a rollback" not in r.text


async def test_models_page_lists_models_with_their_prices(session, api):
    api.on("GET", "/providers", [{"id": "p1", "name": "openai", "base_url": "https://api.openai.com/v1", "auth_type": "bearer",
                                  "is_local": False, "enabled": True, "api_key_mask": "sk-…abcd", "last_check_at": None,
                                  "last_check_ok": None, "last_check_latency_ms": None, "last_check_error": None,
                                  "calibration_max_concurrency": None, "calibration_max_rps": None,
                                  "created_at": "2026-11-01T10:00:00Z"}])  # fmt: skip
    api.on("GET", f"/models/{REFERENCE_ID}/price", {"id": "x", "model_id": REFERENCE_ID, "valid_from": "2026-11-01T00:00:00Z",
                                                    "valid_to": None, "input_per_1m": "5.000000", "output_per_1m": "15.000000",
                                                    "monthly_cost_usd": None})  # fmt: skip
    r = await session.get("/models")
    assert r.status_code == 200
    assert "openai" in r.text and "sk-…abcd" in r.text and "big-model" in r.text
    assert "$5.0000 in / $15.0000 out" in r.text
    assert "no price" in r.text  # tiny-model has none
    assert len(MODELS) == 2
