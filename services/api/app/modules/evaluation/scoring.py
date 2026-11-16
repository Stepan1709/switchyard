"""Combining the parts of an evaluation into one quality number and one verdict. Pure.

Scale 0..1:  a deterministic check counts 0 or 1;  the judge counts (score - 1) / 4, and an
answer equal to the truth counts 1 without asking the judge at all.
Quality is the mean of the parts that were applied.
Passed means: every REQUIRED check passed and the judge part (if there is one) reached its
minimum score. An optional check that fails only lowers the quality.
"""

from dataclasses import dataclass

from app.modules.evaluation.checks import CheckOutcome

PAIR_CONSISTENT_WIN = 1.0
PAIR_INCONSISTENT = 0.5
PAIR_LOSS = 0.0


@dataclass(frozen=True)
class JudgePart:
    """The judge's share of an evaluation."""

    quality: float  # 0..1
    passed: bool
    required: bool = True


def grade_part(score: int, min_score: int, *, required: bool = True) -> JudgePart:
    return JudgePart((score - 1) / 4, score >= min_score, required)


def equal_part(*, required: bool = True) -> JudgePart:
    """The answer is the same as the expected one: full marks, no judge call needed."""
    return JudgePart(1.0, True, required)


def pairwise_part(first: str, second: str, *, required: bool = True) -> JudgePart:
    """Two judgements of the same pair in opposite orders.

    Each is "candidate" (it won or tied) or "truth" (the reference was clearly better). A
    judge that prefers whatever is shown first contradicts itself between the two orders:
    that is a position bias, and neither verdict can be trusted, so it scores in the middle
    and does not pass."""
    wins = [first == "candidate", second == "candidate"]
    if all(wins):
        return JudgePart(PAIR_CONSISTENT_WIN, True, required)
    if any(wins):
        return JudgePart(PAIR_INCONSISTENT, False, required)
    return JudgePart(PAIR_LOSS, False, required)


def combine(
    outcomes: list[CheckOutcome], judge: JudgePart | None
) -> tuple[float | None, bool | None]:
    """(quality, passed); (None, None) when nothing could be applied."""
    applied = [o for o in outcomes if o.applied]
    qualities = [1.0 if o.passed else 0.0 for o in applied]
    if judge is not None:
        qualities.append(judge.quality)
    if not qualities:
        return None, None
    passed = all(o.passed for o in applied if o.required)
    if judge is not None and judge.required:
        passed = passed and judge.passed
    return sum(qualities) / len(qualities), passed
