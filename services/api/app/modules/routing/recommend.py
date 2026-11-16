"""The recommendation: which model should answer a task type, from the numbers of a run.

A candidate is eligible when ALL of these hold:
  * enough evaluated answers (otherwise the numbers say too little: "not enough data");
  * pass rate >= the threshold (a point estimate; the interval is shown next to it);
  * mean quality >= its threshold, when one is set;
  * p95 latency <= its limit, when one is set;
  * error rate <= its limit (answers that did not fit the context are NOT errors, but a model
    whose context was too small for some of the requests is not eligible: it would fail them
    in production);
Among the eligible ones the cheapest comes first. A model paid by the month has no cost per
request, so it wins over every paid-per-token model that passes the same thresholds (and the
recommendation says so); ties go to the faster model, then to the name, so the result never
depends on the order of the input.

The chain is the eligible models in that order and, as the last resort, the reference
model: it is what the project used before, so it is the one that is known to be acceptable.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from app.modules.routing.models import MAX_CHAIN


@dataclass(frozen=True)
class Criteria:
    min_pass_rate: float = 0.9
    min_quality: float | None = None
    max_p95_ms: float | None = None
    max_error_rate: float = 0.05


@dataclass(frozen=True)
class CandidateStats:
    model_id: str
    name: str
    cost_type: str  # per_token | fixed_monthly
    cost_per_request_usd: Decimal | None
    n_evaluated: int
    n_context_exceeded: int
    insufficient_data: bool
    pass_rate: float | None
    quality_mean: float | None
    latency_p95_ms: float | None
    error_rate: float | None


@dataclass
class Verdict:
    model_id: str
    name: str
    eligible: bool
    reasons: list[str] = field(default_factory=list)  # why not, or empty
    fixed_cost: bool = False
    cost_per_request_usd: Decimal | None = None


@dataclass
class Recommendation:
    primary: str | None
    chain: list[str]  # model ids, primary first, the reference last
    verdicts: list[Verdict]
    notes: list[str] = field(default_factory=list)


def judge_candidate(c: CandidateStats, criteria: Criteria) -> Verdict:
    reasons: list[str] = []
    if c.insufficient_data:
        reasons.append(f"not enough data ({c.n_evaluated} evaluated answers)")
    if c.pass_rate is None:
        reasons.append("nothing was evaluated")
    elif c.pass_rate < criteria.min_pass_rate:
        reasons.append(f"pass rate {c.pass_rate:.2f} is below {criteria.min_pass_rate:.2f}")
    if criteria.min_quality is not None and (
        c.quality_mean is None or c.quality_mean < criteria.min_quality
    ):
        shown = "none" if c.quality_mean is None else f"{c.quality_mean:.2f}"
        reasons.append(f"quality {shown} is below {criteria.min_quality:.2f}")
    if criteria.max_p95_ms is not None and (
        c.latency_p95_ms is None or c.latency_p95_ms > criteria.max_p95_ms
    ):
        shown = "unknown" if c.latency_p95_ms is None else f"{c.latency_p95_ms:.0f} ms"
        reasons.append(f"p95 latency {shown} is above {criteria.max_p95_ms:.0f} ms")
    if c.error_rate is not None and c.error_rate > criteria.max_error_rate:
        reasons.append(f"error rate {c.error_rate:.2f} is above {criteria.max_error_rate:.2f}")
    if c.n_context_exceeded > 0:
        reasons.append(f"the context was too small for {c.n_context_exceeded} request(s)")
    return Verdict(
        model_id=c.model_id,
        name=c.name,
        eligible=not reasons,
        reasons=reasons,
        fixed_cost=c.cost_type == "fixed_monthly",
        cost_per_request_usd=c.cost_per_request_usd,
    )


def _rank_key(v: Verdict, c: CandidateStats) -> tuple[Decimal, float, str]:
    # a server paid by the month costs nothing extra per request
    cost = Decimal(0) if v.fixed_cost else (c.cost_per_request_usd or Decimal(0))
    return cost, c.latency_p95_ms if c.latency_p95_ms is not None else float("inf"), c.name


def recommend(
    candidates: list[CandidateStats], reference_id: str | None, criteria: Criteria
) -> Recommendation:
    verdicts = [judge_candidate(c, criteria) for c in candidates]
    by_id = {c.model_id: c for c in candidates}
    eligible = sorted(
        (v for v in verdicts if v.eligible), key=lambda v: _rank_key(v, by_id[v.model_id])
    )
    notes: list[str] = []
    if not eligible:
        notes.append("No candidate passed the thresholds: the reference model should stay.")
        return Recommendation(None, [], verdicts, notes)

    chain = [v.model_id for v in eligible][: MAX_CHAIN - 1]
    if reference_id is not None and reference_id not in chain:
        chain.append(reference_id)  # the last resort
    if eligible[0].fixed_cost:
        notes.append(
            "The primary model is paid by the month: it costs nothing extra per request, but "
            "check that the server has the capacity for this traffic."
        )
    return Recommendation(chain[0], chain, verdicts, notes)
