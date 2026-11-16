"""Aggregates of a calibration run per model. Pure functions on plain numbers.

No numpy: percentiles and the Wilson interval are a few lines, and the inputs are small
(thousands of numbers), so a heavy dependency would buy nothing.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

Z95 = 1.959963984540054  # 95% two-sided


def percentile(values: Sequence[float], p: float) -> float | None:
    """The p-th percentile (0..100) by linear interpolation between neighbours; None if empty.
    The same definition as numpy's default, so results can be cross-checked."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p / 100
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def wilson_interval(successes: int, total: int, z: float = Z95) -> tuple[float, float] | None:
    """Confidence interval of a proportion. Unlike the textbook "p +- z*sqrt(p(1-p)/n)" it stays
    inside [0, 1] and is sensible for small samples and for p close to 0 or 1."""
    if total <= 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    # The estimate always lies inside the interval; clamp away rounding noise of ~1e-16.
    return max(0.0, min(p, centre - half)), min(1.0, max(p, centre + half))


@dataclass
class ResultRow:
    """What the aggregates need to know about one replay result and its evaluation."""

    status: str  # ok | error | context_exceeded | refused
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    ttft_ms: float | None = None
    latency_ms: float | None = None
    cost_usd: Decimal | None = None
    # the evaluation (None when the result was not evaluated)
    eval_status: str | None = None  # ok | judge_failed | unevaluated
    quality: float | None = None
    passed: bool | None = None
    valid_schema: bool | None = None
    basis: str | None = None
    judge_cost_usd: Decimal | None = None


@dataclass
class Aggregate:
    n_total: int
    n_ok: int
    n_error: int
    n_context_exceeded: int
    n_evaluated: int
    n_judge_failed: int
    quality_mean: float | None
    quality_p10: float | None
    pass_rate: float | None
    pass_rate_low: float | None
    pass_rate_high: float | None
    valid_rate: float | None
    prompt_tokens_avg: float | None
    completion_tokens_avg: float | None
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None
    error_rate: float | None
    cost_per_request_usd: Decimal | None
    judge_cost_usd: Decimal | None
    insufficient_data: bool
    basis_counts: dict[str, int] = field(default_factory=dict)


def aggregate(rows: Sequence[ResultRow], min_samples: int) -> Aggregate:
    """Numbers for one model.

    * error rate counts failed and refused calls; answers that did not fit the context are
      left out of it (it is a limit of the model, not a failure of its quality);
    * a judge that gave no verdict (`judge_failed`) is not a zero: those answers are left out
      of quality and pass rate and counted separately;
    * with fewer than `min_samples` evaluated answers the conclusion is marked as insufficient.
    """
    ok = [r for r in rows if r.status == "ok"]
    n_context = sum(1 for r in rows if r.status == "context_exceeded")
    n_error = sum(1 for r in rows if r.status in ("error", "refused"))
    evaluated = [r for r in ok if r.eval_status == "ok" and r.quality is not None]
    judged_failed = sum(1 for r in ok if r.eval_status == "judge_failed")
    passes = [r for r in evaluated if r.passed is not None]
    n_pass = sum(1 for r in passes if r.passed)
    interval = wilson_interval(n_pass, len(passes))
    schema = [r.valid_schema for r in ok if r.valid_schema is not None]
    attempts = len(rows) - n_context
    costs = [r.cost_usd for r in ok if r.cost_usd is not None]
    judge_costs = [r.judge_cost_usd for r in rows if r.judge_cost_usd is not None]
    counts: dict[str, int] = {}
    for r in evaluated:
        counts[r.basis or "none"] = counts.get(r.basis or "none", 0) + 1

    def numbers(values: list[float | None]) -> list[float]:
        return [v for v in values if v is not None]

    return Aggregate(
        n_total=len(rows),
        n_ok=len(ok),
        n_error=n_error,
        n_context_exceeded=n_context,
        n_evaluated=len(evaluated),
        n_judge_failed=judged_failed,
        quality_mean=mean([r.quality for r in evaluated if r.quality is not None]),
        quality_p10=percentile([r.quality for r in evaluated if r.quality is not None], 10),
        pass_rate=n_pass / len(passes) if passes else None,
        pass_rate_low=interval[0] if interval else None,
        pass_rate_high=interval[1] if interval else None,
        valid_rate=sum(1 for v in schema if v) / len(schema) if schema else None,
        prompt_tokens_avg=mean(
            numbers([float(r.prompt_tokens) if r.prompt_tokens is not None else None for r in ok])
        ),
        completion_tokens_avg=mean(
            numbers(
                [
                    float(r.completion_tokens) if r.completion_tokens is not None else None
                    for r in ok
                ]
            )
        ),
        latency_p50_ms=percentile(numbers([r.latency_ms for r in ok]), 50),
        latency_p95_ms=percentile(numbers([r.latency_ms for r in ok]), 95),
        ttft_p50_ms=percentile(numbers([r.ttft_ms for r in ok]), 50),
        ttft_p95_ms=percentile(numbers([r.ttft_ms for r in ok]), 95),
        error_rate=n_error / attempts if attempts > 0 else None,
        cost_per_request_usd=(sum(costs, Decimal(0)) / len(costs)) if costs else None,
        judge_cost_usd=sum(judge_costs, Decimal(0)) if judge_costs else None,
        insufficient_data=len(evaluated) < min_samples,
        basis_counts=counts,
    )
