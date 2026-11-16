"""Rules of routing through the admin API: recommendation, versions, activation, rollback."""

import asyncio
import uuid

import pytest

from tests.integration.calibration_support import (
    Env,
    Pipeline,
    add_check,
    add_judge,
    add_traffic_from_mock,
    build_env,
    sql,
    start_run,
    wait_evaluated,
    wait_status,
)
from tests.integration.conftest import MockServer
from tests.integration.routing_support import (
    activate,
    go_live,
    propose,
    rollback,
    rule_ids,
    set_mode,
)
from tests.integration.test_auth_api import GOOD_PASSWORD, bearer, login, make_admin

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
def settings(app_settings, queue_name):
    return app_settings.model_copy(
        update={
            "replay_queue": f"{queue_name}_replay",
            "run_finished_queue": f"{queue_name}_done",
            "judge_queue": f"{queue_name}_judge",
            "shadow_queue": f"{queue_name}_shadow",
            "retry_delays_ms": [100, 200],
            "calibration_limit_wait": 1.0,
            "calibration_default_rps": 1000.0,
            "calibration_default_concurrency": 20,
        }
    )


@pytest.fixture
async def env(client, admin, api_app, mock_llm: MockServer) -> Env:
    env = await build_env(client, admin, api_app, mock_llm.base_url, requests=0)
    await add_judge(client, admin, env)
    return env


@pytest.fixture
async def pipeline(api_app, settings, amqp, publisher):
    p = Pipeline(api_app, settings, amqp, publisher)
    yield p
    await p.stop()


async def evaluated_run(client, env, api_app, mock_llm, pipeline, **thresholds):
    """A finished and evaluated run of mock-tiny and mock-mid against the mock-strong reference."""
    await add_traffic_from_mock(api_app, env, mock_llm.base_url, 60)
    await add_check(client, env, "exact_match")
    created = await start_run(
        client, env, "tiny", "mid", sample_size=60, seed=11, thresholds=thresholds,
    )  # fmt: skip
    assert created.status_code == 201, created.text
    run = created.json()
    await pipeline.start()
    await wait_status(client, env, run["id"], "completed")
    await wait_evaluated(client, env, run["id"])
    return run


async def recommendation(client, env, run_id):
    return await client.get(
        f"/projects/{env.project_id}/calibration-runs/{run_id}/recommendation", headers=env.admin
    )


# ---- the recommendation -----------------------------------------------------------------------
async def test_the_cheapest_model_that_passes_is_recommended_with_the_reference_as_a_last_resort(
    client, env, api_app, mock_llm, pipeline
):
    run = await evaluated_run(client, env, api_app, mock_llm, pipeline, min_pass_rate=0.5)
    r = await recommendation(client, env, run["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["criteria"]["min_pass_rate"] == 0.5
    assert body["chain_names"] == ["mock-tiny", "mock-mid", "mock-strong"]  # cheapest first
    assert body["primary"] == env.candidates["tiny"]
    by_name = {v["name"]: v for v in body["verdicts"]}
    assert by_name["mock-tiny"]["eligible"] and by_name["mock-mid"]["eligible"]
    assert float(by_name["mock-tiny"]["cost_per_request_usd"]) < float(
        by_name["mock-mid"]["cost_per_request_usd"]
    )
    assert by_name["mock-tiny"]["pass_rate_low"] < by_name["mock-tiny"]["pass_rate"]


async def test_a_model_that_misses_the_threshold_is_left_out_and_the_reason_is_given(
    client, env, api_app, mock_llm, pipeline
):
    run = await evaluated_run(client, env, api_app, mock_llm, pipeline, min_pass_rate=0.8)
    body = (await recommendation(client, env, run["id"])).json()
    assert body["chain_names"] == ["mock-mid", "mock-strong"]
    tiny = next(v for v in body["verdicts"] if v["name"] == "mock-tiny")
    assert not tiny["eligible"] and "is below 0.80" in tiny["reasons"][0]


async def test_when_nobody_passes_the_default_thresholds_nothing_is_proposed(
    client, env, api_app, mock_llm, pipeline
):
    run = await evaluated_run(client, env, api_app, mock_llm, pipeline)  # the default is 0.9
    body = (await recommendation(client, env, run["id"])).json()
    assert body["primary"] is None and body["chain"] == []
    assert "reference model should stay" in body["notes"][0]
    refused = await client.post(
        f"/projects/{env.project_id}/calibration-runs/{run['id']}/recommendation/propose",
        json={}, headers=env.admin,
    )  # fmt: skip
    assert refused.status_code == 422


async def test_a_run_that_is_not_evaluated_yet_has_no_recommendation(
    client, env, api_app, mock_llm
):
    await add_traffic_from_mock(api_app, env, mock_llm.base_url, 5)
    run = (await start_run(client, env, "tiny", sample_size=5)).json()
    r = await recommendation(client, env, run["id"])
    assert r.status_code == 409 and "not evaluated" in r.text


async def test_a_recommendation_becomes_a_proposed_rule_that_changes_nothing_live(
    client, env, api_app, mock_llm, pipeline
):
    run = await evaluated_run(client, env, api_app, mock_llm, pipeline, min_pass_rate=0.8)
    r = await client.post(
        f"/projects/{env.project_id}/calibration-runs/{run['id']}/recommendation/propose",
        json={"note": "from the first run"}, headers=env.admin,
    )  # fmt: skip
    assert r.status_code == 201, r.text
    rule = r.json()
    assert (rule["status"], rule["version"], rule["note"]) == ("proposed", 1, "from the first run")
    assert rule["chain_names"] == ["mock-mid", "mock-strong"]
    assert rule["source_run_id"] == run["id"]
    assert rule["basis"]["criteria"]["min_pass_rate"] == 0.8 and rule["basis"]["verdicts"]
    task = (await client.get(f"/projects/{env.project_id}/task-types", headers=env.admin)).json()
    assert {t["name"]: t["mode"] for t in task}["classification"] == "record"  # nothing switched


# ---- the matrix -------------------------------------------------------------------------------
async def test_the_matrix_lists_every_model_with_the_numbers_of_the_latest_evaluated_run(
    client, env, api_app, mock_llm, pipeline
):
    run = await evaluated_run(client, env, api_app, mock_llm, pipeline, min_pass_rate=0.5)
    r = await client.get(f"/projects/{env.project_id}/matrix", headers=env.admin)
    assert r.status_code == 200, r.text
    body = r.json()
    names = {m["name"]: m for m in body["models"]}
    assert {"mock-tiny", "mock-mid", "mock-strong"} <= set(names)
    assert names["mock-strong"]["is_reference"] and not names["mock-tiny"]["is_reference"]
    column = {c["task_type"]: c for c in body["columns"]}["classification"]
    assert column["run_id"] == run["id"] and column["active_version"] is None
    assert column["criteria"]["min_pass_rate"] == 0.5
    cells = {c["model_id"]: c for c in body["cells"] if c["task_type"] == "classification"}
    tiny = cells[env.candidates["tiny"]]
    assert tiny["is_primary"] and tiny["eligible"] and not tiny["in_active_rule"]
    assert tiny["latency_p95_ms"] is not None and tiny["n_total"] > 0


async def test_the_matrix_marks_the_models_of_the_active_rule(
    client, env, api_app, mock_llm, pipeline
):
    await evaluated_run(client, env, api_app, mock_llm, pipeline, min_pass_rate=0.5)
    rule = await propose(client, env, "mid", "ref")
    assert (await activate(client, env, rule, None)).status_code == 200
    body = (await client.get(f"/projects/{env.project_id}/matrix", headers=env.admin)).json()
    column = {c["task_type"]: c for c in body["columns"]}["classification"]
    assert column["active_version"] == 1 and len(column["active_chain"]) == 2
    marked = {c["model_id"] for c in body["cells"] if c["in_active_rule"]}
    assert env.candidates["mid"] in marked and env.candidates["tiny"] not in marked


async def test_the_matrix_of_a_project_without_runs_has_columns_but_no_cells(client, env):
    body = (await client.get(f"/projects/{env.project_id}/matrix", headers=env.admin)).json()
    assert body["cells"] == []
    assert [c["run_id"] for c in body["columns"]] == [None]


async def test_the_matrix_is_hidden_from_a_stranger(client, admin, env):
    await make_admin(client, admin, "stranger@example.com", "admin")
    stranger = bearer((await login(client, "stranger@example.com", GOOD_PASSWORD)).json())
    r = await client.get(f"/projects/{env.project_id}/matrix", headers=stranger)
    assert r.status_code == 404


# ---- versions, activation, rollback -----------------------------------------------------------
async def test_a_rule_is_proposed_then_activated_by_a_person(client, env, api_app):
    base = f"/projects/{env.project_id}"
    rule = await propose(client, env, "mid", "ref", note="by hand")
    assert (rule["status"], rule["version"]) == ("proposed", 1)
    assert rule["chain_names"] == ["mock-mid", "mock-strong"]

    r = await activate(client, env, rule, None)
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert r.json()["activated_at"] is not None
    again = await activate(client, env, rule, None)  # nothing proposed to activate any more
    assert again.status_code == 409

    got = await client.get(f"{base}/rules/{rule['id']}", headers=env.admin)
    assert got.json()["status"] == "active"
    listed = (await client.get(f"{base}/task-types/classification/rules", headers=env.admin)).json()
    assert [(x["version"], x["status"]) for x in listed] == [(1, "active")]


async def test_a_newer_version_supersedes_the_active_one_and_stale_decisions_are_refused(
    client, env, api_app
):
    first = await propose(client, env, "mid", "ref")
    await activate(client, env, first, None)
    second = await propose(client, env, "tiny", "ref")
    assert second["version"] == 2

    stale = await activate(client, env, second, None)  # "nothing is active" is no longer true
    assert stale.status_code == 409 and "active version is 1" in stale.text
    ok = await activate(client, env, second, 1)
    assert ok.status_code == 200

    base = f"/projects/{env.project_id}/task-types/classification/rules"
    listed = (await client.get(base, headers=env.admin)).json()
    assert [(x["version"], x["status"]) for x in listed] == [(2, "active"), (1, "superseded")]
    assert listed[1]["retired_at"] is not None


async def test_two_people_activating_at_once_cannot_both_win(client, env, api_app):
    a = await propose(client, env, "mid", "ref")
    b = await propose(client, env, "tiny", "ref")
    results = await asyncio.gather(activate(client, env, a, None), activate(client, env, b, None))
    assert sorted(r.status_code for r in results) == [200, 409]
    active = await sql(api_app, "SELECT count(*) AS n FROM routing_rules WHERE status = 'active'")
    assert active[0].n == 1  # the database holds exactly one active version


async def test_the_database_itself_refuses_a_second_active_version(api_app, env):
    project = uuid.UUID(env.project_id)
    insert = (
        "INSERT INTO routing_rules (id, project_id, task_type, version, status, chain, basis)"
        " VALUES (gen_random_uuid(), :p, 'classification', :v, 'active',"
        " ARRAY[CAST(:m AS uuid)], '{}')"
    )
    await sql(api_app, insert, p=project, v=1, m=uuid.UUID(env.reference_id))
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await sql(api_app, insert, p=project, v=2, m=uuid.UUID(env.reference_id))


async def test_a_rollback_brings_the_previous_version_back_in_one_step(client, env, api_app):
    await go_live(client, env, "mid", "ref")
    second = await propose(client, env, "tiny", "ref")
    await activate(client, env, second, 1)

    r = await rollback(client, env)
    assert r.status_code == 200
    assert (r.json()["version"], r.json()["status"]) == (1, "active")
    base = f"/projects/{env.project_id}/task-types/classification/rules"
    listed = (await client.get(base, headers=env.admin)).json()
    assert [(x["version"], x["status"]) for x in listed] == [(2, "rolled_back"), (1, "active")]


async def test_rolling_back_the_only_version_returns_the_task_type_to_record_mode(
    client, env, api_app
):
    await go_live(client, env, "mid", "ref")
    r = await rollback(client, env)
    assert r.status_code == 200 and r.json()["status"] == "rolled_back"
    task = (await client.get(f"/projects/{env.project_id}/task-types", headers=env.admin)).json()
    assert {t["name"]: t["mode"] for t in task}["classification"] == "record"  # never a ghost rule
    again = await rollback(client, env)
    assert again.status_code == 409  # nothing active left


async def test_a_proposed_rule_can_be_rejected_and_then_not_activated(client, env):
    rule = await propose(client, env, "mid", "ref")
    r = await client.post(
        f"/projects/{env.project_id}/rules/{rule['id']}/reject", headers=env.admin
    )
    assert r.json()["status"] == "rejected"
    assert (await activate(client, env, rule, None)).status_code == 409
    assert (
        await client.post(
            f"/projects/{env.project_id}/rules/{rule['id']}/reject", headers=env.admin
        )
    ).status_code == 409


async def test_an_old_version_can_be_activated_again_but_a_rolled_back_one_cannot(client, env):
    one = await go_live(client, env, "mid", "ref")
    two = await propose(client, env, "tiny", "ref")
    await activate(client, env, two, 1)  # v1 is superseded
    back = await activate(client, env, {"id": one["id"]}, 2)  # a person chooses v1 again
    assert back.status_code == 200 and back.json()["status"] == "active"
    await rollback(client, env)  # v1 is rolled back, v2 is active again
    listed = (
        await client.get(
            f"/projects/{env.project_id}/task-types/classification/rules", headers=env.admin
        )
    ).json()
    status = {x["version"]: x["status"] for x in listed}
    assert status == {1: "rolled_back", 2: "active"}
    assert (await activate(client, env, {"id": one["id"]}, 2)).status_code == 409


async def test_activation_is_idempotent_and_every_change_is_announced_and_audited(
    client, env, api_app
):
    rule = await propose(client, env, "mid", "ref")
    first = await activate(client, env, rule, None, **{"Idempotency-Key": "act-1"})
    again = await activate(client, env, rule, None, **{"Idempotency-Key": "act-1"})
    assert first.status_code == again.status_code == 200
    assert again.headers["idempotent-replayed"] == "true"
    other = await propose(client, env, "tiny", "ref")
    await activate(client, env, other, 1)
    await rollback(client, env, **{"Idempotency-Key": "rb-1"})
    await rollback(client, env, **{"Idempotency-Key": "rb-1"})  # a retry of the same request

    events = await sql(
        api_app, "SELECT payload FROM outbox WHERE type = 'rules.changed' ORDER BY seq"
    )
    assert [e.payload["reason"] for e in events] == ["activated:1", "activated:2", "rolled_back:2"]
    actions = await sql(
        api_app,
        "SELECT action FROM audit_log WHERE action LIKE 'routing_rule.%' ORDER BY created_at",
    )
    assert [a.action for a in actions].count("routing_rule.activate") == 2
    assert [a.action for a in actions].count("routing_rule.rollback") == 1
    assert [a.action for a in actions].count("routing_rule.propose") == 2


# ---- what a chain may contain -----------------------------------------------------------------
async def test_a_chain_has_to_be_made_of_known_enabled_models_used_once(client, admin, env):
    base = f"/projects/{env.project_id}/task-types/classification/rules"
    mid, tiny = env.candidates["mid"], env.candidates["tiny"]
    bad = {
        "twice": [mid, mid],
        "unknown": [str(uuid.uuid4())],
        "empty": [],
        "too long": [
            mid,
            tiny,
            env.reference_id,
            env.candidates["judge"],
            str(uuid.uuid4()),
            str(uuid.uuid4()),
        ],
    }
    for name, chain in bad.items():
        r = await client.post(base, json={"chain": chain}, headers=admin)
        assert r.status_code == 422, (name, r.text)
    await client.patch(f"/models/{tiny}", json={"enabled": False}, headers=admin)
    assert (await client.post(base, json={"chain": [tiny]}, headers=admin)).status_code == 422
    unknown_task = f"/projects/{env.project_id}/task-types/nothing/rules"
    assert (
        await client.post(unknown_task, json={"chain": [mid]}, headers=admin)
    ).status_code == 404


async def test_a_model_disabled_after_the_proposal_blocks_the_activation(client, admin, env):
    rule = await propose(client, env, "mid", "ref")
    await client.patch(f"/models/{env.candidates['mid']}", json={"enabled": False}, headers=admin)
    r = await activate(client, env, rule, None)
    assert r.status_code == 422 and "disabled" in r.text


# ---- modes ------------------------------------------------------------------------------------
async def test_route_mode_needs_an_active_rule_and_shadow_mode_needs_models_and_a_share(
    client, env
):
    assert (await set_mode(client, env, "route")).status_code == 422
    models = [env.candidates["tiny"]]
    bad = [
        {"shadow_model_ids": [], "shadow_rate": 0.5},
        {"shadow_model_ids": models, "shadow_rate": 0},
        {"shadow_model_ids": [str(uuid.uuid4())], "shadow_rate": 0.5},
        {"shadow_model_ids": models, "shadow_rate": 1.5},
    ]
    for extra in bad:
        assert (await set_mode(client, env, "shadow", **extra)).status_code == 422, extra
    ok = await set_mode(client, env, "shadow", shadow_model_ids=models, shadow_rate=0.2)
    assert ok.status_code == 200
    assert ok.json()["mode"] == "shadow" and ok.json()["shadow_rate"] == 0.2
    assert ok.json()["shadow_model_ids"] == models
    back = await set_mode(client, env, "record")
    assert back.json()["shadow_model_ids"] == [] and back.json()["shadow_rate"] == 0.0


async def test_mode_changes_are_announced_to_the_gateways(client, env, api_app):
    await go_live(client, env, "mid", "ref")
    await set_mode(client, env, "record")
    reasons = await sql(
        api_app, "SELECT payload FROM outbox WHERE type = 'rules.changed' ORDER BY seq"
    )
    assert [e.payload["reason"] for e in reasons] == ["activated:1", "mode:route", "mode:record"]


# ---- who may do what --------------------------------------------------------------------------
async def test_members_read_admins_change_and_strangers_see_nothing(client, admin, env):
    rule = await propose(client, env, "mid", "ref")
    base = f"/projects/{env.project_id}"

    await make_admin(client, admin, "stranger@example.com", "admin")
    stranger = bearer((await login(client, "stranger@example.com", GOOD_PASSWORD)).json())
    assert (await client.get(f"{base}/rules/{rule['id']}", headers=stranger)).status_code == 404
    assert (
        await client.get(f"{base}/task-types/classification/rules", headers=stranger)
    ).status_code == 404
    assert (
        await client.post(f"{base}/rules/{rule['id']}/activate", json={}, headers=stranger)
    ).status_code == 404

    await make_admin(client, admin, "viewer@example.com", "viewer")
    await client.post(f"{base}/members", json={"email": "viewer@example.com"}, headers=admin)
    viewer = bearer((await login(client, "viewer@example.com", GOOD_PASSWORD)).json())
    assert (await client.get(f"{base}/rules/{rule['id']}", headers=viewer)).status_code == 200
    assert (
        await client.post(f"{base}/rules/{rule['id']}/activate", json={}, headers=viewer)
    ).status_code == 403
    assert (
        await client.post(
            f"{base}/task-types/classification/rules",
            json={"chain": await rule_ids(env, "mid")},
            headers=viewer,
        )
    ).status_code == 403


async def test_a_rule_of_another_project_is_not_reachable_through_this_one(client, admin, env):
    rule = await propose(client, env, "mid", "ref")
    other = (await client.post("/projects", json={"name": "other"}, headers=admin)).json()
    r = await client.get(f"/projects/{other['id']}/rules/{rule['id']}", headers=admin)
    assert r.status_code == 404
