"""Pure parts of stage 10: load, agreement of judge and people, the rollback signal, budgets,
the priority of disliked requests."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from redis.asyncio import Redis

from app.core.messaging import PermanentError
from app.core.snapshot import KeyInfo, RouteTarget
from app.modules.analytics import budgets, calc
from app.modules.analytics.service import parse_month
from app.modules.calibration import sampling
from app.modules.calibration.sampling import PoolItem
from app.modules.feedback.writer import parse as parse_feedback
from app.modules.gateway.budget import (
    BudgetGuard,
    counter_name,
    month_key,
    price_of,
    request_cost,
)
from app.modules.gateway.errors import GatewayError
from app.modules.gateway.router import _feedback_payload

# ---- load: the union of intervals ------------------------------------------------------------


def test_parallel_requests_do_not_add_up():
    four_at_once = [(0.0, 10.0)] * 4
    assert calc.union_length(four_at_once, 0, 100) == 10.0
    assert calc.utilization_pct(10.0, 100.0) == 10.0


def test_overlapping_touching_and_separate_intervals():
    assert calc.union_length([(0, 5), (3, 8)], 0, 100) == 8  # overlap: 0..8
    assert calc.union_length([(0, 5), (5, 8)], 0, 100) == 8  # touching: 0..8
    assert calc.union_length([(0, 5), (6, 8)], 0, 100) == 7  # a gap of one second
    assert calc.union_length([(2, 4), (0, 10)], 0, 100) == 10  # one inside the other
    assert calc.union_length([], 0, 100) == 0.0


def test_intervals_are_clipped_to_the_window_and_bad_ones_ignored():
    assert calc.union_length([(-5, 5), (95, 120)], 0, 100) == 10
    assert calc.union_length([(200, 300)], 0, 100) == 0
    assert calc.union_length([(10, 5)], 0, 100) == 0  # the end is before the start


interval = st.tuples(st.floats(-50, 150), st.floats(-50, 150)).map(
    lambda t: (t[0], t[0] + abs(t[1]))
)


@given(st.lists(interval, max_size=60))
def test_the_load_never_exceeds_the_window_whatever_the_overlap(intervals):
    """The property that matters: no number of parallel requests can make a server busier than
    100% of the time."""
    busy = calc.union_length(intervals, 0.0, 100.0)
    assert 0.0 <= busy <= 100.0
    assert calc.utilization_pct(busy, 100.0) <= 100.0
    clipped = [max(0.0, min(e, 100.0) - max(s, 0.0)) for s, e in intervals]
    assert busy >= max(clipped, default=0.0) - 1e-9  # at least the longest single request
    assert busy <= sum(clipped) + 1e-9  # at most all of them back to back


@given(st.lists(interval, max_size=40), st.randoms())
def test_the_load_does_not_depend_on_the_order_of_requests(intervals, rnd):
    shuffled = list(intervals)
    rnd.shuffle(shuffled)
    assert calc.union_length(intervals, 0, 100) == pytest.approx(
        calc.union_length(shuffled, 0, 100)
    )


@given(st.lists(interval, max_size=30), st.lists(interval, max_size=30))
def test_more_requests_never_reduce_the_load(a, b):
    assert calc.union_length(a + b, 0, 100) >= calc.union_length(a, 0, 100) - 1e-9


def test_utilization_edge_cases():
    assert calc.utilization_pct(5, 0) == 0.0
    assert calc.utilization_pct(500, 100) == 100.0  # defensive cap


def test_the_cost_of_idleness():
    month = 30 * 86400.0
    # a whole month, used a quarter of the time: three quarters of the price was wasted
    assert calc.idle_cost(100.0, month / 4, month, month) == pytest.approx(75.0)
    # a day of a month: the day's share of the price, and half of it idle
    assert calc.idle_cost(300.0, 43200.0, 86400.0, month) == pytest.approx(5.0)
    assert calc.idle_cost(100.0, 0, 0, month) == 0.0
    assert calc.idle_cost(100.0, month * 2, month, month) == 0.0  # more busy than the window


# ---- judge and people ------------------------------------------------------------------------


def test_agreement_of_two_raters():
    pairs = [(True, True)] * 6 + [(False, False)] * 2 + [(True, False)] + [(False, True)]
    a = calc.agreement(pairs)
    assert (a.n, a.agreed) == (10, 8) and a.rate == 0.8
    assert (a.judge_pass_human_pass, a.judge_pass_human_fail) == (6, 1)
    assert (a.judge_fail_human_pass, a.judge_fail_human_fail) == (1, 2)
    assert a.kappa == pytest.approx(0.5238, abs=1e-3)


def test_a_judge_that_always_passes_agrees_often_but_has_no_skill():
    pairs = [(True, True)] * 8 + [(True, False)] * 2  # people pass 80%, the judge passes all
    a = calc.agreement(pairs)
    assert a.rate == 0.8
    assert a.kappa == pytest.approx(0.0)  # agreement beyond chance: none


def test_perfect_and_opposite_agreement():
    assert calc.agreement([(True, True), (False, False)]).kappa == pytest.approx(1.0)
    assert calc.agreement([(True, False), (False, True)]).kappa == pytest.approx(-1.0)


def test_agreement_without_data_or_without_variation():
    empty = calc.agreement([])
    assert (empty.n, empty.rate, empty.kappa) == (0, None, None)
    same = calc.agreement([(True, True)] * 5)  # both always pass: chance explains everything
    assert same.rate == 1.0 and same.kappa is None


@given(st.lists(st.tuples(st.booleans(), st.booleans()), max_size=80))
def test_agreement_numbers_are_consistent(pairs):
    a = calc.agreement(pairs)
    assert a.n == len(pairs)
    assert (
        a.judge_pass_human_pass
        + a.judge_pass_human_fail
        + a.judge_fail_human_pass
        + (a.judge_fail_human_fail)
        == a.n
    )
    assert a.agreed == a.judge_pass_human_pass + a.judge_fail_human_fail
    if a.rate is not None:
        assert 0.0 <= a.rate <= 1.0
    if a.kappa is not None:
        assert -1.0 - 1e-9 <= a.kappa <= 1.0 + 1e-9


# ---- the rollback signal ---------------------------------------------------------------------


def signal(now_bad, now_n, before_bad, before_n, **kw):
    return calc.rollback_signal(
        now_bad,
        now_n,
        before_bad,
        before_n,
        min_ratings=kw.get("min", 20),
        margin=kw.get("margin", 0.1),
    )


def test_too_few_ratings_say_nothing():
    s = signal(9, 10, 1, 100)
    assert not s.suggest_rollback and "not enough" in s.reason


def test_a_clear_rise_of_dislikes_suggests_a_rollback():
    s = signal(30, 60, 5, 100)  # 50% now, 5% before
    assert s.suggest_rollback and "rose from 5% to 50%" in s.reason
    assert s.rate_low is not None and s.rate_low > 0.15


def test_a_small_rise_that_may_be_chance_does_not():
    assert not signal(7, 40, 10, 100).suggest_rollback  # 17.5% vs 10%: within the noise
    assert not signal(4, 40, 10, 100).suggest_rollback  # fewer dislikes than before


def test_without_a_baseline_the_new_rate_must_clearly_exceed_the_margin():
    assert signal(20, 30, 0, 0).suggest_rollback
    assert not signal(1, 30, 0, 0).suggest_rollback


@given(st.integers(0, 200), st.integers(0, 200), st.integers(0, 200), st.integers(0, 200))
def test_the_signal_is_a_boolean_with_a_reason_for_any_input(a, b, c, d):
    s = signal(min(a, b), b, min(c, d), d)
    assert isinstance(s.suggest_rollback, bool) and s.reason
    if s.ratings_now < 20:
        assert not s.suggest_rollback


# ---- budgets and the cost of a request ------------------------------------------------------


def target(**kw):
    base = dict(
        model_id="m", model_name="x", provider_id="p", provider_name="p", base_url="http://x/v1",
        is_local=True, auth_type="none", api_key_encrypted=None, context_window=1000,
    )  # fmt: skip
    return RouteTarget(**{**base, **kw})


def test_a_request_is_costed_with_the_price_of_the_model():
    t = target(price_input_per_1m="3.00", price_output_per_1m="15.00")
    assert request_cost(price_of(t), 1000, 200) == Decimal("0.00600000")
    assert request_cost(price_of(t), None, 5) is None  # no usage reported
    assert request_cost(price_of(t), 0, 0) == 0


def test_a_server_paid_by_the_month_or_without_a_price_adds_nothing_per_request():
    assert request_cost(price_of(target(cost_type="fixed_monthly")), 10**6, 10**6) == 0
    assert request_cost(price_of(target()), 1000, 1000) == 0  # per token, but no price yet


def test_counters_live_per_key_and_month():
    assert counter_name("k1", "2026-11") == "budget:k1:2026-11"
    assert month_key(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == "2026-12"
    assert month_key(datetime(2027, 1, 1, tzinfo=UTC)) == "2027-01"


def test_month_bounds_cross_the_year():
    start, end = budgets.month_bounds(datetime(2026, 12, 15, tzinfo=UTC))
    assert (start.month, end.year, end.month) == (12, 2027, 1)
    start, end = budgets.month_bounds(datetime(2026, 2, 3, tzinfo=UTC))
    assert (start.day, end.month, end.day) == (1, 3, 1)


def test_the_month_of_a_report():
    now = datetime(2026, 11, 16, tzinfo=UTC)
    assert parse_month(None, now)[0] == "2026-11"
    label, first, following = parse_month("2026-12", now)
    assert (label, first.isoformat(), following.isoformat()) == (
        "2026-12",
        "2026-12-01",
        "2027-01-01",
    )
    for bad in ("2026-13", "november", "2026/11"):
        with pytest.raises(Exception):  # noqa: B017 (Unprocessable)
            parse_month(bad, now)


def _key(budget):
    return KeyInfo(
        key_id="k1", prefix="abc12345", secret_hash="0" * 64, project_id="p1",
        default_task_type=None, rate_limit_rpm=None, monthly_budget_usd=budget,
    )  # fmt: skip


def _guard(redis) -> BudgetGuard:
    return BudgetGuard(cast(Redis, redis))  # a small stand-in is enough for what the guard calls


class _DeadRedis:
    async def get(self, *_):
        raise ConnectionError("redis is down")

    async def incrby(self, *_):
        raise ConnectionError("redis is down")

    async def expire(self, *_):
        raise ConnectionError("redis is down")


class _Redis:
    def __init__(self, value=None):
        self.value = value
        self.added = 0

    async def get(self, *_):
        return self.value

    async def incrby(self, name, amount):
        self.added += amount

    async def expire(self, *_):
        return True


async def test_a_key_without_a_budget_is_never_blocked():
    assert await _guard(_DeadRedis()).exceeded(_key(None)) is False


async def test_a_key_is_blocked_at_its_limit_not_before():
    guard = lambda v: _guard(_Redis(v))  # noqa: E731
    assert await guard(None).exceeded(_key("1.00")) is False
    assert await guard("999999").exceeded(_key("1.00")) is False
    assert await guard("1000000").exceeded(_key("1.00")) is True
    assert await guard("1500000").exceeded(_key("1.00")) is True


async def test_a_redis_outage_never_blocks_a_request_or_breaks_a_charge():
    guard = _guard(_DeadRedis())
    assert await guard.exceeded(_key("1.00")) is False  # fail open
    await guard.charge("k1", Decimal("0.5"))  # does not raise


async def test_a_charge_adds_whole_millionths_and_skips_nothing_or_less():
    redis = _Redis()
    guard = _guard(redis)
    await guard.charge("k1", Decimal("0.0000015"))  # rounded up: never undercharged
    await guard.charge("k1", Decimal(0))
    assert redis.added == 2


# ---- feedback --------------------------------------------------------------------------------

GOOD = {
    "request_id": str(uuid.uuid4()),
    "project_id": str(uuid.uuid4()),
    "key_id": str(uuid.uuid4()),
    "rating": -1,
    "comment": "wrong answer",
    "submitted_at": "2026-11-16T10:00:00+00:00",
}


def test_a_feedback_event_is_read_and_checked():
    f = parse_feedback(GOOD)
    assert f["rating"] == -1 and f["comment"] == "wrong answer"
    assert f["at"].tzinfo is not None and isinstance(f["request_id"], uuid.UUID)
    assert parse_feedback({**GOOD, "comment": None, "key_id": None})["key_id"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "rating": 0},
        {**GOOD, "rating": 2},
        {**GOOD, "rating": True},
        {**GOOD, "rating": "1"},
        {**GOOD, "request_id": "nope"},
        {**GOOD, "comment": "x" * 1001},
        {**GOOD, "comment": 5},
        {**GOOD, "submitted_at": "yesterday"},
        {k: v for k, v in GOOD.items() if k != "project_id"},
        None,
    ],
)
def test_a_broken_feedback_event_is_a_permanent_error(bad):
    with pytest.raises(PermanentError):
        parse_feedback(bad)


def test_the_feedback_request_of_a_client_is_validated():
    rid = str(uuid.uuid4())
    ok = _feedback_payload(f'{{"request_id": "{rid}", "rating": 1}}'.encode())
    assert ok == {"request_id": rid, "rating": 1, "comment": None}
    assert (
        _feedback_payload(f'{{"request_id": "{rid}", "rating": -1, "comment": "bad"}}'.encode())[
            "comment"
        ]
        == "bad"
    )
    for body in (
        b"not json",
        b"[]",
        b'{"rating": 1}',
        f'{{"request_id": "{rid}", "rating": 0}}'.encode(),
        f'{{"request_id": "{rid}", "rating": true}}'.encode(),
        f'{{"request_id": "{rid}", "rating": 1, "comment": 7}}'.encode(),
        f'{{"request_id": "{rid}", "rating": 1, "comment": "{"x" * 1001}"}}'.encode(),
    ):
        with pytest.raises(GatewayError) as err:
            _feedback_payload(body)
        assert err.value.status == 400


# ---- the priority of disliked requests ------------------------------------------------------


def item(n, disliked=False, dup=None):
    return PoolItem(
        uuid.UUID(int=n),
        "traffic",
        f"d{dup if dup is not None else n}",
        100,
        False,
        disliked=disliked,
    )


def test_disliked_requests_take_up_to_their_share_of_the_sample():
    pool = [item(i, disliked=i < 20) for i in range(100)]
    picked = sampling.choose_with_priority(pool, 10, "random", 7, 0.5)
    assert len(picked) == 10
    assert sum(1 for p in picked if p.disliked) == 5  # half of the sample, not more


def test_with_fewer_disliked_requests_than_the_share_all_of_them_are_used():
    pool = [item(i, disliked=i < 3) for i in range(50)]
    picked = sampling.choose_with_priority(pool, 20, "random", 1, 0.5)
    assert sum(1 for p in picked if p.disliked) == 3 and len(picked) == 20


def test_without_any_dislike_it_is_the_ordinary_sample():
    pool = [item(i) for i in range(30)]
    assert sampling.choose_with_priority(pool, 8, "stratified", 3, 0.5) == sampling.choose(
        pool, 8, "stratified", 3
    )


@given(
    st.lists(st.tuples(st.integers(0, 500), st.booleans()), max_size=80, unique_by=lambda t: t[0]),
    st.integers(1, 60),
    st.sampled_from(["random", "stratified"]),
    st.integers(0, 2**31 - 1),
)
def test_a_priority_sample_is_a_duplicate_free_subset_of_the_right_size(rows, n, strategy, seed):
    pool = [item(i, d) for i, d in rows]
    picked = sampling.choose_with_priority(pool, n, strategy, seed, 0.5)
    assert len(picked) == min(n, len(pool))
    assert len({p.request_id for p in picked}) == len(picked)
    assert {p.request_id for p in picked} <= {p.request_id for p in pool}
    assert picked == sampling.choose_with_priority(list(reversed(pool)), n, strategy, seed, 0.5)
    available = sum(1 for _, d in rows if d)
    assume(available > 0)
    assert sum(1 for p in picked if p.disliked) >= min(available, -(-n // 2), len(picked))
