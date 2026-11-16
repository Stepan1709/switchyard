"""The worker side of quality evaluation.

EvaluationStarter  consumes `calibration.run.finished`: for every successful replay result of
                   the run it writes a `judge.execute` job (through the outbox, in the same
                   transaction that marks the run `running` for evaluation).
Evaluator          consumes `judge.execute`: one result in, one verdict out.

The order of steps for one answer (cheap and certain first):
  1. deterministic checks (schema, regex, exact match, tool call);
  2. if the answer equals the labelled / reference answer, the judge part is full marks and
     the judge is NOT called;
  3. otherwise, if the run has a judge, the judge grades the answer (or compares it with
     the truth in both orders);
  4. the verdict is stored; the last verdict of a run computes the aggregates.

A judge that cannot give a verdict makes the evaluation `judge_failed`: the answer is then
neither good nor bad, and it is left out of the statistics (and counted separately).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ssrf
from app.core.config import Settings
from app.core.crypto import DecryptionError, KeyRing
from app.core.events import JUDGE_EXECUTE
from app.core.logging import get_logger
from app.core.messaging import Message, PermanentError, TemporaryError
from app.core.outbox import enqueue
from app.core.resources import Resources
from app.modules.calibration import pricing, replay
from app.modules.calibration.limits import ProviderLimiter
from app.modules.calibration.repository import CalibrationRepository
from app.modules.evaluation import checks as chk
from app.modules.evaluation import judge as jd
from app.modules.evaluation import scoring, stats
from app.modules.evaluation.models import TaskCheck
from app.modules.evaluation.repository import EvaluationRepository, JobData

log = get_logger(__name__)


def parse_run_id(payload: Any) -> uuid.UUID:
    try:
        return uuid.UUID(payload["run_id"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PermanentError("invalid run event payload") from exc


def parse_result_id(payload: Any) -> uuid.UUID:
    try:
        return uuid.UUID(payload["result_id"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PermanentError("invalid judge.execute payload") from exc


async def compute_aggregates(
    session: AsyncSession, evals: EvaluationRepository, run_id: uuid.UUID, task_type: str,
    min_samples: int,
) -> None:  # fmt: skip
    rows = await evals.result_rows(run_id)
    aggregates = {model_id: stats.aggregate(items, min_samples) for model_id, items in rows.items()}
    await evals.replace_aggregates(run_id, task_type, aggregates)


async def finish_evaluation_if_done(
    session: AsyncSession, run_id: uuid.UUID, min_samples: int
) -> bool:
    """When every answer of the run has a verdict, compute the aggregates and mark the run
    evaluated. The run row is locked first for the same reason as when a run is completed:
    two workers that store the last two verdicts together must not both think "one is
    missing"."""
    runs = CalibrationRepository(session)
    evals = EvaluationRepository(session)
    run = await runs.get_run(run_id, lock=True)
    if run is None or run.evaluation_status != "running":
        return False
    if await evals.count_evaluations(run_id) < run.evaluation_total:
        return False
    await compute_aggregates(session, evals, run_id, run.task_type, min_samples)
    run.evaluation_status = "done"
    return True


class EvaluationStarter:
    """Turns the end of a run into evaluation jobs."""

    def __init__(self, res: Resources, settings: Settings) -> None:
        self._res = res
        self._settings = settings

    async def handle(self, message: Message) -> None:
        run_id = parse_run_id(message.payload)
        try:
            async with self._res.session_factory() as session, session.begin():
                await self._start(session, run_id)
        except SQLAlchemyError as exc:
            raise TemporaryError(f"{type(exc).__name__} while starting the evaluation") from exc

    async def _start(self, session: AsyncSession, run_id: uuid.UUID) -> None:
        runs = CalibrationRepository(session)
        evals = EvaluationRepository(session)
        run = await runs.get_run(run_id, lock=True)
        # A duplicate of the event (the outbox sends at least once) finds the run already taken.
        if run is None or run.evaluation_status != "pending":
            return
        result_ids = await evals.ok_result_ids(run_id)
        run.evaluation_status = "running"
        run.evaluation_total = len(result_ids)
        for result_id in result_ids:
            enqueue(
                session,
                JUDGE_EXECUTE,
                {"run_id": str(run_id), "result_id": str(result_id)},
                correlation_id=str(run_id),
            )
        if not result_ids:  # nothing to judge: the numbers are only about errors and context
            await compute_aggregates(
                session, evals, run_id, run.task_type, self._settings.calibration_min_samples
            )
            run.evaluation_status = "done"
        log.info("evaluation_started", run_id=str(run_id), answers=len(result_ids))


@dataclass
class _JudgeOutcome:
    part: scoring.JudgePart | None = None
    mode: str | None = None
    version: int = 0
    score: int | None = None
    verdict: str | None = None
    rationale: str | None = None
    error: str | None = None
    usage: jd.Usage | None = None
    cost: Decimal | None = None
    failed: bool = False


class Evaluator:
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
        result_id = parse_result_id(message.payload)
        try:
            await self._run(result_id, message.attempt)
        except (SQLAlchemyError, RedisError, OSError) as exc:
            log.warning(
                "evaluation_infrastructure_error",
                error=type(exc).__name__,
                detail=" ".join(str(exc).split())[:200],
            )
            raise TemporaryError(f"{type(exc).__name__} while evaluating") from exc

    async def _run(self, result_id: uuid.UUID, attempt: int) -> None:
        async with self._res.session_factory() as session:
            repo = EvaluationRepository(session)
            data = await repo.load_job(result_id)
            if data is None or await repo.evaluation_exists(result_id):
                return  # the result is gone, or this job was already done (a redelivery)

        answer = chk.answer_from_response(data.response)
        truth, basis = self._truth(data)
        configured = data.checks
        deterministic = [c for c in configured if c.kind != "llm_judge"]
        tools = data.request.get("tools")
        outcomes = [
            chk.run_check(c.kind, c.config, answer, truth, required=c.required, tools=tools)
            for c in deterministic
        ]
        # Whether the tool calls fit the tools of the request is always recorded (it needs no
        # truth); it only counts towards the verdict when a tool_args_valid check is configured.
        args_outcome = next((o for o in outcomes if o.kind == "tool_args_valid"), None)
        if args_outcome is None:
            args_outcome = chk.run_check("tool_args_valid", {}, answer, truth, tools=tools)
        valid_args = args_outcome.passed
        ignore = self._ignored_args(deterministic)
        equal = truth is not None and chk.answers_equal(answer, truth, ignore_args=ignore)

        if not configured and data.judge_model_id is None and truth is not None:
            # Nothing was configured and there is no judge: the only honest statement is
            # "same as the truth or not".
            outcomes = [
                chk.CheckOutcome(
                    "same_as_truth", equal, True, "" if equal else "differs from the truth"
                )
            ]
        judge_check = next((c for c in configured if c.kind == "llm_judge"), None)
        # A run that names a judge uses it. An llm_judge check only tunes it (threshold, rubric,
        # whether it is required); without one the defaults apply.
        judge_wanted = data.judge_model_id is not None
        outcome = _JudgeOutcome()
        if judge_wanted and truth is not None:
            required = judge_check.required if judge_check else True
            if equal:
                outcome.part = scoring.equal_part(required=required)  # no judge call needed
            else:
                outcome = await self._ask_judge(data, answer, truth, basis, judge_check, attempt)

        await self._store(data, outcomes, outcome, equal, basis, valid_args)

    @staticmethod
    def _truth(data: JobData) -> tuple[chk.Answer | None, str]:
        """The labelled answer outranks the reference model's recorded answer."""
        if data.expected:
            return chk.answer_from_expected(data.expected), "expected"
        if data.reference_response:
            reference = chk.answer_from_response(data.reference_response)
            if not reference.empty:
                return reference, "reference"
        return None, "none"

    @staticmethod
    def _ignored_args(checks: list[TaskCheck]) -> frozenset[str]:
        names: set[str] = set()
        for c in checks:
            if c.kind == "tool_call_match":
                names.update(c.config.get("ignore_args", []))
        return frozenset(names)

    # ---- the judge ----
    async def _ask_judge(
        self,
        data: JobData,
        answer: chk.Answer,
        truth: chk.Answer,
        basis: str,
        judge_check: TaskCheck | None,
        attempt: int,
    ) -> _JudgeOutcome:
        assert data.judge_model_id is not None
        config = judge_check.config if judge_check else {}
        required = judge_check.required if judge_check else True
        min_score = int(config.get("min_score", 4))
        kind = "pairwise" if data.judge_pairwise else "grade"

        async with self._res.session_factory() as session:
            prompt = await EvaluationRepository(session).active_prompt(kind)
            runs = CalibrationRepository(session)
            judges = await runs.candidates([data.judge_model_id])
            price = await runs.current_price(data.judge_model_id, datetime.now(UTC))
        judge_model = judges.get(data.judge_model_id)
        if prompt is None or judge_model is None or price is None:
            return _JudgeOutcome(failed=True, error="judge_unavailable", mode=kind)
        if not (judge_model.enabled and judge_model.provider_enabled):
            return _JudgeOutcome(failed=True, error="judge_disabled", mode=kind)
        try:
            key = (
                self._keyring.decrypt(
                    judge_model.api_key_encrypted, aad=str(judge_model.provider_id)
                )
                if judge_model.api_key_encrypted
                else None
            )
        except DecryptionError:
            return _JudgeOutcome(failed=True, error="credentials_unreadable", mode=kind)
        target = replay.Target(
            judge_model.base_url, judge_model.is_local, judge_model.auth_type, key,
            judge_model.name, judge_model.supports_stream,
        )  # fmt: skip

        async def call(
            messages: list[dict[str, str]],
        ) -> tuple[str, str | None, int, int, str | None]:
            body = {
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "max_tokens": self._settings.judge_max_tokens,
            }
            out = await replay.call_candidate(
                self._res.http, self._settings, target, body, resolver=self._resolver
            )
            text = chk.answer_from_response(out.response).content if out.kind == "ok" else None
            return (
                out.kind,
                text,
                out.prompt_tokens or 0,
                out.completion_tokens or 0,
                out.error_code,
            )

        # The marker depends on the request and the model, not on the run: judging the same
        # pair again (in another run with the same sample) must give the judge the same text.
        pair = uuid.uuid5(data.request_id, str(data.model_id))
        boundary = jd.boundary_for(pair, prompt.version)
        limit = self._settings.judge_text_limit
        request_text = jd.render_request(data.request, limit)
        rubric = str(config.get("rubric", ""))
        concurrency = judge_model.max_concurrency or self._settings.calibration_default_concurrency
        rps = float(judge_model.max_rps or self._settings.calibration_default_rps)

        usage = jd.Usage()
        outcome = _JudgeOutcome(mode=kind, version=prompt.version)
        async with self._limiter.slot(
            str(judge_model.provider_id), max_concurrency=concurrency, max_rps=rps
        ):
            if kind == "grade":
                messages = jd.build_grade_messages(
                    prompt.system_prompt, prompt.user_template,
                    task_type=data.task_type, rubric=rubric, request=request_text,
                    truth_label="EXPECTED" if basis == "expected" else "REFERENCE",
                    truth=truth.as_text(), candidate=answer.as_text(),
                    boundary=boundary, limit=limit,
                )  # fmt: skip
                graded = await jd.ask(
                    call, messages, jd.GradeReply, self._settings.judge_invalid_retries
                )
                self._add(usage, graded.usage)
                if graded.reply is not None:
                    outcome.part = scoring.grade_part(
                        graded.reply.score, min_score, required=required
                    )
                    outcome.score = graded.reply.score
                    outcome.verdict = graded.reply.verdict
                    outcome.rationale = graded.reply.rationale
                else:
                    outcome.error = graded.error
            else:
                verdicts: list[str] = []
                notes: list[str] = []
                for candidate_first in (True, False):  # the two orders
                    messages = jd.build_pairwise_messages(
                        prompt.system_prompt, prompt.user_template,
                        task_type=data.task_type, rubric=rubric, request=request_text,
                        truth=truth.as_text(), candidate=answer.as_text(),
                        candidate_first=candidate_first, boundary=boundary, limit=limit,
                    )  # fmt: skip
                    paired = await jd.ask(
                        call, messages, jd.PairReply, self._settings.judge_invalid_retries
                    )
                    self._add(usage, paired.usage)
                    if paired.reply is None:
                        outcome.error = paired.error
                        break
                    candidate_letter = "A" if candidate_first else "B"
                    verdicts.append(
                        "candidate" if paired.reply.winner in (candidate_letter, "tie") else "truth"
                    )
                    order = "cand-first" if candidate_first else "cand-second"
                    why = paired.reply.rationale[:200]
                    notes.append(f"{order}: {paired.reply.winner} ({why})")
                if len(verdicts) == 2:
                    outcome.part = scoring.pairwise_part(
                        verdicts[0], verdicts[1], required=required
                    )
                    outcome.verdict = "|".join(verdicts)
                    outcome.rationale = " / ".join(notes)
        outcome.usage = usage
        outcome.cost = pricing.cost_usd(price, usage.prompt_tokens, usage.completion_tokens)

        if outcome.part is None:
            error = outcome.error or "no_verdict"
            if error.startswith("temporary:") and attempt < len(self._settings.retry_delays_ms):
                raise TemporaryError(f"the judge is unavailable ({error})")
            outcome.failed = True
            outcome.error = error
        return outcome

    @staticmethod
    def _add(total: jd.Usage, extra: jd.Usage) -> None:
        total.calls += extra.calls
        total.prompt_tokens += extra.prompt_tokens
        total.completion_tokens += extra.completion_tokens

    # ---- storing the verdict ----
    async def _store(
        self,
        data: JobData,
        outcomes: list[chk.CheckOutcome],
        judge: _JudgeOutcome,
        equal: bool,
        basis: str,
        valid_args: bool | None,
    ) -> None:
        schema_results = [o.passed for o in outcomes if o.kind == "json_schema" and o.applied]
        valid_schema = all(schema_results) if schema_results else None
        if judge.failed:
            status, quality, passed = "judge_failed", None, None
        else:
            quality, passed = scoring.combine(outcomes, judge.part)
            status = "ok" if quality is not None else "unevaluated"
        usage = judge.usage or jd.Usage()
        values: dict[str, Any] = {
            "result_id": data.result_id,
            "run_id": data.run_id,
            "judge_prompt_version": judge.version,
            "status": status,
            "basis": basis,
            "quality": quality,
            "passed": passed,
            "equal_to_truth": equal,
            "checks": [o.as_json() for o in outcomes],
            "valid_schema": valid_schema,
            "valid_args": valid_args,
            "judge_mode": judge.mode,
            "judge_model_id": data.judge_model_id if judge.mode else None,
            "judge_score": judge.score,
            "judge_verdict": judge.verdict,
            "judge_rationale": judge.rationale,
            "judge_error": (judge.error or "")[:300] or None,
            "judge_calls": usage.calls,
            "judge_prompt_tokens": usage.prompt_tokens,
            "judge_completion_tokens": usage.completion_tokens,
            "judge_cost_usd": judge.cost,
        }
        async with self._res.session_factory() as session, session.begin():
            repo = EvaluationRepository(session)
            if await repo.insert_evaluation(values):
                await finish_evaluation_if_done(
                    session, data.run_id, self._settings.calibration_min_samples
                )
