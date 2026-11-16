"""Shared setup for calibration tests: a provisioned project with candidates and traffic, and an
in-process pipeline (outbox relay + the replay and end-of-run consumers)."""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import text

from app.core.messaging import Consumer
from app.core.messaging.topology import QueueNames
from app.core.outbox import OutboxRelay
from app.modules.calibration.executor import ReplayExecutor
from app.modules.evaluation.evaluator import EvaluationStarter, Evaluator
from app.modules.routing.shadow import ShadowExecutor
from app.worker.consumers import judge_spec, replay_spec, run_finished_spec, shadow_spec
from tests.integration.conftest import MOCK_KEY, wait_until
from tests.integration.gateway_support import Provisioned, provision
from tests.integration.test_traffic_api import insert_rows


@dataclass
class Env:
    admin: dict[str, str]
    project_id: str
    reference_id: str
    stack: Provisioned
    candidates: dict[str, str] = field(default_factory=dict)  # "tiny" / "mid" -> model id
    prompts: list[str] = field(default_factory=list)


def review(i: int) -> str:
    return f"Classify the sentiment of review number {i}: the product is great"


def answer(text_: str = "positive") -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text_}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 2},
    }


async def add_model(client, admin, provider_id: str, name: str, **kw: Any) -> str:
    body = {"provider_id": provider_id, "name": name, "context_window": 8192, **kw}
    r = await client.post("/models", json=body, headers=admin)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def set_price(client, admin, model_id: str, input_: str = "0.5", output: str = "1.5") -> None:
    r = await client.post(
        f"/models/{model_id}/prices",
        json={"input_per_1m": input_, "output_per_1m": output},
        headers=admin,
    )
    assert r.status_code == 201, r.text


async def build_env(
    client,
    admin,
    api_app,
    mock_base_url: str,
    *,
    requests: int = 6,
    task_type: str = "classification",
) -> Env:
    stack = await provision(client, admin, mock_base_url)
    assert stack.model is not None
    provider_id = stack.provider["id"]
    env = Env(admin, stack.project["id"], stack.model["id"], stack)
    env.candidates["tiny"] = await add_model(client, admin, provider_id, "mock-tiny")
    env.candidates["mid"] = await add_model(client, admin, provider_id, "mock-mid")
    await set_price(client, admin, env.candidates["tiny"], "0.1", "0.2")
    await set_price(client, admin, env.candidates["mid"], "0.5", "1.0")
    r = await client.post(
        f"/projects/{env.project_id}/task-types", json={"name": task_type}, headers=admin
    )
    assert r.status_code == 201, r.text
    await add_traffic(api_app, env, requests, task_type=task_type)
    return env


async def add_traffic(api_app, env: Env, n: int, *, task_type: str = "classification") -> None:
    start = len(env.prompts)
    rows = []
    for i in range(start, start + n):
        prompt = review(i)
        env.prompts.append(prompt)
        rows.append(
            {
                "task_type": task_type,
                "model_id": uuid.UUID(env.reference_id),
                "request_payload": {
                    "model": "mock-strong",
                    "messages": [{"role": "user", "content": prompt}],
                },
                "response_payload": answer(),
                "prompt_tokens": 12,
                "completion_tokens": 2,
            }
        )
    await insert_rows(api_app, env.project_id, rows)


def run_body(env: Env, *candidates: str, **extra: Any) -> dict[str, Any]:
    return {
        "task_type": "classification",
        "candidate_model_ids": [env.candidates[c] for c in candidates or ("tiny", "mid")],
        "sample_size": 6,
        **extra,
    }


async def start_run(client, env: Env, *candidates: str, key: str | None = None, **extra: Any):
    headers = {**env.admin, **({"Idempotency-Key": key} if key else {})}
    return await client.post(
        f"/projects/{env.project_id}/calibration-runs",
        json=run_body(env, *candidates, **extra),
        headers=headers,
    )


async def get_run(client, env: Env, run_id: str) -> dict[str, Any]:
    r = await client.get(f"/projects/{env.project_id}/calibration-runs/{run_id}", headers=env.admin)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def wait_status(client, env: Env, run_id: str, *statuses: str, timeout: float = 30):  # noqa: ASYNC109
    async def reached():
        return (await get_run(client, env, run_id))["status"] in statuses

    await wait_until(reached, timeout=timeout)
    return await get_run(client, env, run_id)


async def sql(api_app, query: str, **params: Any) -> list[Any]:
    """Run a statement in its own committed transaction; SELECTs return their rows."""
    async with api_app.state.resources.session_factory() as s, s.begin():
        result = await s.execute(text(query), params)
        return list(result.all()) if result.returns_rows else []


async def results(api_app, run_id: str) -> list[Any]:
    return await sql(
        api_app,
        "SELECT request_id, model_id, status, error_code, cost_usd, prompt_tokens, completion_tokens,"
        " ttft_ms, latency_ms, has_tool_calls, finish_reason, price_input_per_1m,"
        " price_output_per_1m, attempts FROM run_results WHERE run_id = :r",
        r=uuid.UUID(run_id),
    )


class Pipeline:
    """The worker's calibration part running inside the test, on private queues."""

    def __init__(self, api_app, settings, amqp, publisher) -> None:
        self.api_app, self.settings, self.amqp, self.publisher = api_app, settings, amqp, publisher
        self.consumers: list[Consumer] = []
        self.relay: OutboxRelay | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> "Pipeline":
        res = self.api_app.state.resources
        executor = ReplayExecutor(res, self.settings, self.api_app.state.keyring)
        specs = (
            replay_spec(executor, self.settings),
            run_finished_spec(EvaluationStarter(res, self.settings), self.settings),
            judge_spec(Evaluator(res, self.settings, self.api_app.state.keyring), self.settings),
            shadow_spec(
                ShadowExecutor(res, self.settings, self.api_app.state.keyring), self.settings
            ),
        )
        for spec in specs:
            consumer = Consumer(
                spec, self.amqp, self.publisher, retry_delays_ms=self.settings.retry_delays_ms
            )
            await consumer.start()
            self.consumers.append(consumer)
        self.relay = OutboxRelay(res.session_factory, self.publisher, poll_interval=0.05)
        self._task = asyncio.create_task(self.relay.run())
        return self

    async def stop(self) -> None:
        if self.relay is not None and self._task is not None:
            self.relay.stop()
            await self._task
        for consumer in self.consumers:
            await consumer.stop(5)
        await delete_queues(self.amqp, self.settings)


async def delete_queues(amqp, settings) -> None:
    channel = await amqp.channel()
    for name in (
        settings.replay_queue,
        settings.run_finished_queue,
        settings.judge_queue,
        settings.shadow_queue,
    ):
        names = QueueNames(name)
        for q in [names.work, names.dlq, *(names.retry(i) for i in (1, 2, 3))]:
            await channel.queue_delete(q)


def jobs_of(payloads: list[Any]) -> list[dict[str, Any]]:
    return [p if isinstance(p, dict) else json.loads(p) for p in payloads]


async def add_traffic_from_mock(
    api_app, env: Env, mock_base_url: str, n: int, *, task_type: str = "classification",
    extra: dict[str, Any] | None = None, prefix: str = "Review",
) -> None:  # fmt: skip
    """Record traffic the way the gateway would: the answers are really given by mock-strong."""
    rows = []
    async with httpx.AsyncClient() as http:
        for i in range(len(env.prompts), len(env.prompts) + n):
            prompt = f"{prefix} number {i}: classify the sentiment of this text about product {i}"
            env.prompts.append(prompt)
            body = {
                "model": "mock-strong",
                "messages": [{"role": "user", "content": prompt}],
                **(extra or {}),
            }
            r = await http.post(
                f"{mock_base_url}/chat/completions",
                json=body,
                headers={"authorization": f"Bearer {MOCK_KEY}"},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            rows.append(
                {
                    "task_type": task_type,
                    "model_id": uuid.UUID(env.reference_id),
                    "request_payload": body,
                    "response_payload": data,
                    "prompt_tokens": data["usage"]["prompt_tokens"],
                    "completion_tokens": data["usage"]["completion_tokens"],
                }
            )
    await insert_rows(api_app, env.project_id, rows)


async def add_judge(client, admin, env: Env) -> str:
    judge = await add_model(
        client, admin, env.stack.provider["id"], "mock-judge", context_window=32_768
    )
    await set_price(client, admin, judge, "1.0", "2.0")
    env.candidates["judge"] = judge
    return judge


async def add_check(client, env: Env, kind: str, config: dict | None = None, **extra: Any) -> dict:
    r = await client.post(
        f"/projects/{env.project_id}/task-types/classification/checks",
        json={"kind": kind, "config": config or {}, **extra},
        headers=env.admin,
    )
    assert r.status_code == 201, r.text
    body: dict = r.json()
    return body


async def wait_evaluated(client, env: Env, run_id: str, timeout: float = 60):  # noqa: ASYNC109
    async def done():
        return (await get_run(client, env, run_id))["evaluation"]["status"] == "done"

    await wait_until(done, timeout=timeout)
    return await get_run(client, env, run_id)


async def aggregates(client, env: Env, run_id: str) -> dict[str, Any]:
    r = await client.get(
        f"/projects/{env.project_id}/calibration-runs/{run_id}/aggregates", headers=env.admin
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def evaluations(api_app, run_id: str) -> list[Any]:
    return await sql(
        api_app,
        "SELECT e.*, r.model_id, r.request_id FROM evaluations e JOIN run_results r"
        " ON r.id = e.result_id WHERE e.run_id = :r",
        r=uuid.UUID(run_id),
    )
