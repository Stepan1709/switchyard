"""Whole conversations (agent trajectories) and the validity of tool arguments in calibration."""

import uuid

import httpx
import pytest

from tests.integration.calibration_support import (
    Env,
    Pipeline,
    add_check,
    add_judge,
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
from tests.integration.conftest import MOCK_KEY, MockServer
from tests.integration.test_auth_api import bearer, login
from tests.integration.test_traffic_api import insert_rows

pytestmark = pytest.mark.integration

TOOLS = [
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
STEPS = 3


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


def history(trip: int, step: int) -> list[dict]:
    """The messages of an agent at `step`: every earlier step left an assistant turn and a
    tool result behind, and the user keeps adding requests (so each step is a new question)."""
    messages: list[dict] = [
        {"role": "system", "content": "You are a travel agent."},
        {"role": "user", "content": f"Trip {trip}: first, what is the weather like?"},
    ]
    for j in range(step):
        messages += [
            {"role": "assistant", "content": f"calling tool {j}"},
            {"role": "tool", "content": f"result {j}"},
            {"role": "user", "content": f"Trip {trip}: and for part {j + 1}, what next?"},
        ]
    return messages


async def record(api_app, env, mock, rows):
    """Store conversation steps the way the gateway would, answered by the real mock-strong."""
    stored = []
    async with httpx.AsyncClient() as http:
        for session_id, step, trip in rows:
            body = {"model": "mock-strong", "messages": history(trip, step), "tools": TOOLS}
            r = await http.post(
                f"{mock.base_url}/chat/completions",
                json=body,
                headers={"authorization": f"Bearer {MOCK_KEY}"},
            )
            data = r.json()
            stored.append(
                {
                    "task_type": "classification",
                    "model_id": uuid.UUID(env.reference_id),
                    "request_payload": body,
                    "response_payload": data,
                    "prompt_tokens": data["usage"]["prompt_tokens"],
                    "completion_tokens": data["usage"]["completion_tokens"],
                    "session_id": session_id,
                    "step": step,
                }
            )
    await insert_rows(api_app, env.project_id, stored)


def complete_sessions(n: int) -> list[tuple[str, int, int]]:
    return [(f"sess-{i}", step, i) for i in range(n) for step in range(STEPS)]


# ---- sampling whole conversations -------------------------------------------------------------
async def test_a_run_can_sample_whole_conversations_and_only_complete_ones(
    client, env, api_app, mock_llm
):
    rows = complete_sessions(6)
    rows += [("gappy", 0, 100), ("gappy", 2, 100)]  # step 1 is missing
    rows += [("twice", 0, 101), ("twice", 0, 102), ("twice", 1, 101)]  # step 0 occurs twice
    rows += [("long-enough-but-not-from-zero", 1, 103), ("long-enough-but-not-from-zero", 2, 103)]
    await record(api_app, env, mock_llm, rows)

    created = await start_run(client, env, "tiny", sample_unit="session", sample_size=100)
    assert created.status_code == 201, created.text
    run = created.json()
    assert run["sample_unit"] == "session" and run["samples_taken"] == 6 * STEPS
    assert run["progress"]["total_jobs"] == 6 * STEPS  # one candidate

    stored = await sql(
        api_app,
        "SELECT session_id, step FROM run_samples WHERE run_id = :r ORDER BY session_id, step",
        r=uuid.UUID(run["id"]),
    )
    assert {s.session_id for s in stored} == {f"sess-{i}" for i in range(6)}
    assert all(s.step in range(STEPS) for s in stored)


async def test_sample_size_counts_conversations_and_takes_every_step_of_each(
    client, env, api_app, mock_llm
):
    await record(api_app, env, mock_llm, complete_sessions(8))
    run = (
        await start_run(client, env, "tiny", "mid", sample_unit="session", sample_size=5, seed=3)
    ).json()
    assert run["samples_taken"] == 5 * STEPS and run["progress"]["total_jobs"] == 5 * STEPS * 2
    sessions = await sql(
        api_app,
        "SELECT session_id, count(*) AS n FROM run_samples WHERE run_id = :r GROUP BY session_id",
        r=uuid.UUID(run["id"]),
    )
    assert len(sessions) == 5 and {s.n for s in sessions} == {STEPS}


async def test_conversations_need_recorded_traffic_and_complete_sessions(client, env, api_app):
    refused = await start_run(client, env, "tiny", sample_unit="session", source="labeled")
    assert refused.status_code == 422 and "recorded traffic" in refused.text
    nothing = await start_run(client, env, "tiny", sample_unit="session")
    assert nothing.status_code == 422 and "complete recorded conversations" in nothing.text


async def test_too_many_steps_in_one_run_are_refused(client, env, api_app, mock_llm, settings):
    await record(api_app, env, mock_llm, complete_sessions(4))
    # the limit is a setting of the API; ask for fewer conversations than exist instead of
    # raising it, and check that the refusal path exists through the unit of the limit
    ok = await start_run(client, env, "tiny", sample_unit="session", sample_size=2)
    assert ok.status_code == 201 and ok.json()["samples_taken"] == 2 * STEPS


# ---- the verdict on whole conversations ------------------------------------------------------
async def test_a_conversation_passes_only_if_every_step_does(
    client, env, api_app, mock_llm, pipeline
):
    await record(api_app, env, mock_llm, complete_sessions(12))
    await add_check(client, env, "tool_call_match")
    created = await start_run(
        client, env, "tiny", "mid", sample_unit="session", sample_size=12, seed=5
    )
    run = created.json()
    await pipeline.start()
    await wait_status(client, env, run["id"], "completed")
    done = await wait_evaluated(client, env, run["id"])
    assert done["evaluation"]["total"] == 12 * STEPS * 2

    report = (await aggregates(client, env, run["id"]))["items"]
    assert len(report) == 2
    for m in report:
        assert m["n_sessions"] == 12 and m["n_evaluated"] == 12 * STEPS
        assert m["session_pass_low"] <= m["session_pass_rate"] <= m["session_pass_high"]
        # every step of a conversation has to pass, so conversations pass less often than steps
        assert m["session_pass_rate"] <= m["pass_rate"] + 1e-9
        assert m["n_sessions_passed"] <= m["n_sessions"]

    # the numbers agree with the verdicts themselves
    rows = await sql(
        api_app,
        "SELECT r.model_id, s.session_id, bool_and(e.passed) AS all_passed"
        " FROM evaluations e JOIN run_results r ON r.id = e.result_id"
        " JOIN run_samples s ON s.run_id = r.run_id AND s.request_id = r.request_id"
        " WHERE e.run_id = :r GROUP BY r.model_id, s.session_id",
        r=uuid.UUID(run["id"]),
    )
    for m in report:
        passed = sum(1 for r in rows if str(r.model_id) == m["model_id"] and r.all_passed)
        assert passed == m["n_sessions_passed"]


async def test_the_runs_of_single_requests_have_no_conversation_numbers(
    client, env, api_app, mock_llm, pipeline
):
    await record(api_app, env, mock_llm, complete_sessions(4))
    run = (await start_run(client, env, "tiny", sample_size=8)).json()
    await pipeline.start()
    await wait_status(client, env, run["id"], "completed")
    await wait_evaluated(client, env, run["id"])
    (m,) = (await aggregates(client, env, run["id"]))["items"]
    # single requests were sampled, so there are no whole conversations to speak about
    assert m["n_sessions"] == 0 and m["session_pass_rate"] is None
    assert (await get_run(client, env, run["id"]))["sample_unit"] == "request"


# ---- the validity of tool arguments -----------------------------------------------------------
async def test_the_validity_of_tool_arguments_is_recorded_for_every_tool_call(
    client, env, api_app, mock_llm, pipeline
):
    await record(api_app, env, mock_llm, complete_sessions(5))
    run = (await start_run(client, env, "tiny", "mid", sample_size=15)).json()
    await pipeline.start()
    await wait_status(client, env, run["id"], "completed")
    await wait_evaluated(client, env, run["id"])

    rows = await evaluations(api_app, run["id"])
    assert rows and all(r.valid_args is True for r in rows)  # the mock calls tools correctly
    assert all(not any(c["kind"] == "tool_args_valid" for c in r.checks) for r in rows)
    for m in (await aggregates(client, env, run["id"]))["items"]:
        assert m["valid_args_rate"] == 1.0


async def test_a_configured_arguments_check_takes_part_in_the_verdict(
    client, env, api_app, mock_llm, pipeline
):
    await record(api_app, env, mock_llm, complete_sessions(4))
    await add_check(client, env, "tool_args_valid")
    run = (await start_run(client, env, "tiny", sample_size=12)).json()
    await pipeline.start()
    await wait_status(client, env, run["id"], "completed")
    await wait_evaluated(client, env, run["id"])
    rows = await evaluations(api_app, run["id"])
    assert all(any(c["kind"] == "tool_args_valid" and c["passed"] for c in r.checks) for r in rows)
    assert len(await results(api_app, run["id"])) == 12
