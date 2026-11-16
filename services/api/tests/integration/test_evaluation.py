"""Quality evaluation of calibration runs: checks, the judge, aggregates (real PostgreSQL, Redis,
RabbitMQ and a real mock_llm provider whose models have known, different accuracy)."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.messaging import Message
from app.modules.calibration import replay
from app.modules.evaluation.evaluator import EvaluationStarter, Evaluator
from tests.integration.calibration_support import (
    Env,
    Pipeline,
    add_check,
    add_judge,
    add_traffic_from_mock,
    aggregates,
    build_env,
    evaluations,
    get_run,
    results,
    sql,
    start_run,
    wait_evaluated,
    wait_status,
)
from tests.integration.conftest import MockServer
from tests.integration.test_auth_api import GOOD_PASSWORD, bearer, login, make_admin

pytestmark = pytest.mark.integration

N = 60  # enough answers for the numbers to mean something (the minimum is 30)


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
    await add_traffic_from_mock(api_app, env, mock_llm.base_url, N)
    await add_judge(client, admin, env)
    return env


@pytest.fixture
async def pipeline(api_app, settings, amqp, publisher):
    p = Pipeline(api_app, settings, amqp, publisher)
    yield p
    await p.stop()


async def run_to_the_end(client, env, pipeline, *candidates, **extra):
    extra.setdefault("sample_size", N)
    created = await start_run(client, env, *candidates, **extra)
    assert created.status_code == 201, created.text
    run = created.json()
    await pipeline.start()
    await wait_status(client, env, run["id"], "completed", "budget_exceeded")
    return await wait_evaluated(client, env, run["id"])


def by_model(report, env) -> dict[str, dict]:
    names = {v: k for k, v in env.candidates.items()}
    return {names[i["model_id"]]: i for i in report["items"]}


# ---- the whole path ---------------------------------------------------------------------------
async def test_the_stronger_model_agrees_more_with_the_reference_and_the_judge_only_sees_differences(
    client, env, api_app, pipeline
):
    await add_check(client, env, "exact_match")
    run = await run_to_the_end(
        client, env, pipeline, "tiny", "mid", judge_model_id=env.candidates["judge"]
    )
    assert run["evaluation"] == {"status": "done", "total": 2 * N, "done": 2 * N}

    report = await aggregates(client, env, run["id"])
    assert report["evaluation_status"] == "done" and report["min_samples"] == 30
    models = by_model(report, env)
    tiny, mid = models["tiny"], models["mid"]

    # the mock models are built to differ: mid is more accurate than tiny, and the numbers
    # of the evaluation show exactly that
    assert tiny["n_evaluated"] == mid["n_evaluated"] == N and not tiny["insufficient_data"]
    assert mid["pass_rate"] > tiny["pass_rate"] + 0.1
    assert mid["quality_mean"] > tiny["quality_mean"]
    assert tiny["quality_p10"] <= tiny["quality_mean"]
    for m in (tiny, mid):
        assert m["pass_rate_low"] <= m["pass_rate"] <= m["pass_rate_high"]
        assert m["basis_counts"] == {"reference": N}
        assert m["latency_p95_ms"] >= m["latency_p50_ms"] > 0 and m["ttft_p50_ms"] > 0
        assert m["prompt_tokens_avg"] > 0 and m["completion_tokens_avg"] > 0
        assert m["error_rate"] == 0 and m["n_judge_failed"] == 0
    assert Decimal(str(tiny["cost_per_request_usd"])) < Decimal(str(mid["cost_per_request_usd"]))

    rows = await evaluations(api_app, run["id"])
    same = [r for r in rows if r.equal_to_truth]
    differing = [r for r in rows if not r.equal_to_truth]
    assert same and differing
    # an answer equal to the reference is full marks and costs no judge call at all ...
    bad = [r for r in same if not (r.quality == 1.0 and r.passed and r.judge_calls == 0)]
    assert not bad, [(r.status, r.quality, r.passed, r.judge_calls, r.checks) for r in bad[:2]]
    # ... only different answers are put in front of the judge, whose reasoning is kept
    assert all(r.judge_calls >= 1 and r.judge_score in range(1, 6) for r in differing)
    assert all(r.judge_rationale and r.judge_mode == "grade" for r in differing)
    assert all(r.judge_prompt_version == 1 for r in differing)
    assert all(r.passed is False for r in differing)  # the required exact_match check failed
    assert sum(r.judge_calls for r in rows) == len(differing)
    assert Decimal(str(tiny["judge_cost_usd"])) > 0


async def test_without_a_judge_the_checks_alone_decide(client, env, api_app, pipeline):
    await add_check(client, env, "exact_match")
    run = await run_to_the_end(client, env, pipeline, "tiny", "mid")
    rows = await evaluations(api_app, run["id"])
    assert all(r.judge_calls == 0 and r.judge_mode is None for r in rows)
    assert all((r.passed is True) == r.equal_to_truth for r in rows)
    assert all(r.quality == (1.0 if r.equal_to_truth else 0.0) for r in rows)
    report = by_model(await aggregates(client, env, run["id"]), env)
    assert report["tiny"]["judge_cost_usd"] is None


async def test_with_no_checks_and_no_judge_an_answer_must_match_the_reference(
    client, env, api_app, pipeline
):
    run = await run_to_the_end(client, env, pipeline, "tiny", "mid")
    rows = await evaluations(api_app, run["id"])
    assert {r.status for r in rows} == {"ok"}
    assert all((r.passed is True) == r.equal_to_truth for r in rows)
    assert all(r.checks[0]["kind"] == "same_as_truth" for r in rows)


async def test_results_are_reproducible(client, env, api_app, pipeline):
    await add_check(client, env, "exact_match")
    judge = env.candidates["judge"]
    first = await run_to_the_end(client, env, pipeline, "tiny", "mid", seed=7, judge_model_id=judge)
    second = await run_to_the_end(
        client, env, pipeline, "tiny", "mid", seed=7, judge_model_id=judge
    )
    assert first["id"] != second["id"]
    a = by_model(await aggregates(client, env, first["id"]), env)
    b = by_model(await aggregates(client, env, second["id"]), env)
    for name in ("tiny", "mid"):
        for key in ("pass_rate", "quality_mean", "quality_p10", "n_evaluated", "basis_counts"):
            assert a[name][key] == b[name][key], (name, key)


# ---- the judge --------------------------------------------------------------------------------
async def test_a_judge_that_gives_no_verdict_is_not_a_zero_score(
    client, env, api_app, pipeline, monkeypatch
):
    real = replay.call_candidate

    async def rambling_judge(http, settings, target, request, **kw):
        if target.model_name == "mock-judge":
            reply = {"choices": [{"message": {"role": "assistant", "content": "no idea, sorry"}}]}
            return replay.Outcome("ok", 200, response=reply, prompt_tokens=50, completion_tokens=4)
        return await real(http, settings, target, request, **kw)

    monkeypatch.setattr(replay, "call_candidate", rambling_judge)
    run = await run_to_the_end(
        client, env, pipeline, "tiny", judge_model_id=env.candidates["judge"]
    )

    rows = await evaluations(api_app, run["id"])
    failed = [r for r in rows if r.status == "judge_failed"]
    assert failed and all(r.quality is None and r.passed is None for r in failed)
    assert all(r.judge_calls == 3 for r in failed)  # one try and two repeats
    assert all(r.judge_error == "invalid_reply" for r in failed)
    assert all(r.status == "ok" and r.equal_to_truth for r in rows if r not in failed)

    tiny = by_model(await aggregates(client, env, run["id"]), env)["tiny"]
    assert tiny["n_judge_failed"] == len(failed)
    assert tiny["n_evaluated"] + tiny["n_judge_failed"] == tiny["n_ok"]  # left out, not zeroed
    assert tiny["pass_rate"] == 1.0  # of the answers that WERE evaluated (all equal to the truth)


async def test_the_judge_can_compare_two_answers_in_both_orders(client, env, api_app, pipeline):
    run = await run_to_the_end(
        client, env, pipeline, "tiny", judge_model_id=env.candidates["judge"], judge_pairwise=True
    )
    rows = await evaluations(api_app, run["id"])
    differing = [r for r in rows if not r.equal_to_truth]
    assert differing
    for r in differing:
        assert r.judge_mode == "pairwise" and r.judge_calls == 2  # the order was swapped
        first, second = r.judge_verdict.split("|")
        assert {first, second} <= {"candidate", "truth"}
        expected = {("candidate", "candidate"): 1.0, ("truth", "truth"): 0.0}.get(
            (first, second), 0.5
        )
        assert r.quality == expected
        assert r.passed is (expected == 1.0)
    assert all(r.judge_calls == 0 for r in rows if r.equal_to_truth)


async def test_the_judge_must_not_judge_itself(client, env):
    cases = {
        "a candidate": {"judge_model_id": env.candidates["tiny"]},
        "the reference": {"judge_model_id": env.reference_id},
        "an unknown model": {"judge_model_id": str(uuid.uuid4())},
        "pairwise without a judge": {"judge_pairwise": True},
    }
    for name, extra in cases.items():
        r = await start_run(client, env, "tiny", **extra)
        assert r.status_code == 422, (name, r.text)
    ok = await start_run(client, env, "tiny", judge_model_id=env.candidates["judge"])
    assert ok.status_code == 201 and ok.json()["judge_model_id"] == env.candidates["judge"]


async def test_a_disabled_judge_is_refused(client, admin, env):
    r = await client.patch(
        f"/models/{env.candidates['judge']}", json={"enabled": False}, headers=admin
    )
    assert r.status_code == 200
    refused = await start_run(client, env, "tiny", judge_model_id=env.candidates["judge"])
    assert refused.status_code == 422 and "disabled" in refused.text


# ---- what kind of answers ---------------------------------------------------------------------
async def test_tool_calls_are_compared_not_executed(client, env, api_app, pipeline, mock_llm):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_time",
                "parameters": {"type": "object", "properties": {"zone": {"type": "string"}}},
            },
        },
    ]
    await sql(api_app, "DELETE FROM requests")
    env.prompts.clear()
    await add_traffic_from_mock(
        api_app, env, mock_llm.base_url, 40, extra={"tools": tools}, prefix="Agent"
    )
    await add_check(client, env, "tool_call_match")
    run = await run_to_the_end(client, env, pipeline, "tiny", "mid", sample_size=40)

    rows = await evaluations(api_app, run["id"])
    assert len(rows) == 80
    assert all(r.checks[0]["kind"] == "tool_call_match" for r in rows)
    assert all((r.passed is True) == r.equal_to_truth for r in rows)  # the same decision, or not
    wrong = [r for r in rows if not r.passed]
    assert wrong and {r.checks[0]["detail"] for r in wrong} <= {
        "different tool calls",
        "right tool, different arguments",
    }
    stored = await results(api_app, run["id"])
    assert all(r.has_tool_calls and r.finish_reason == "tool_calls" for r in stored)


async def test_json_schema_validity_is_reported(client, env, api_app, pipeline, mock_llm):
    await sql(api_app, "DELETE FROM requests")
    env.prompts.clear()
    await add_traffic_from_mock(
        api_app, env, mock_llm.base_url, 40, extra={"response_format": {"type": "json_object"}}
    )
    good = {"type": "object", "required": ["label"], "properties": {"label": {"type": "string"}}}
    check = await add_check(client, env, "json_schema", {"schema": good})
    run = await run_to_the_end(client, env, pipeline, "tiny", sample_size=40)
    tiny = by_model(await aggregates(client, env, run["id"]), env)["tiny"]
    assert tiny["valid_rate"] == 1.0 and tiny["pass_rate"] == 1.0

    r = await client.delete(f"/projects/{env.project_id}/checks/{check['id']}", headers=env.admin)
    assert r.status_code == 204
    strict = {"type": "object", "required": ["label", "confidence"]}
    await add_check(client, env, "json_schema", {"schema": strict})
    run = await run_to_the_end(client, env, pipeline, "tiny", sample_size=40)
    tiny = by_model(await aggregates(client, env, run["id"]), env)["tiny"]
    assert tiny["valid_rate"] == 0.0 and tiny["pass_rate"] == 0.0


async def test_a_labelled_example_outranks_the_reference_answer(
    client, env, api_app, pipeline, mock_llm
):
    import json

    import httpx

    from tests.integration.conftest import MOCK_KEY

    await sql(api_app, "DELETE FROM requests")  # only the labelled examples are used
    lines = []
    async with httpx.AsyncClient() as http:
        for i in range(40):
            body = {"messages": [{"role": "user", "content": f"Labelled text {i}: classify it"}]}
            r = await http.post(
                f"{mock_llm.base_url}/chat/completions",
                json={**body, "model": "mock-strong"},
                headers={"authorization": f"Bearer {MOCK_KEY}"},
            )
            lines.append(
                json.dumps({**body, "expected": r.json()["choices"][0]["message"]["content"]})
            )
    imported = await client.post(
        f"/projects/{env.project_id}/task-types/classification/labeled-samples",
        content="\n".join(lines).encode(),
        headers={**env.admin, "content-type": "application/x-ndjson"},
    )
    assert imported.json()["imported"] == 40
    await add_check(client, env, "exact_match")
    run = await run_to_the_end(
        client, env, pipeline, "tiny", "mid", source="labeled", sample_size=40
    )
    for model in by_model(await aggregates(client, env, run["id"]), env).values():
        assert model["basis_counts"] == {"expected": 40}
    rows = await evaluations(api_app, run["id"])
    assert {r.basis for r in rows} == {"expected"}


# ---- robustness -------------------------------------------------------------------------------
async def test_a_repeated_job_stores_one_verdict(client, env, api_app, pipeline, settings):
    await add_check(client, env, "exact_match")
    run = await run_to_the_end(client, env, pipeline, "tiny")
    first = (
        await sql(
            api_app, "SELECT id FROM run_results WHERE run_id = :r LIMIT 1", r=uuid.UUID(run["id"])
        )
    )[0]
    before = len(await evaluations(api_app, run["id"]))
    evaluator = Evaluator(api_app.state.resources, settings, api_app.state.keyring)
    await evaluator.handle(
        Message("judge.execute", {"run_id": run["id"], "result_id": str(first.id)})
    )
    assert len(await evaluations(api_app, run["id"])) == before


async def test_a_repeated_end_of_run_event_starts_the_evaluation_once(
    client, env, api_app, pipeline, settings
):
    run = await run_to_the_end(client, env, pipeline, "tiny")
    jobs = await sql(api_app, "SELECT count(*) AS n FROM outbox WHERE type = 'judge.execute'")
    starter = EvaluationStarter(api_app.state.resources, settings)
    await starter.handle(Message("calibration.run.finished", {"run_id": run["id"]}))
    after = await sql(api_app, "SELECT count(*) AS n FROM outbox WHERE type = 'judge.execute'")
    assert jobs[0].n == after[0].n == N


async def test_a_run_without_usable_answers_is_still_evaluated_and_reported(
    client, admin, env, api_app, pipeline
):
    shrunk = await client.patch(
        f"/models/{env.candidates['tiny']}", json={"context_window": 10}, headers=admin
    )
    assert shrunk.status_code == 200, shrunk.text
    run = (await start_run(client, env, "tiny", sample_size=10)).json()
    await pipeline.start()
    done = await wait_evaluated(client, env, run["id"])
    assert done["evaluation"] == {"status": "done", "total": 0, "done": 0}
    (agg,) = (await aggregates(client, env, run["id"]))["items"]
    assert agg["n_context_exceeded"] == 10 and agg["n_ok"] == 0
    assert agg["quality_mean"] is None and agg["error_rate"] is None and agg["insufficient_data"]


async def test_a_small_run_is_marked_as_not_enough_data(client, env, api_app, pipeline):
    run = await run_to_the_end(client, env, pipeline, "tiny", sample_size=8)
    (agg,) = (await aggregates(client, env, run["id"]))["items"]
    assert agg["n_evaluated"] == 8 and agg["insufficient_data"] is True


async def test_aggregates_are_empty_until_the_evaluation_is_done(client, env):
    run = (await start_run(client, env, "tiny")).json()
    report = await aggregates(client, env, run["id"])
    assert report["items"] == [] and report["evaluation_status"] == "pending"
    assert (await get_run(client, env, run["id"]))["evaluation"]["status"] == "pending"


async def test_evaluations_can_be_listed_filtered_and_paged(client, env, api_app, pipeline):
    run = await run_to_the_end(client, env, pipeline, "tiny", "mid")
    base = f"/projects/{env.project_id}/calibration-runs/{run['id']}/evaluations"
    seen, cursor = [], None
    while True:
        page = (
            await client.get(
                base,
                params={"limit": 25, **({"cursor": cursor} if cursor else {})},
                headers=env.admin,
            )
        ).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 2 * N

    failed = (await client.get(base, params={"passed": "false"}, headers=env.admin)).json()
    assert failed["items"] and all(i["passed"] is False for i in failed["items"])
    assert all(i["model_id"] and i["request_id"] for i in failed["items"])
    tiny = (
        await client.get(
            base, params={"model_id": env.candidates["tiny"], "limit": 200}, headers=env.admin
        )
    ).json()
    assert len(tiny["items"]) == N
    assert (await client.get(base, params={"cursor": "zzz"}, headers=env.admin)).status_code == 422


# ---- the checks of a task type ----------------------------------------------------------------
async def test_checks_are_managed_through_the_api(client, admin, env, api_app):
    base = f"/projects/{env.project_id}/task-types/classification/checks"
    created = await add_check(client, env, "regex", {"pattern": "^pos"}, required=False, position=2)
    assert created["required"] is False and created["position"] == 2
    await add_check(client, env, "exact_match")
    listed = (await client.get(base, headers=admin)).json()
    assert [c["kind"] for c in listed] == ["exact_match", "regex"]  # by position

    bad = [
        {"kind": "regex", "config": {"pattern": "("}},
        {"kind": "json_schema", "config": {"schema": {"$ref": "x"}}},
        {"kind": "llm_judge", "config": {"min_score": 9}},
        {"kind": "exact_match", "config": {"nope": 1}},
        {"kind": "nonsense", "config": {}},
    ]
    for body in bad:
        assert (await client.post(base, json=body, headers=admin)).status_code == 422, body
    await add_check(client, env, "llm_judge", {"min_score": 3, "rubric": "be exact"})
    second = await client.post(base, json={"kind": "llm_judge", "config": {}}, headers=admin)
    assert second.status_code == 422 and "at most one" in second.text

    gone = await client.delete(f"/projects/{env.project_id}/checks/{created['id']}", headers=admin)
    assert gone.status_code == 204
    assert (
        await client.delete(f"/projects/{env.project_id}/checks/{created['id']}", headers=admin)
    ).status_code == 404
    unknown = await client.get(
        f"/projects/{env.project_id}/task-types/nothing/checks", headers=admin
    )
    assert unknown.status_code == 404
    actions = await sql(api_app, "SELECT action FROM audit_log WHERE action LIKE 'task_check.%'")
    assert {a.action for a in actions} == {"task_check.create", "task_check.delete"}


async def test_only_members_see_checks_and_only_admins_change_them(client, admin, env):
    base = f"/projects/{env.project_id}/task-types/classification/checks"
    await make_admin(client, admin, "outsider@example.com", "admin")
    outsider = bearer((await login(client, "outsider@example.com", GOOD_PASSWORD)).json())
    assert (await client.get(base, headers=outsider)).status_code == 404
    assert (
        await client.post(base, json={"kind": "exact_match"}, headers=outsider)
    ).status_code == 404

    await make_admin(client, admin, "reader@example.com", "viewer")
    await client.post(
        f"/projects/{env.project_id}/members", json={"email": "reader@example.com"}, headers=admin
    )
    reader = bearer((await login(client, "reader@example.com", GOOD_PASSWORD)).json())
    assert (await client.get(base, headers=reader)).status_code == 200
    assert (
        await client.post(base, json={"kind": "exact_match"}, headers=reader)
    ).status_code == 403


async def test_a_run_of_another_project_has_no_aggregates_here(client, admin, env):
    run = (await start_run(client, env, "tiny")).json()
    other = (await client.post("/projects", json={"name": "other"}, headers=admin)).json()
    r = await client.get(
        f"/projects/{other['id']}/calibration-runs/{run['id']}/aggregates", headers=admin
    )
    assert r.status_code == 404


# ---- human verdicts (storage only) ------------------------------------------------------------
async def test_human_verdicts_have_a_home_and_one_per_person_and_result(
    client, env, api_app, pipeline
):
    await add_check(client, env, "exact_match")
    run = await run_to_the_end(client, env, pipeline, "tiny", sample_size=3)
    result = (
        await sql(
            api_app, "SELECT id FROM run_results WHERE run_id = :r LIMIT 1", r=uuid.UUID(run["id"])
        )
    )[0]
    admin_id = (await sql(api_app, "SELECT id FROM admins LIMIT 1"))[0].id
    insert = (
        "INSERT INTO human_verdicts (id, result_id, admin_id, verdict) "
        "VALUES (gen_random_uuid(), :r, :a, 'pass')"
    )
    await sql(api_app, insert, r=result.id, a=admin_id)
    with pytest.raises(IntegrityError):
        await sql(api_app, insert, r=result.id, a=admin_id)
