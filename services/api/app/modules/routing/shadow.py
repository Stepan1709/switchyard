"""Shadow mode, the worker side: a copy of a live request goes to a candidate and its answer is
kept next to the real one. Nobody is waiting for it and nothing it says reaches a client.

The copy is the request as it was stored in the traffic log (so a project that does not keep
bodies, or whose prompt was too big to keep, is not copied at all), sent exactly as the
calibration replay sends it: tools are not executed, a prompt that does not fit the model's
context is recorded and not sent, the provider's limits (shared with calibration) are honoured.

The answers are only stored here. Judging them (checks, the judge) comes with the analytics.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from app.core import ssrf
from app.core.config import Settings
from app.core.crypto import DecryptionError, KeyRing
from app.core.logging import get_logger
from app.core.messaging import Message, PermanentError, TemporaryError
from app.core.resources import Resources
from app.modules.calibration import pricing, replay
from app.modules.calibration.limits import ProviderLimiter
from app.modules.calibration.repository import CalibrationRepository
from app.modules.routing.models import ShadowResult

log = get_logger(__name__)

_STATUS = {"ok": "ok", "context_exceeded": "context_exceeded", "refused": "refused",
           "error": "error", "temporary": "error"}  # fmt: skip


@dataclass(frozen=True)
class ShadowJob:
    request_id: uuid.UUID
    started_at: datetime
    project_id: uuid.UUID
    task_type: str | None
    model_id: uuid.UUID


def parse_job(payload: Any) -> ShadowJob:
    try:
        task = payload.get("task_type")
        return ShadowJob(
            uuid.UUID(payload["request_id"]),
            datetime.fromisoformat(payload["started_at"]),
            uuid.UUID(payload["project_id"]),
            task if isinstance(task, str) else None,
            uuid.UUID(payload["model_id"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PermanentError("invalid shadow.execute payload") from exc


class ShadowExecutor:
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
            res.redis, slot_ttl=settings.calibration_slot_ttl, wait=settings.calibration_limit_wait
        )

    async def handle(self, message: Message) -> None:
        job = parse_job(message.payload)
        try:
            await self._run(job, message.attempt)
        except (SQLAlchemyError, RedisError, OSError) as exc:
            log.warning(
                "shadow_infrastructure_error",
                error=type(exc).__name__,
                detail=" ".join(str(exc).split())[:200],
            )
            raise TemporaryError(f"{type(exc).__name__} while copying a request") from exc

    async def _run(self, job: ShadowJob, attempt: int) -> None:
        async with self._res.session_factory() as session:
            done = await session.execute(
                select(ShadowResult.id).where(
                    ShadowResult.request_id == job.request_id, ShadowResult.model_id == job.model_id
                )
            )
            if done.first() is not None:
                return  # a redelivery: the copy was made already
            row = (
                await session.execute(
                    text(
                        "SELECT request_payload, prompt_tokens FROM requests"
                        " WHERE started_at = :t AND request_id = :r AND project_id = :p"
                    ),
                    {"t": job.started_at, "r": job.request_id, "p": job.project_id},
                )
            ).first()
            repo = CalibrationRepository(session)
            cand = (await repo.candidates([job.model_id])).get(job.model_id)
            price = await repo.current_price(job.model_id, datetime.now(UTC))
        if row is None or row.request_payload is None:
            return  # the request is gone (retention) or never kept its body
        if cand is None or price is None:
            return  # the model was deleted since: nothing to copy to
        request: dict[str, Any] = row.request_payload

        prompt = row.prompt_tokens or pricing.estimate_prompt_tokens(request)
        if prompt + (pricing.requested_max_tokens(request) or 0) > cand.context_window:
            await self._store(job, replay.Outcome("context_exceeded", None, "context_too_small"),
                              price, Decimal(0))  # fmt: skip
            return
        if not (cand.enabled and cand.provider_enabled):
            return
        try:
            key = (
                self._keyring.decrypt(cand.api_key_encrypted, aad=str(cand.provider_id))
                if cand.api_key_encrypted
                else None
            )
        except DecryptionError:
            await self._store(job, replay.Outcome("error", None, "credentials_unreadable"),
                              price, Decimal(0))  # fmt: skip
            return
        target = replay.Target(
            cand.base_url, cand.is_local, cand.auth_type, key, cand.name, cand.supports_stream
        )
        async with self._limiter.slot(
            str(cand.provider_id),
            max_concurrency=cand.max_concurrency or self._settings.calibration_default_concurrency,
            max_rps=float(cand.max_rps or self._settings.calibration_default_rps),
        ):
            outcome = await replay.call_candidate(
                self._res.http, self._settings, target, request, resolver=self._resolver
            )
        if outcome.kind == "temporary":
            if attempt < len(self._settings.retry_delays_ms):
                raise TemporaryError(outcome.error_code or "provider unavailable")
            cost = Decimal(0)
        else:
            cost = pricing.cost_usd(
                price, outcome.prompt_tokens or 0, outcome.completion_tokens or 0
            )
        await self._store(job, outcome, price, cost)

    async def _store(
        self, job: ShadowJob, outcome: replay.Outcome, price: pricing.Price, cost: Decimal
    ) -> None:
        per_token = price.cost_type == "per_token"
        values: dict[str, Any] = {
            "id": uuid.uuid4(),
            "request_id": job.request_id,
            "request_started_at": job.started_at,
            "project_id": job.project_id,
            "task_type": job.task_type,
            "model_id": job.model_id,
            "status": _STATUS[outcome.kind],
            "http_status": outcome.http_status,
            "error_code": outcome.error_code,
            "response_payload": outcome.response,
            "finish_reason": outcome.finish_reason,
            "has_tool_calls": outcome.has_tool_calls,
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
            "ttft_ms": outcome.ttft_ms,
            "latency_ms": outcome.latency_ms,
            "price_input_per_1m": price.input_per_1m if per_token else None,
            "price_output_per_1m": price.output_per_1m if per_token else None,
            "cost_usd": cost if per_token and outcome.kind in ("ok", "refused") else None,
        }
        async with self._res.session_factory() as session, session.begin():
            await session.execute(
                pg_insert(ShadowResult)
                .values(**values)
                .on_conflict_do_nothing(constraint="uq_shadow_results_pair")
            )
