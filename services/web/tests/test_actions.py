"""Forms: what the web app sends to the api, and how failures come back to the person."""

import json
from typing import Any

import httpx

from tests.conftest import (
    PROJECT_ID,
    REFERENCE_ID,
    RUN_ID,
    TINY_ID,
    FakeApi,
    csrf_header,
    sign_in,
    world,
)

P = f"/projects/{PROJECT_ID}"


def body_of(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body


async def test_a_viewer_cannot_change_anything_and_the_api_is_not_even_asked(client, api, redis):
    world(api, role="viewer")
    api.on("GET", "/providers", [])
    await sign_in(client)
    r = await client.post(
        "/models/providers",
        data={"name": "x", "base_url": "http://x"},
        headers={**csrf_header(client, redis), "referer": "http://web/models"},
    )
    assert r.status_code == 303 and r.headers["location"] == "/models"
    assert not api.calls_to("POST", "/providers")
    assert "may look but not change" in (await client.get("/models")).text


async def test_a_viewer_sees_no_buttons_that_change_things(client, api):
    world(api, role="viewer")
    api.on("GET", "/providers", [])
    await sign_in(client)
    r = await client.get("/models")
    assert "Add a provider" not in r.text and "Add a model" not in r.text


async def test_a_rejected_form_comes_back_with_the_reason(session, api, redis):
    api.on(
        "POST",
        "/providers",
        {"title": "Unprocessable request", "detail": "base_url points to a forbidden address"},
        422,
    )
    api.on("GET", "/providers", [])
    r = await session.post(
        "/models/providers",
        data={"name": "x", "base_url": "http://169.254.169.254"},
        headers={**csrf_header(session, redis), "referer": "http://web/models"},
    )
    assert r.status_code == 303 and r.headers["location"] == "/models"
    page = await session.get("/models")
    assert "base_url points to a forbidden address" in page.text
    again = await session.get("/models")
    assert "forbidden address" not in again.text  # the message is shown once


async def test_a_provider_is_created_with_typed_values(session, api, redis):
    api.on("POST", "/providers", {"id": "p9", "name": "local"}, 201)
    r = await session.post(
        "/models/providers",
        data={"name": " local ", "base_url": "http://ollama:11434/v1", "auth_type": "none", "api_key": "",
              "is_local": "1", "max_concurrency": "4", "max_rps": ""},
        headers=csrf_header(session, redis),
    )  # fmt: skip
    assert r.status_code == 303
    sent = body_of(api.calls_to("POST", "/providers")[0])
    assert sent == {"name": "local", "base_url": "http://ollama:11434/v1", "auth_type": "none", "is_local": True,
                    "api_key": None, "calibration_max_concurrency": 4, "calibration_max_rps": None}  # fmt: skip


async def test_a_model_and_its_price_are_created_together(session, api, redis):
    api.on("POST", "/models", {"id": "m9", "name": "mini"}, 201)
    api.on("POST", "/models/m9/prices", {"id": "pr"}, 201)
    r = await session.post(
        "/models/models",
        data={"provider_id": "p1", "name": "mini", "context_window": "8192", "tier": "2", "cost_type": "per_token",
              "input_per_1m": "0,15", "output_per_1m": "0.6", "supports_json": "1", "supports_stream": "1"},
        headers=csrf_header(session, redis),
    )  # fmt: skip
    assert r.status_code == 303
    model = body_of(api.calls_to("POST", "/models")[0])
    assert (
        model["supports_json"] is True and model["supports_tools"] is False and model["tier"] == 2
    )
    assert body_of(api.calls_to("POST", "/models/m9/prices")[0]) == {
        "input_per_1m": "0.15",
        "output_per_1m": "0.6",
    }


async def test_a_fixed_price_server_gets_a_monthly_price(session, api, redis):
    api.on("POST", "/models", {"id": "m8", "name": "gpu"}, 201)
    api.on("POST", "/models/m8/prices", {"id": "pr"}, 201)
    await session.post(
        "/models/models",
        data={"provider_id": "p1", "name": "gpu", "context_window": "4096", "cost_type": "fixed_monthly", "monthly_cost_usd": "120"},
        headers=csrf_header(session, redis),
    )  # fmt: skip
    assert body_of(api.calls_to("POST", "/models/m8/prices")[0]) == {"monthly_cost_usd": "120"}


async def test_a_text_that_is_not_a_number_is_explained(session, api, redis):
    r = await session.post(
        "/models/models/m1/price",
        data={"cost_type": "per_token", "input_per_1m": "cheap", "output_per_1m": "1"},
        headers={**csrf_header(session, redis), "referer": "http://web/models"},
    )
    assert r.status_code == 303
    assert not api.calls_to("POST", "/models/m1/prices")
    assert "is not a number" in (await session.get("/models")).text


# ---- calibration -------------------------------------------------------------------------------
async def test_a_calibration_is_started_with_thresholds_as_fractions_and_an_idempotency_key(
    session, api, redis
):
    api.on("POST", f"{P}/calibration-runs", {"id": RUN_ID}, 201)
    r = await session.post(
        "/runs",
        data={"task_type": "classification", "sample_size": "80", "candidate_model_ids": [TINY_ID],
              "strategy": "stratified", "min_pass_rate_pct": "92", "max_error_rate_pct": "3", "max_p95_ms": "1500",
              "max_cost_usd": "2.50", "prefer_disliked": "1", "idem": "key-123"},
        headers=csrf_header(session, redis),
    )  # fmt: skip
    assert r.status_code == 303 and r.headers["location"] == f"/runs/{RUN_ID}"
    call = api.calls_to("POST", f"{P}/calibration-runs")[0]
    assert call.headers["idempotency-key"] == "key-123"
    sent = body_of(call)
    assert sent["thresholds"] == {"min_pass_rate": 0.92, "max_error_rate": 0.03, "max_p95_ms": 1500}
    assert sent["candidate_model_ids"] == [TINY_ID] and sent["prefer_disliked"] is True
    assert (
        sent["max_cost_usd"] == "2.50"
        and sent["max_requests"] is None
        and sent["judge_model_id"] is None
    )


async def test_a_calibration_needs_at_least_one_model(session, api, redis):
    r = await session.post(
        "/runs",
        data={"task_type": "classification", "sample_size": "10"},
        headers={**csrf_header(session, redis), "referer": "http://web/runs"},
    )
    assert r.status_code == 303 and r.headers["location"] == "/runs"
    assert not api.calls_to("POST", f"{P}/calibration-runs")


def run_payload(status="running", evaluation="pending", percent=40.0) -> dict[str, Any]:
    return {
        "id": RUN_ID, "project_id": PROJECT_ID, "task_type": "classification", "status": status, "status_reason": None,
        "strategy": "random", "source": "traffic", "sample_unit": "request", "seed": 7, "sample_size": 50,
        "samples_taken": 50, "reference_model_id": REFERENCE_ID, "candidate_model_ids": [TINY_ID], "max_requests": None,
        "max_cost_usd": None, "thresholds": {"min_pass_rate": 0.9}, "judge_model_id": None, "judge_pairwise": False,
        "created_at": "2026-11-17T09:00:00Z", "finished_at": None,
        "progress": {"total_jobs": 50, "finished": 20, "percent": percent, "by_status": {"ok": 20}, "calls": 20,
                     "spent_usd": "0.01", "remaining_requests": None, "remaining_usd": None},
        "evaluation": {"status": evaluation, "total": 50, "done": 10},
    }  # fmt: skip


async def test_the_progress_block_keeps_polling_while_the_run_is_alive(session, api):
    api.on("GET", f"{P}/calibration-runs/{RUN_ID}", run_payload())
    r = await session.get(f"/runs/{RUN_ID}/progress", headers={"hx-request": "true"})
    assert 'hx-trigger="every 3s"' in r.text and "40%" in r.text
    assert "hx-refresh" not in r.headers


async def test_the_progress_block_reloads_the_page_when_the_numbers_are_ready(session, api):
    api.on("GET", f"{P}/calibration-runs/{RUN_ID}", run_payload("completed", "done", 100.0))
    r = await session.get(f"/runs/{RUN_ID}/progress", headers={"hx-request": "true"})
    assert r.headers["hx-refresh"] == "true" and 'hx-trigger="every 3s"' not in r.text


async def test_a_paused_run_is_not_polled_but_can_be_resumed(session, api, redis):
    api.on("GET", f"{P}/calibration-runs/{RUN_ID}", run_payload("paused"))
    r = await session.get(f"/runs/{RUN_ID}/progress", headers={"hx-request": "true"})
    assert 'hx-trigger="every 3s"' not in r.text and f"/runs/{RUN_ID}/resume" in r.text
    api.on("POST", f"{P}/calibration-runs/{RUN_ID}/resume", run_payload())
    done = await session.post(f"/runs/{RUN_ID}/resume", headers=csrf_header(session, redis))
    assert done.status_code == 303 and api.calls_to("POST", f"{P}/calibration-runs/{RUN_ID}/resume")


async def test_an_unknown_run_command_is_refused(session, redis):
    r = await session.post(
        f"/runs/{RUN_ID}/explode",
        headers={**csrf_header(session, redis), "referer": "http://web/runs"},
    )
    assert r.status_code == 303


async def test_the_finished_run_shows_the_recommendation_and_can_propose_a_rule(
    session, api, redis
):
    from tests.test_pages import cell_routes

    cell_routes(api)
    api.on("GET", f"{P}/calibration-runs/{RUN_ID}", run_payload("completed", "done", 100.0))
    r = await session.get(f"/runs/{RUN_ID}")
    assert r.status_code == 200
    assert (
        "Use <strong>tiny-model</strong>" in r.text
        and "Propose as a rule" in r.text
        and "94.0%" in r.text
    )
    api.on("POST", f"{P}/calibration-runs/{RUN_ID}/recommendation/propose",
           {"id": "r1", "version": 3, "task_type": "classification"}, 201)  # fmt: skip
    done = await session.post(
        f"/runs/{RUN_ID}/recommendation/propose",
        data={"note": "first try"},
        headers=csrf_header(session, redis),
    )
    assert done.status_code == 303 and done.headers["location"] == "/rules#classification"
    assert body_of(
        api.calls_to("POST", f"{P}/calibration-runs/{RUN_ID}/recommendation/propose")[0]
    ) == {"note": "first try"}


# ---- rules -------------------------------------------------------------------------------------
def rules_routes(api: FakeApi, *, active_version: int | None = 1) -> None:
    api.on("GET", f"{P}/task-types", [{"name": "classification", "mode": "route", "shadow_model_ids": [], "shadow_rate": 0.0,
                                       "auto_registered": False, "created_at": "2026-11-01T10:00:00Z"}])  # fmt: skip
    rule: dict[str, Any] = {"project_id": PROJECT_ID, "task_type": "classification", "chain": [TINY_ID, REFERENCE_ID],
            "chain_names": ["tiny-model", "big-model"], "source_run_id": None, "basis": {}, "note": None,
            "created_at": "2026-11-10T10:00:00Z", "activated_at": None, "retired_at": None}  # fmt: skip
    history: list[dict[str, Any]] = [{**rule, "id": "r2", "version": 2, "status": "proposed"}]
    if active_version:
        history.append(
            {
                **rule,
                "id": "r1",
                "version": active_version,
                "status": "active",
                "activated_at": "2026-11-11T10:00:00Z",
            }
        )
        history.append(
            {
                **rule,
                "id": "r0",
                "version": 0,
                "status": "superseded",
                "retired_at": "2026-11-11T10:00:00Z",
            }
        )
    api.on("GET", f"{P}/task-types/classification/rules", history)
    api.on(
        "GET",
        f"{P}/task-types/classification/rule-health",
        {"title": "Conflict", "detail": "none"},
        409,
    )


async def test_the_rules_page_shows_live_proposed_and_history(session, api):
    rules_routes(api)
    r = await session.get("/rules")
    assert r.status_code == 200
    assert "Live rule" in r.text and "Version 1" in r.text and "Version 2" in r.text
    assert "Roll back to the previous version" in r.text and "tiny-model" in r.text
    assert 'name="expected" value="1"' in r.text  # the version the person saw


async def test_activation_sends_the_version_the_person_saw(session, api, redis):
    rules_routes(api)
    api.on("POST", f"{P}/rules/r2/activate", {"id": "r2", "version": 2, "status": "active"})
    r = await session.post(
        "/rules/r2/activate", data={"task": "classification", "expected": "1", "idem": "k1"},
        headers=csrf_header(session, redis),
    )  # fmt: skip
    assert r.status_code == 303 and r.headers["location"] == "/rules#classification"
    call = api.calls_to("POST", f"{P}/rules/r2/activate")[0]
    assert (
        body_of(call) == {"expected_active_version": 1} and call.headers["idempotency-key"] == "k1"
    )


async def test_a_conflicting_activation_asks_the_person_to_decide_again(session, api, redis):
    rules_routes(api)
    api.on(
        "POST",
        f"{P}/rules/r2/activate",
        {"title": "Conflict", "detail": "active version is 5"},
        409,
    )
    r = await session.post(
        "/rules/r2/activate", data={"task": "classification", "expected": "1"},
        headers={**csrf_header(session, redis), "referer": "http://web/rules"},
    )  # fmt: skip
    assert r.status_code == 303
    assert "changed since you opened this page" in (await session.get("/rules")).text


async def test_the_mode_and_the_shadow_share_are_saved(session, api, redis):
    api.on("PATCH", f"{P}/task-types/classification", {"name": "classification"})
    await session.post(
        "/rules/task/classification/mode",
        data={"mode": "shadow", "shadow_model_ids": [TINY_ID], "shadow_rate_pct": "25"},
        headers=csrf_header(session, redis),
    )  # fmt: skip
    assert body_of(api.calls_to("PATCH", f"{P}/task-types/classification")[0]) == {
        "mode": "shadow", "shadow_model_ids": [TINY_ID], "shadow_rate": 0.25,
    }  # fmt: skip


async def test_a_rule_written_by_hand_and_a_rollback(session, api, redis):
    api.on("POST", f"{P}/task-types/classification/rules", {"id": "r5", "version": 5}, 201)
    api.on("POST", f"{P}/task-types/classification/rules/rollback", {"id": "r1", "version": 1})
    await session.post("/rules/task/classification/create", data={"first": TINY_ID, "second": REFERENCE_ID, "third": "", "note": "manual"},
                       headers=csrf_header(session, redis))  # fmt: skip
    assert body_of(api.calls_to("POST", f"{P}/task-types/classification/rules")[0]) == {
        "chain": [TINY_ID, REFERENCE_ID],
        "note": "manual",
    }
    await session.post("/rules/task/classification/rollback", headers=csrf_header(session, redis))
    assert api.calls_to("POST", f"{P}/task-types/classification/rules/rollback")


async def test_the_same_model_twice_in_a_chain_is_refused(session, api, redis):
    r = await session.post("/rules/task/classification/create", data={"first": TINY_ID, "second": TINY_ID},
                           headers={**csrf_header(session, redis), "referer": "http://web/rules"})  # fmt: skip
    assert r.status_code == 303 and not api.calls_to("POST", f"{P}/task-types/classification/rules")


# ---- projects and keys -------------------------------------------------------------------------
async def test_a_new_key_is_shown_once_and_only_once(session, api, redis):
    api.on("POST", f"{P}/keys", {"id": "k1", "name": "prod", "prefix": "abc", "key": "swy_abc_SECRETVALUE", "revoked_at": None,
                                 "default_task_type": None, "rate_limit_rpm": None, "monthly_budget_usd": None,
                                 "project_id": PROJECT_ID, "created_at": "2026-11-17T10:00:00Z"}, 201)  # fmt: skip
    api.on("GET", f"{P}/keys", [])
    api.on("GET", f"{P}/members", [])
    api.on("GET", f"{P}/task-types", [])
    r = await session.post(
        "/projects/keys",
        data={"name": "prod", "monthly_budget_usd": "5"},
        headers=csrf_header(session, redis),
    )
    assert r.status_code == 303
    assert body_of(api.calls_to("POST", f"{P}/keys")[0])["monthly_budget_usd"] == "5"
    first = await session.get("/projects")
    assert "swy_abc_SECRETVALUE" in first.text and "shown only once" in first.text
    second = await session.get("/projects")
    assert "swy_abc_SECRETVALUE" not in second.text
    assert "SECRETVALUE" not in json.dumps(list(redis.sessions().values()))


async def test_project_settings_are_saved_with_the_reference_model(session, api, redis):
    api.on("PATCH", P, {"id": PROJECT_ID})
    api.on("PUT", f"{P}/reference-model", {"id": PROJECT_ID})
    await session.post("/projects/settings", data={"description": "d", "store_bodies": "1", "reference_model_id": REFERENCE_ID},
                       headers=csrf_header(session, redis))  # fmt: skip
    assert body_of(api.calls_to("PATCH", P)[0]) == {
        "description": "d",
        "store_bodies": True,
        "mask_pii": False,
    }
    assert body_of(api.calls_to("PUT", f"{P}/reference-model")[0]) == {"model_id": REFERENCE_ID}


async def test_a_project_can_be_picked_only_from_your_own_list(session, redis):
    r = await session.post("/project/select", data={"project_id": "99999999-9999-9999-9999-999999999999"},
                           headers={**csrf_header(session, redis), "referer": "http://web/overview"})  # fmt: skip
    assert r.status_code == 303
    stored = next(s for s in redis.sessions().values() if s.get("access_token"))
    assert stored.get("project_id") != "99999999-9999-9999-9999-999999999999"
