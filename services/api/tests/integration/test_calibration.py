"""Calibration runs: sampling, replay on candidates, budgets, steering, idempotency (real
PostgreSQL, Redis, RabbitMQ and a real mock_llm provider)."""

import asyncio
import json
import uuid
from decimal import Decimal

import pytest

from app.core.messaging import Message, PermanentError, TemporaryError
from app.modules.calibration import replay
from app.modules.calibration.executor import ReplayExecutor
from tests.integration.calibration_support import (
    Env,
    Pipeline,
    add_model,
    add_traffic,
    answer,
    build_env,
    get_run,
    results,
    run_body,
    set_price,
    sql,
    start_run,
    wait_status,
)
from tests.integration.conftest import MockServer, wait_until
from tests.integration.test_auth_api import GOOD_PASSWORD, bearer, login, make_admin
from tests.integration.test_traffic_api import insert_rows

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
def settings(app_settings, queue_name):
    """Private queues per test, short retry delays, generous provider limits."""
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
    return await build_env(client, admin, api_app, mock_llm.base_url)


@pytest.fixture
async def pipeline(api_app, settings, amqp, publisher):
    p = Pipeline(api_app, settings, amqp, publisher)
    yield p
    await p.stop()


@pytest.fixture
def calls(monkeypatch):
    """Record every request that is sent to a candidate, then really send it."""
    seen: list[tuple[str, dict]] = []
    real = replay.call_candidate

    async def spy(http, settings, target, request, **kw):
        seen.append((target.model_name, request))
        return await real(http, settings, target, request, **kw)

    monkeypatch.setattr(replay, "call_candidate", spy)
    return seen


# ---- the happy path ---------------------------------------------------------------------------
async def test_a_run_replays_every_request_on_every_candidate_and_completes(
    client, env, api_app, pipeline, calls
):
    created = await start_run(client, env)
    assert created.status_code == 201, created.text
    run = created.json()
    assert (run["status"], run["samples_taken"]) == ("running", 6)
    assert run["progress"]["total_jobs"] == 12 and run["progress"]["finished"] == 0

    await pipeline.start()
    done = await wait_status(client, env, run["id"], "completed")

    assert done["progress"]["finished"] == 12 and done["progress"]["percent"] == 100.0
    assert done["finished_at"] is not None
    rows = await results(api_app, run["id"])
    assert len(rows) == 12 and {r.status for r in rows} == {"ok"}
    assert len({(r.request_id, r.model_id) for r in rows}) == 12  # every pair exactly once
    assert len(calls) == 12  # one provider call per job: nothing was repeated

    for r in rows:  # measurements and the price that applied
        assert r.prompt_tokens and r.completion_tokens
        assert r.ttft_ms is not None and r.latency_ms >= r.ttft_ms
        expected = (
            r.prompt_tokens * r.price_input_per_1m + r.completion_tokens * r.price_output_per_1m
        ) / Decimal(1_000_000)
        assert r.cost_usd == expected.quantize(Decimal("0.00000001"))
    tiny = {r.price_input_per_1m for r in rows if str(r.model_id) == env.candidates["tiny"]}
    assert tiny == {Decimal("0.100000")}
    assert done["progress"]["spent_usd"] != "0"


async def test_the_end_of_a_run_is_announced_once_through_the_outbox(
    client, env, api_app, pipeline
):
    run = (await start_run(client, env)).json()
    await pipeline.start()
    await wait_status(client, env, run["id"], "completed")

    async def delivered():
        rows = await sql(
            api_app,
            "SELECT payload, sent_at FROM outbox WHERE type = 'calibration.run.finished'",
        )
        return rows if rows and all(r.sent_at for r in rows) else None

    rows = await wait_until(delivered)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["run_id"] == run["id"] and payload["status"] == "completed"
    assert payload["results"] == 12 and payload["calls"] == 12


async def test_the_samples_are_copied_so_the_run_survives_the_loss_of_traffic(
    client, env, api_app, pipeline
):
    run = (await start_run(client, env)).json()
    await sql(api_app, "DELETE FROM requests")  # the traffic partition is gone
    await pipeline.start()
    done = await wait_status(client, env, run["id"], "completed")
    assert done["progress"]["by_status"] == {"ok": 12}
    samples = await sql(
        api_app,
        "SELECT reference_response, reference_prompt_tokens FROM run_samples WHERE run_id = :r",
        r=uuid.UUID(run["id"]),
    )
    assert len(samples) == 6 and all(s.reference_prompt_tokens == 12 for s in samples)


# ---- idempotency ------------------------------------------------------------------------------
async def test_the_same_idempotency_key_starts_one_run_and_queues_the_jobs_once(
    client, env, api_app
):
    first = await start_run(client, env, key="run-1")
    again = await start_run(client, env, key="run-1")
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert again.headers["idempotent-replayed"] == "true"
    assert (await sql(api_app, "SELECT count(*) AS n FROM calibration_runs"))[0].n == 1
    jobs = await sql(api_app, "SELECT count(*) AS n FROM outbox WHERE type = 'replay.execute'")
    assert jobs[0].n == 12

    other = await start_run(client, env, "tiny", key="run-1")  # same key, another request
    assert other.status_code == 422


async def test_a_run_without_a_key_is_a_new_run_each_time(client, env, api_app):
    await start_run(client, env)
    await start_run(client, env)
    assert (await sql(api_app, "SELECT count(*) AS n FROM calibration_runs"))[0].n == 2


async def test_a_repeated_job_makes_neither_a_second_call_nor_a_second_result(
    client, env, api_app, settings, calls
):
    run = (await start_run(client, env, "tiny", sample_size=1)).json()
    job = (await sql(api_app, "SELECT payload FROM outbox WHERE type = 'replay.execute'"))[
        0
    ].payload
    executor = ReplayExecutor(api_app.state.resources, settings, api_app.state.keyring)
    message = Message("replay.execute", job)

    await executor.handle(message)
    await executor.handle(message)  # the broker delivered it again
    assert len(calls) == 1
    assert len(await results(api_app, run["id"])) == 1
    assert (await get_run(client, env, run["id"]))["status"] == "completed"


# ---- budgets ----------------------------------------------------------------------------------
async def test_the_request_budget_stops_the_run_with_a_clear_status(
    client, env, api_app, pipeline, calls
):
    run = (await start_run(client, env, max_requests=5)).json()
    await pipeline.start()
    stopped = await wait_status(client, env, run["id"], "budget_exceeded")

    assert "budget" in stopped["status_reason"]
    assert stopped["progress"]["calls"] == 5 and stopped["progress"]["remaining_requests"] == 0
    await asyncio.sleep(0.8)  # nothing more happens afterwards
    assert len(await results(api_app, run["id"])) == 5
    assert len(calls) == 5
    assert stopped["finished_at"] is not None


async def test_the_money_budget_is_not_exceeded_even_with_parallel_jobs(
    client, env, api_app, pipeline
):
    limit = Decimal("0.0002")
    run = (await start_run(client, env, "mid", sample_size=6, max_cost_usd=str(limit))).json()
    await pipeline.start()
    stopped = await wait_status(client, env, run["id"], "budget_exceeded")

    done = await results(api_app, run["id"])
    assert 1 <= len(done) < 6  # it started, and stopped before the end
    assert sum(r.cost_usd for r in done) <= limit
    assert Decimal(str(stopped["progress"]["spent_usd"])) <= limit


# ---- context, tools, errors -------------------------------------------------------------------
async def test_a_prompt_that_does_not_fit_is_recorded_and_never_sent(
    client, admin, env, api_app, pipeline, calls
):
    small = await add_model(
        client, admin, env.stack.provider["id"], "mock-judge", context_window=10
    )
    await set_price(client, admin, small)
    env.candidates["small"] = small

    run = (await start_run(client, env, "small", "mid")).json()
    await pipeline.start()
    done = await wait_status(client, env, run["id"], "completed")

    assert done["progress"]["by_status"] == {"context_exceeded": 6, "ok": 6}
    assert {m for m, _ in calls} == {"mock-mid"}  # the small model was never called
    skipped = [r for r in await results(api_app, run["id"]) if r.status == "context_exceeded"]
    assert all(r.cost_usd is None and r.prompt_tokens is None for r in skipped)
    assert done["progress"]["calls"] == 6


async def test_tools_are_sent_as_definitions_and_never_executed(
    client, env, api_app, pipeline, calls
):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    history = [
        {"role": "user", "content": "weather in Paris?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
    ]  # fmt: skip
    await sql(api_app, "DELETE FROM requests")
    await insert_rows(
        api_app,
        env.project_id,
        [
            {
                "task_type": "classification",
                "model_id": uuid.UUID(env.reference_id),
                "request_payload": {"messages": history[:1], "tools": tools, "tool_choice": "auto"},
                "response_payload": answer(),
            },
            {
                "task_type": "classification",
                "model_id": uuid.UUID(env.reference_id),
                "request_payload": {"messages": history, "tools": tools},
                "response_payload": answer(),
            },
        ],
    )
    run = (await start_run(client, env, "mid", sample_size=2)).json()
    await pipeline.start()
    done = await wait_status(client, env, run["id"], "completed")

    assert done["progress"]["by_status"] == {"ok": 2}
    assert len(calls) == 2  # exactly one call per job: a tool call is never followed up
    for _, sent in calls:
        assert sent["tools"] == tools  # definitions go along unchanged ...
    sent_histories = sorted(len(sent["messages"]) for _, sent in calls)
    assert sent_histories == [1, 3]  # ... and the recorded history is replayed as it was
    stored = await results(api_app, run["id"])
    assert all(r.has_tool_calls and r.finish_reason == "tool_calls" for r in stored)


async def test_a_failing_provider_is_retried_and_finally_recorded_as_an_error(
    client, env, api_app, settings, monkeypatch
):
    run = (await start_run(client, env, "tiny", sample_size=1)).json()
    job = (await sql(api_app, "SELECT payload FROM outbox WHERE type = 'replay.execute'"))[
        0
    ].payload
    executor = ReplayExecutor(api_app.state.resources, settings, api_app.state.keyring)

    async def overloaded(*a, **kw):
        return replay.Outcome("temporary", 503, "http_503")

    monkeypatch.setattr(replay, "call_candidate", overloaded)
    for attempt in (0, 1):  # the retries
        with pytest.raises(TemporaryError):
            await executor.handle(Message("replay.execute", job, headers={"x-retry": attempt}))
    assert await results(api_app, run["id"]) == []

    # the last attempt: the failure is stored, so the run can finish instead of waiting forever
    await executor.handle(Message("replay.execute", job, headers={"x-retry": 2}))
    (row,) = await results(api_app, run["id"])
    assert (row.status, row.error_code, row.attempts) == ("error", "http_503", 3)
    assert (await get_run(client, env, run["id"]))["status"] == "completed"


async def test_a_message_with_a_broken_payload_goes_to_the_dead_letters(api_app, settings):
    executor = ReplayExecutor(api_app.state.resources, settings, api_app.state.keyring)
    with pytest.raises(PermanentError):
        await executor.handle(Message("replay.execute", {"run_id": "x"}))


# ---- steering ---------------------------------------------------------------------------------
async def test_a_paused_run_does_nothing_until_it_is_resumed(client, env, api_app, pipeline):
    run = (await start_run(client, env)).json()
    paused = await client.post(
        f"/projects/{env.project_id}/calibration-runs/{run['id']}/pause", headers=env.admin
    )
    assert paused.json()["status"] == "paused"

    await pipeline.start()
    await asyncio.sleep(1.5)  # jobs keep coming back and keep being put off
    assert await results(api_app, run["id"]) == []
    assert (await get_run(client, env, run["id"]))["status"] == "paused"

    resumed = await client.post(
        f"/projects/{env.project_id}/calibration-runs/{run['id']}/resume", headers=env.admin
    )
    assert resumed.json()["status"] == "running"
    done = await wait_status(client, env, run["id"], "completed")
    assert done["progress"]["by_status"] == {"ok": 12}


async def test_a_cancelled_run_stops_and_is_announced(client, env, api_app, pipeline):
    run = (await start_run(client, env)).json()
    base = f"/projects/{env.project_id}/calibration-runs/{run['id']}"
    cancelled = await client.post(f"{base}/cancel", headers=env.admin)
    assert cancelled.json()["status"] == "cancelled" and cancelled.json()["finished_at"]

    await pipeline.start()
    await asyncio.sleep(1.0)
    assert await results(api_app, run["id"]) == []  # the queued jobs were dropped
    again = await client.post(f"{base}/cancel", headers=env.admin)
    assert again.status_code == 409
    assert (await client.post(f"{base}/pause", headers=env.admin)).status_code == 409
    events = await sql(
        api_app, "SELECT payload FROM outbox WHERE type = 'calibration.run.finished'"
    )
    assert [e.payload["status"] for e in events] == ["cancelled"]


async def test_resuming_completes_a_run_whose_jobs_all_finished_while_it_was_paused(
    client, env, api_app, settings, monkeypatch
):
    """Nobody else would complete such a run: resuming has to."""
    run = (await start_run(client, env, "tiny", sample_size=1)).json()
    job = (await sql(api_app, "SELECT payload FROM outbox WHERE type = 'replay.execute'"))[
        0
    ].payload
    executor = ReplayExecutor(api_app.state.resources, settings, api_app.state.keyring)
    base = f"/projects/{env.project_id}/calibration-runs/{run['id']}"
    real = replay.call_candidate

    async def paused_meanwhile(*args, **kwargs):
        await client.post(f"{base}/pause", headers=env.admin)  # the pause arrives mid-call
        return await real(*args, **kwargs)

    monkeypatch.setattr(replay, "call_candidate", paused_meanwhile)
    await executor.handle(Message("replay.execute", job))

    assert len(await results(api_app, run["id"])) == 1  # the running job still stored its result
    assert (await get_run(client, env, run["id"]))["status"] == "paused"  # but cannot complete it
    resumed = await client.post(f"{base}/resume", headers=env.admin)
    assert resumed.json()["status"] == "completed"


# ---- validation and access --------------------------------------------------------------------
async def test_invalid_runs_are_refused_with_a_reason(client, admin, env, api_app):
    ref = env.reference_id
    unpriced = await add_model(client, admin, env.stack.provider["id"], "mock-judge")
    cases = {
        "unknown task type": {**run_body(env), "task_type": "nonexistent"},
        "reference as candidate": {**run_body(env), "candidate_model_ids": [ref]},
        "unknown model": {**run_body(env), "candidate_model_ids": [str(uuid.uuid4())]},
        "model without price": {**run_body(env), "candidate_model_ids": [unpriced]},
        "sample too large": {**run_body(env), "sample_size": 10_000},
        "no candidates": {**run_body(env), "candidate_model_ids": []},
        "bad window": {
            **run_body(env),
            "since": "2030-01-02T00:00:00Z",
            "until": "2030-01-01T00:00:00Z",
        },
        "labelled source without examples": {**run_body(env), "source": "labeled"},
    }
    for name, body in cases.items():
        r = await client.post(
            f"/projects/{env.project_id}/calibration-runs", json=body, headers=admin
        )
        assert r.status_code == 422, (name, r.text)
    assert (await sql(api_app, "SELECT count(*) AS n FROM calibration_runs"))[0].n == 0
    assert (await sql(api_app, "SELECT count(*) AS n FROM outbox WHERE type = 'replay.execute'"))[
        0
    ].n == 0


async def test_a_project_without_a_reference_model_cannot_be_calibrated(client, admin, env):
    other = (await client.post("/projects", json={"name": "bare"}, headers=admin)).json()
    r = await client.post(
        f"/projects/{other['id']}/calibration-runs", json=run_body(env), headers=admin
    )
    assert r.status_code == 422 and "reference" in r.text


async def test_members_only_and_viewers_cannot_steer(client, admin, env):
    run = (await start_run(client, env)).json()
    base = f"/projects/{env.project_id}/calibration-runs"

    await make_admin(client, admin, "stranger@example.com", "admin")
    stranger = bearer((await login(client, "stranger@example.com", GOOD_PASSWORD)).json())
    assert (await client.get(f"{base}/{run['id']}", headers=stranger)).status_code == 404
    assert (await client.get(base, headers=stranger)).status_code == 404
    assert (await client.post(f"{base}/{run['id']}/cancel", headers=stranger)).status_code == 404

    await make_admin(client, admin, "viewer@example.com", "viewer")
    await client.post(
        f"/projects/{env.project_id}/members", json={"email": "viewer@example.com"}, headers=admin
    )
    viewer = bearer((await login(client, "viewer@example.com", GOOD_PASSWORD)).json())
    assert (await client.get(f"{base}/{run['id']}", headers=viewer)).status_code == 200
    assert (await client.post(f"{base}/{run['id']}/cancel", headers=viewer)).status_code == 403
    assert (await client.post(base, json=run_body(env), headers=viewer)).status_code == 403


async def test_a_run_of_another_project_is_not_reachable_through_this_one(client, admin, env):
    run = (await start_run(client, env)).json()
    other = (await client.post("/projects", json={"name": "other"}, headers=admin)).json()
    r = await client.get(f"/projects/{other['id']}/calibration-runs/{run['id']}", headers=admin)
    assert r.status_code == 404


# ---- sampling through the API -----------------------------------------------------------------
async def test_the_same_seed_picks_the_same_requests_and_stratified_runs_work(client, env, api_app):
    await add_traffic_more(api_app, env)
    a = (await start_run(client, env, "tiny", sample_size=5, seed=42)).json()
    b = (await start_run(client, env, "tiny", sample_size=5, seed=42)).json()
    c = (await start_run(client, env, "tiny", sample_size=5, seed=43)).json()
    s = (await start_run(client, env, "tiny", sample_size=5, strategy="stratified", seed=1)).json()

    async def picked(run):
        rows = await sql(
            api_app,
            "SELECT request_id FROM run_samples WHERE run_id = :r",
            r=uuid.UUID(run["id"]),
        )
        return {str(r.request_id) for r in rows}

    assert await picked(a) == await picked(b)
    assert await picked(a) != await picked(c)
    assert len(await picked(s)) == 5 and s["strategy"] == "stratified"


async def test_requests_logged_without_bodies_are_never_sampled(client, env, api_app):
    """A project that does not store bodies leaves the JSON value `null` in old rows; `IS NOT
    NULL` accepted them and the replay of such a sample crashed (found by the load test)."""
    await sql(api_app, "DELETE FROM requests")
    rows = [
        {
            "task_type": "classification",
            "model_id": uuid.UUID(env.reference_id),
            "request_payload": {"messages": [{"role": "user", "content": f"prompt {i}"}]},
            "response_payload": answer(),
        }
        for i in range(6)
    ]
    await insert_rows(api_app, env.project_id, rows)
    await sql(
        api_app,
        "UPDATE requests SET request_payload = 'null'::jsonb, response_payload = 'null'::jsonb"
        " WHERE request_id IN (SELECT request_id FROM requests ORDER BY request_id LIMIT 4)",
    )
    run = (await start_run(client, env, "tiny", sample_size=10)).json()
    assert run["samples_taken"] == 2  # only the two rows that really have bodies


async def test_duplicate_prompts_are_replayed_once(client, env, api_app):
    same = {"messages": [{"role": "user", "content": "the very same prompt"}]}
    await sql(api_app, "DELETE FROM requests")
    await insert_rows(
        api_app,
        env.project_id,
        [
            {
                "task_type": "classification",
                "model_id": uuid.UUID(env.reference_id),
                "request_payload": same,
                "response_payload": answer(),
            }
            for _ in range(5)
        ],
    )
    run = (await start_run(client, env, "tiny", sample_size=5)).json()
    assert run["samples_taken"] == 1


async def test_only_good_recorded_requests_of_the_reference_are_candidates_for_the_sample(
    client, env, api_app
):
    await sql(api_app, "DELETE FROM requests")
    base = {
        "task_type": "classification",
        "model_id": uuid.UUID(env.reference_id),
        "request_payload": {"messages": [{"role": "user", "content": "x"}]},
        "response_payload": answer(),
    }
    await insert_rows(
        api_app,
        env.project_id,
        [
            {**base, "request_payload": {"messages": [{"role": "user", "content": "good"}]}},
            {**base, "status": "error"},  # the reference failed
            {
                **base,
                "payload_skipped": True,
                "request_payload": {"messages": [{"role": "user", "content": "big"}]},
            },
            {
                **base,
                "model_id": uuid.uuid4(),
                "request_payload": {"messages": [{"role": "user", "content": "other model"}]},
            },
            {
                **base,
                "task_type": "extraction",
                "request_payload": {"messages": [{"role": "user", "content": "other task"}]},
            },
        ],
    )
    run = (await start_run(client, env, "tiny", sample_size=10)).json()
    assert run["samples_taken"] == 1


async def add_traffic_more(api_app, env: Env) -> None:
    await add_traffic(api_app, env, 30)


# ---- labelled examples ------------------------------------------------------------------------
def jsonl(*lines) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode()


async def import_samples(client, env, body: bytes, name: str = "classification"):
    return await client.post(
        f"/projects/{env.project_id}/task-types/{name}/labeled-samples",
        content=body,
        headers={**env.admin, "content-type": "application/x-ndjson"},
    )


def labelled(i: int, expected="positive") -> dict:
    return {"messages": [{"role": "user", "content": f"labelled review {i}"}], "expected": expected}


async def test_labelled_examples_are_imported_and_used_in_a_run(client, env, api_app, pipeline):
    r = await import_samples(client, env, jsonl(labelled(1), labelled(2), labelled(3), labelled(1)))
    assert r.status_code == 200, r.text
    assert r.json() == {"total": 4, "imported": 3, "duplicates": 1, "available": 3}
    again = await import_samples(client, env, jsonl(labelled(1), labelled(4)))
    assert again.json()["imported"] == 1 and again.json()["available"] == 4
    count = await client.get(
        f"/projects/{env.project_id}/task-types/classification/labeled-samples", headers=env.admin
    )
    assert count.json() == {"count": 4}

    run = (await start_run(client, env, "tiny", source="labeled", sample_size=10)).json()
    assert run["samples_taken"] == 4
    await pipeline.start()
    done = await wait_status(client, env, run["id"], "completed")
    assert done["progress"]["by_status"] == {"ok": 4}
    stored = await sql(
        api_app,
        "SELECT source, expected, reference_response FROM run_samples WHERE run_id = :r",
        r=uuid.UUID(run["id"]),
    )
    assert {s.source for s in stored} == {"labeled"}
    assert all(
        s.expected == {"content": "positive"} and s.reference_response is None for s in stored
    )


async def test_a_bad_import_changes_nothing_and_names_the_lines(client, env, api_app):
    body = jsonl(labelled(1)) + b"not json\n" + jsonl({"messages": [], "expected": "x"})
    r = await import_samples(client, env, body)
    assert r.status_code == 422
    assert "line 2" in r.text and "line 3" in r.text
    assert (await sql(api_app, "SELECT count(*) AS n FROM labeled_samples"))[0].n == 0
    assert (await import_samples(client, env, b"")).status_code == 422
    assert (await import_samples(client, env, jsonl(labelled(1)), "missing")).status_code == 404


async def test_the_import_is_limited_in_size_and_to_administrators(client, admin, env, api_app):
    big = jsonl(*[labelled(i) for i in range(5001)])
    assert (await import_samples(client, env, big)).status_code == 413

    await make_admin(client, admin, "viewer2@example.com", "viewer")
    viewer = bearer((await login(client, "viewer2@example.com", GOOD_PASSWORD)).json())
    r = await client.post(
        f"/projects/{env.project_id}/task-types/classification/labeled-samples",
        content=jsonl(labelled(1)),
        headers=viewer,
    )
    assert r.status_code in (403, 404)


# ---- reading the results ----------------------------------------------------------------------
async def test_results_can_be_listed_filtered_and_paged(client, env, api_app, pipeline):
    run = (await start_run(client, env)).json()
    await pipeline.start()
    await wait_status(client, env, run["id"], "completed")
    base = f"/projects/{env.project_id}/calibration-runs/{run['id']}/results"

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 5, **({"cursor": cursor} if cursor else {})}
        page = (await client.get(base, params=params, headers=env.admin)).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 12 and len(set(seen)) == 12

    tiny = (
        await client.get(base, params={"model_id": env.candidates["tiny"]}, headers=env.admin)
    ).json()
    assert len(tiny["items"]) == 6 and tiny["items"][0]["response_payload"]["choices"]
    none = (await client.get(base, params={"status": "error"}, headers=env.admin)).json()
    assert none["items"] == []
    assert (await client.get(base, params={"cursor": "zzz"}, headers=env.admin)).status_code == 422


async def test_provider_limits_can_be_set_and_are_validated(client, admin, env):
    provider = env.stack.provider["id"]
    ok = await client.patch(
        f"/providers/{provider}",
        json={"calibration_max_concurrency": 3, "calibration_max_rps": "2.5"},
        headers=admin,
    )
    assert ok.status_code == 200
    assert ok.json()["calibration_max_concurrency"] == 3
    assert Decimal(str(ok.json()["calibration_max_rps"])) == Decimal("2.5")
    for bad in ({"calibration_max_concurrency": 0}, {"calibration_max_rps": "-1"}):
        assert (
            await client.patch(f"/providers/{provider}", json=bad, headers=admin)
        ).status_code == 422
