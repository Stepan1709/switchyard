"""Calculations of the reports. Pure functions: no database, no clock.

Utilization
  A server that handles four requests at once for ten seconds was busy for ten seconds, not
  forty. Busy time is the length of the UNION of the intervals of its requests, so parallel
  requests cannot push the load above 100% (this is only a model of reality: a GPU batches
  requests and may have free capacity while "busy", which the documentation says).

Judge and people
  How often a judge's verdict agrees with a person's, and Cohen's kappa: the agreement that
  remains once the agreement expected by chance is taken out. A judge that says "pass" to
  everything agrees with people as often as they say "pass", yet its kappa is 0.

Rollback signal
  A rule has made the answers worse if people dislike them noticeably more than before.
  "Noticeably" must survive small samples, so the signal needs enough ratings AND the lower
  end of the confidence interval of the new dislike rate has to be above the old rate.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.modules.evaluation.stats import wilson_interval


def union_length(intervals: Iterable[tuple[float, float]], lo: float, hi: float) -> float:
    """Total length of the union of the intervals, clipped to the window [lo, hi].

    Intervals are (start, end) in seconds; an interval with end < start counts as empty."""
    clipped = sorted((max(s, lo), min(e, hi)) for s, e in intervals if min(e, hi) > max(s, lo))
    total = 0.0
    current_start: float | None = None
    current_end = 0.0
    for start, end in clipped:
        if current_start is None:
            current_start, current_end = start, end
        elif start <= current_end:  # overlaps or touches the interval being built
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    if current_start is not None:
        total += current_end - current_start
    return total


def utilization_pct(busy_seconds: float, window_seconds: float) -> float:
    """Percent of the window the server was busy, never above 100."""
    if window_seconds <= 0:
        return 0.0
    return min(100.0, 100.0 * busy_seconds / window_seconds)


def idle_cost(
    monthly_cost: float, busy_seconds: float, window_seconds: float, month_seconds: float
) -> float:
    """What the idle part of a server paid by the month cost during the window: the share of
    the month's price that falls on the window, times the share of the window nobody used."""
    if window_seconds <= 0 or month_seconds <= 0:
        return 0.0
    idle_share = 1.0 - min(1.0, busy_seconds / window_seconds)
    return monthly_cost * (window_seconds / month_seconds) * idle_share


@dataclass(frozen=True)
class Agreement:
    n: int
    agreed: int
    rate: float | None
    kappa: float | None
    # rows: the judge said pass/fail; columns: the person said pass/fail
    judge_pass_human_pass: int
    judge_pass_human_fail: int
    judge_fail_human_pass: int
    judge_fail_human_fail: int


def agreement(pairs: Sequence[tuple[bool, bool]]) -> Agreement:
    """`pairs` are (judge passed, human passed) for the same answers."""
    n = len(pairs)
    pp = sum(1 for j, h in pairs if j and h)
    pf = sum(1 for j, h in pairs if j and not h)
    fp = sum(1 for j, h in pairs if not j and h)
    ff = sum(1 for j, h in pairs if not j and not h)
    if n == 0:
        return Agreement(0, 0, None, None, 0, 0, 0, 0)
    observed = (pp + ff) / n
    judge_pass, human_pass = (pp + pf) / n, (pp + fp) / n
    expected = judge_pass * human_pass + (1 - judge_pass) * (1 - human_pass)
    # kappa is undefined when chance alone explains everything (both always say the same)
    kappa = None if expected >= 1.0 else (observed - expected) / (1.0 - expected)
    return Agreement(n, pp + ff, observed, kappa, pp, pf, fp, ff)


@dataclass(frozen=True)
class RollbackSignal:
    suggest_rollback: bool
    reason: str
    dislikes_now: int
    ratings_now: int
    rate_now: float | None
    rate_low: float | None  # lower end of the interval of the new rate
    dislikes_before: int
    ratings_before: int
    rate_before: float | None


def rollback_signal(
    dislikes_now: int,
    ratings_now: int,
    dislikes_before: int,
    ratings_before: int,
    *,
    min_ratings: int,
    margin: float,
) -> RollbackSignal:
    """Should a person look at rolling a rule back? Only a signal: nothing is rolled back."""
    rate_now = dislikes_now / ratings_now if ratings_now else None
    rate_before = dislikes_before / ratings_before if ratings_before else None
    interval = wilson_interval(dislikes_now, ratings_now)
    low = interval[0] if interval else None

    def result(suggest: bool, reason: str) -> RollbackSignal:
        return RollbackSignal(
            suggest, reason, dislikes_now, ratings_now, rate_now, low,
            dislikes_before, ratings_before, rate_before,
        )  # fmt: skip

    if ratings_now < min_ratings:
        return result(False, f"only {ratings_now} ratings since the rule: not enough to judge")
    baseline = rate_before if rate_before is not None else 0.0
    if low is not None and low > baseline + margin:
        return result(
            True,
            f"dislikes rose from {baseline:.0%} to {rate_now:.0%} "
            f"(at least {low:.0%} with 95% confidence)",
        )
    return result(False, "the share of dislikes is not clearly higher than before the rule")
