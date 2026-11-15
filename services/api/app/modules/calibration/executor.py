"""The worker side of a calibration: one `replay.execute` message = one (request x candidate).

Life of a job
  1. read the run: finished -> drop the job; paused -> Deferred (come back later);
  2. a result for this pair exists already (a redelivered message) -> nothing to do;
  3. the prompt cannot fit the candidate's context -> store `context_exceeded`, no call;
  4. wait for a slot at the provider (shared limits in Redis), reserve budget, call, settle;
  5. store the result and, if it was the last one, complete the run (one transaction).

Failure handling
  * the provider is slow or overloaded (timeout, 429, 5xx) -> TemporaryError: the message is
    retried with a growing delay. After the last retry the failure is STORED as an error
    result, so the run still finishes instead of waiting for a job that went to the DLQ;
  * a rejected request (4xx for the content) is a normal result, not a worker error;
  * the database or Redis is down -> TemporaryError, the message is retried.

The database connection is never held during the provider call, which can take minutes.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.core import ssrf
from app.core.config import Settings
from app.core.crypto import DecryptionError, KeyRing
from app.core.logging import get_logger
from app.core.messaging import Deferred, Message, PermanentError, TemporaryError
from app.core.resources import Resources
from app.modules.calibration import pricing, replay
from app.modules.calibration.lifecycle import complete_if_done, finish_run
from app.modules.calibration.limits import ProviderLimiter, RunBudget
from app.modules.calibration.models import FINISHED_STATUSES
from app.modules.calibration.repository import CalibrationRepository, Candidate

log = get_logger(__name__)

_RESULT_STATUS = {
    "ok": "ok",
    "context_exceeded": "context_exceeded",
    "refused": "refused",
    "error": "error",
    "temporary": "error",  # only reached when the retries are used up
}


@dataclass(frozen=True)
class Job:
    run_id: uuid.UUID
    request_id: uuid.UUID
    model_id: uuid.UUID


def parse_job(payload: Any) -> Job:
    try:
        return Job(
            uuid.UUID(payload["run_id"]),
            uuid.UUID(payload["request_id"]),
            uuid.UUID(payload["model_id"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        # AttributeError: uuid.UUID(<number>) fails that way; a payload is untrusted input.
        raise PermanentError("invalid replay.execute payload") from exc


@dataclass(frozen=True)
class _Plan:
    """Everything a job needs, read from the database in one short transaction."""

    request: dict[str, Any]
    reference_completion_tokens: int | None
    candidate: Candidate
    price: pricing.Price
    max_cost_usd: Decimal | None
    max_requests: int | None
    reference_prompt_tokens: int | None


class ReplayExecutor:
    def __init__(
        self,
        res: Resources,
        settings: Settings,
        keyring: KeyRing,
        *,
        resolver: ssrf.Resolver = ssrf.system_resolver,
    ) -> None:
        self._res = res
        self._settings = settings
        self._keyring = keyring
        self._resolver = resolver
        self._limiter = ProviderLimiter(
            res.redis,
            slot_ttl=settings.calibration_slot_ttl,
            wait=settings.calibration_limit_wait,
        )

    async def handle(self, message: Message) -> None:
        job = parse_job(message.payload)
        try:
            await self._run(job, message.attempt)
        except (SQLAlchemyError, RedisError, OSError) as exc:
            # Infrastructure trouble is not the job's fault: try again later.
            log.warning(
                "replay_infrastructure_error",
                error=type(exc).__name__,
                detail=" ".join(str(exc).split())[:200],
            )
            raise TemporaryError(f"{type(exc).__name__} while replaying") from exc

    async def _run(self, job: Job, attempt: int) -> None:
        plan = await self._load(job)
        if plan is None:
            return  # run finished, deleted, or the job was done before
        cand = plan.candidate

        request = plan.request
        prompt_estimate = plan.reference_prompt_tokens or pricing.estimate_prompt_tokens(request)
        max_tokens = pricing.requested_max_tokens(request)
        if prompt_estimate + (max_tokens or 0) > cand.context_window:
            await self._store(job, replay.Outcome("context_exceeded", None, "context_too_small"),
                              plan, attempt, Decimal(0))  # fmt: skip
            return
        if not cand.enabled or not cand.provider_enabled:
            await self._store(job, replay.Outcome("error", None, "model_disabled"),
                              plan, attempt, Decimal(0))  # fmt: skip
            return
        try:
            api_key = (
                self._keyring.decrypt(cand.api_key_encrypted, aad=str(cand.provider_id))
                if cand.api_key_encrypted
                else None
            )
        except DecryptionError:
            await self._store(job, replay.Outcome("error", None, "credentials_unreadable"),
                              plan, attempt, Decimal(0))  # fmt: skip
            return

        target = replay.Target(
            base_url=cand.base_url,
            is_local=cand.is_local,
            auth_type=cand.auth_type,
            api_key=api_key,
            model_name=cand.name,
            supports_stream=cand.supports_stream,
        )
        concurrency = cand.max_concurrency or self._settings.calibration_default_concurrency
        rps = float(cand.max_rps or self._settings.calibration_default_rps)

        # A slot first: if the provider is busy the job is deferred without touching the budget.
        async with self._limiter.slot(
            str(cand.provider_id), max_concurrency=concurrency, max_rps=rps
        ):
            estimate = pricing.cost_usd(
                plan.price,
                prompt_estimate,
                pricing.estimate_completion_tokens(
                    request,
                    plan.reference_completion_tokens,
                    self._settings.calibration_default_max_tokens,
                ),
            )
            budget = RunBudget(self._res.redis, str(job.run_id))
            estimate_micro = pricing.to_micro(estimate)
            if not await budget.reserve(
                estimate_micro,
                max_micro=None
                if plan.max_cost_usd is None
                else pricing.to_micro(plan.max_cost_usd),
                max_calls=plan.max_requests,
                rebuild=lambda: self._rebuild_budget(job.run_id),
            ):
                await self._stop_for_budget(job.run_id)
                return

            outcome = await replay.call_candidate(
                self._res.http, self._settings, target, request, resolver=self._resolver
            )

            if outcome.kind == "temporary":
                await budget.refund(estimate_micro, keep_call=True)
                if attempt < len(self._settings.retry_delays_ms):
                    raise TemporaryError(outcome.error_code or "provider unavailable")
                # retries are used up: record the failure so the run can finish
                cost = Decimal(0)
            else:
                cost = pricing.cost_usd(
                    plan.price, outcome.prompt_tokens or 0, outcome.completion_tokens or 0
                )
                await budget.settle(estimate_micro, pricing.to_micro(cost))

        await self._store(job, outcome, plan, attempt, cost)

    # ---- database steps (each in its own short transaction) ----
    async def _load(self, job: Job) -> _Plan | None:
        async with self._res.session_factory() as session:
            repo = CalibrationRepository(session)
            run = await repo.get_run(job.run_id)
            if run is None or run.status in FINISHED_STATUSES:
                return None
            if run.status == "paused":
                raise Deferred("run is paused")
            if await repo.result_exists(job.run_id, job.request_id, job.model_id):
                return None
            sample = await repo.get_sample(job.run_id, job.request_id)
            candidates = await repo.candidates([job.model_id])
            cand = candidates.get(job.model_id)
            if sample is None or cand is None:
                raise PermanentError("the sample or the model of the job does not exist")
            price = await repo.current_price(job.model_id, datetime.now(UTC))
            if price is None:
                raise PermanentError("the model has no valid price")
            return _Plan(
                request=sample.request_payload,
                reference_completion_tokens=sample.reference_completion_tokens,
                candidate=cand,
                price=price,
                max_cost_usd=run.max_cost_usd,
                max_requests=run.max_requests,
                reference_prompt_tokens=sample.reference_prompt_tokens,
            )

    async def _rebuild_budget(self, run_id: uuid.UUID) -> tuple[int, int]:
        """Counters in Redis were lost: recompute them from the stored results (the truth)."""
        async with self._res.session_factory() as session:
            spent, calls = await CalibrationRepository(session).spending(run_id)
        return pricing.to_micro(spent), calls

    async def _stop_for_budget(self, run_id: uuid.UUID) -> None:
        async with self._res.session_factory() as session, session.begin():
            repo = CalibrationRepository(session)
            if await finish_run(
                session,
                repo,
                run_id,
                "budget_exceeded",
                reason="the budget of the run is used up",
            ):
                log.info("calibration_budget_exceeded", run_id=str(run_id))

    async def _store(
        self, job: Job, outcome: replay.Outcome, plan: _Plan, attempt: int, cost: Decimal
    ) -> None:
        per_token = plan.price.cost_type == "per_token"
        values: dict[str, Any] = {
            "run_id": job.run_id,
            "request_id": job.request_id,
            "model_id": job.model_id,
            "status": _RESULT_STATUS[outcome.kind],
            "http_status": outcome.http_status,
            "error_code": outcome.error_code,
            "response_payload": outcome.response,
            "finish_reason": outcome.finish_reason,
            "has_tool_calls": outcome.has_tool_calls,
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
            "tokens_estimated": outcome.tokens_estimated,
            "ttft_ms": outcome.ttft_ms,
            "latency_ms": outcome.latency_ms,
            "price_input_per_1m": plan.price.input_per_1m if per_token else None,
            "price_output_per_1m": plan.price.output_per_1m if per_token else None,
            "cost_usd": cost if per_token and outcome.kind in ("ok", "refused") else None,
            "attempts": min(attempt + 1, 32000),
        }
        async with self._res.session_factory() as session, session.begin():
            repo = CalibrationRepository(session)
            inserted = await repo.insert_result(values)
            if inserted:
                await complete_if_done(session, repo, job.run_id)
