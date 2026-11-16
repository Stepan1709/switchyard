"""Pure parts of stage 9: the recommendation, the choice of a model, sessions, tool arguments."""

import random
import uuid
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.snapshot import ProjectInfo, RouteTarget, Snapshot, TaskRoute
from app.modules.evaluation import checks as chk
from app.modules.evaluation import stats
from app.modules.gateway import routing
from app.modules.gateway.body import build_prepared, parse_body
from app.modules.gateway.session import derive_session_id, session_of, step_of
from app.modules.routing import recommend as rec

# ---- the recommendation ----------------------------------------------------------------------


def stat(name, cost="0.001", *, kind="per_token", pass_rate=0.95, p95=100.0, errors=0.0,
         evaluated=60, ctx=0, insufficient=False, quality=0.9):  # fmt: skip
    return rec.CandidateStats(
        model_id=f"id-{name}",
        name=name,
        cost_type=kind,
        cost_per_request_usd=None if kind == "fixed_monthly" else Decimal(cost),
        n_evaluated=evaluated,
        n_context_exceeded=ctx,
        insufficient_data=insufficient,
        pass_rate=pass_rate,
        quality_mean=quality,
        latency_p95_ms=p95,
        error_rate=errors,
    )


CRITERIA = rec.Criteria(min_pass_rate=0.9, max_error_rate=0.05)


def test_the_cheapest_model_that_passes_comes_first_and_the_reference_last():
    result = rec.recommend(
        [stat("big", "0.01"), stat("small", "0.001"), stat("mid", "0.004")], "id-ref", CRITERIA
    )
    assert result.primary == "id-small"
    assert result.chain == ["id-small", "id-mid", "id-big", "id-ref"]
    assert all(v.eligible for v in result.verdicts)


def test_a_model_below_a_threshold_is_not_eligible_and_says_why():
    result = rec.recommend(
        [
            stat("cheap", "0.0001", pass_rate=0.7),
            stat("slow", "0.0002", p95=900.0),
            stat("flaky", "0.0003", errors=0.2),
            stat("fine", "0.01"),
        ],
        "id-ref",
        rec.Criteria(min_pass_rate=0.9, max_p95_ms=500, max_error_rate=0.05),
    )
    assert result.primary == "id-fine"
    reasons = {v.name: v.reasons for v in result.verdicts}
    assert "pass rate 0.70 is below 0.90" in reasons["cheap"][0]
    assert "p95 latency 900 ms is above 500 ms" in reasons["slow"][0]
    assert "error rate 0.20 is above 0.05" in reasons["flaky"][0]
    assert reasons["fine"] == []


def test_too_little_data_means_no_recommendation_for_that_model():
    result = rec.recommend(
        [stat("new", evaluated=5, insufficient=True, pass_rate=1.0)], "id-ref", CRITERIA
    )
    assert result.primary is None and result.chain == []
    assert "not enough data (5 evaluated answers)" in result.verdicts[0].reasons[0]
    assert "reference model should stay" in result.notes[0]


def test_a_context_that_was_too_small_disqualifies_a_model():
    result = rec.recommend([stat("tiny-context", ctx=3)], "id-ref", CRITERIA)
    assert result.primary is None
    assert "context was too small for 3 request(s)" in result.verdicts[0].reasons[0]


def test_nothing_evaluated_is_not_a_pass():
    c = stat("blank", pass_rate=None, evaluated=0, insufficient=True)
    assert "nothing was evaluated" in rec.judge_candidate(c, CRITERIA).reasons


def test_optional_thresholds_only_apply_when_set():
    slow = stat("slow", p95=5000.0, quality=0.2)
    assert rec.judge_candidate(slow, CRITERIA).eligible
    strict = rec.Criteria(min_pass_rate=0.9, min_quality=0.5, max_p95_ms=1000)
    reasons = rec.judge_candidate(slow, strict).reasons
    assert len(reasons) == 2 and any("quality" in r for r in reasons)
    unknown = stat("unknown", p95=None)  # no latency measured: cannot be shown to be fast enough
    assert not rec.judge_candidate(unknown, strict).eligible


def test_a_server_paid_by_the_month_wins_and_the_recommendation_says_so():
    result = rec.recommend(
        [stat("cloud", "0.0001"), stat("own-server", kind="fixed_monthly")], "id-ref", CRITERIA
    )
    assert result.primary == "id-own-server"
    assert any("paid by the month" in n for n in result.notes)
    assert result.verdicts[1].fixed_cost


def test_ties_go_to_the_faster_model_then_to_the_name():
    result = rec.recommend(
        [
            stat("b", "0.001", p95=200.0),
            stat("a", "0.001", p95=200.0),
            stat("c", "0.001", p95=50.0),
        ],
        None,
        CRITERIA,
    )
    assert result.chain == ["id-c", "id-a", "id-b"]  # no reference: nothing is appended


def test_the_chain_is_bounded_and_the_reference_is_not_repeated():
    many = [stat(f"m{i}", f"0.00{i + 1}") for i in range(8)]
    result = rec.recommend(many, "id-ref", CRITERIA)
    assert len(result.chain) == 5 and result.chain[-1] == "id-ref"
    again = rec.recommend([*many[:2], stat("ref", "0.9")], "id-ref", CRITERIA)
    assert again.chain.count("id-ref") == 1  # the reference as a candidate is not added twice


@given(st.permutations([stat(f"m{i}", f"0.00{i}", p95=float(50 * i)) for i in range(1, 7)]))
def test_the_recommendation_does_not_depend_on_the_order_of_the_input(order):
    base = [stat(f"m{i}", f"0.00{i}", p95=float(50 * i)) for i in range(1, 7)]
    assert rec.recommend(list(order), "id-ref", CRITERIA).chain == (
        rec.recommend(base, "id-ref", CRITERIA).chain
    )


@given(
    st.lists(
        st.tuples(
            st.floats(0, 1),
            st.floats(0, 1),
            st.integers(0, 100),
            st.integers(0, 3),
            st.sampled_from(["per_token", "fixed_monthly"]),
        ),
        max_size=8,
    )
)
def test_every_model_of_the_chain_passed_the_thresholds(items):
    stats_ = [
        stat(f"m{i}", "0.001", kind=k, pass_rate=p, errors=e, evaluated=n, ctx=c,
             insufficient=n < 30)
        for i, (p, e, n, c, k) in enumerate(items)
    ]  # fmt: skip
    result = rec.recommend(stats_, "id-ref", CRITERIA)
    eligible = {v.model_id for v in result.verdicts if v.eligible}
    assert set(result.chain) - {"id-ref"} <= eligible
    assert (result.primary is None) == (not eligible)
    assert len(result.chain) <= 5


# ---- which model answers ---------------------------------------------------------------------


def target(name, provider="p"):
    return RouteTarget(
        model_id=f"id-{name}", model_name=name, provider_id=provider, provider_name=provider,
        base_url="http://x/v1", is_local=True, auth_type="none", api_key_encrypted=None,
        context_window=1000,
    )  # fmt: skip


REF, TINY, MID = target("gpt-4o"), target("small"), target("medium")


def snapshot(routes=None):
    all_targets = {t.model_id: t for t in (REF, TINY, MID)}
    return Snapshot(
        version="v",
        created_at="now",
        keys={},
        projects={},
        model_names=tuple(sorted(t.model_name for t in all_targets.values())),
        targets=all_targets,
        target_ids_by_name={t.model_name: t.model_id for t in all_targets.values()},
        routes=routes or {},
    )


PROJECT = ProjectInfo(project_id="p1", name="shop", reference=REF)
ROUTE = TaskRoute(mode="route", chain=(TINY, REF), rule_version=3)
SHADOW = TaskRoute(mode="shadow", shadow=(TINY,), shadow_rate=0.25)


def plan(task="classification", model=None, routes=None, project=PROJECT):
    return routing.plan_for(snapshot(routes), project, task, model)


@pytest.mark.parametrize("asked", [None, "auto", "gpt-4o", "some-model-nobody-registered"])
def test_the_gateway_decides_when_the_client_names_the_reference_or_something_unknown(asked):
    p = plan(model=asked, routes={"p1|classification": ROUTE})
    assert p is not None and p.mode == "route"
    assert [t.model_name for t in p.chain] == ["small", "gpt-4o"]


def test_a_registered_model_that_is_not_the_reference_is_pinned():
    p = plan(model="medium", routes={"p1|classification": ROUTE})
    assert p is not None and p.mode == "pinned"
    assert [t.model_name for t in p.chain] == ["medium"]  # alone: no rules, no fallbacks


def test_without_a_route_the_reference_answers():
    for task in ("classification", None, "extraction"):
        p = plan(task=task)
        assert p is not None and p.mode == "record" and p.chain == (REF,)


def test_a_route_of_another_task_type_or_project_does_not_apply():
    routes = {"p1|classification": ROUTE}
    assert plan(task="extraction", routes=routes).mode == "record"
    other = ProjectInfo(project_id="p2", name="x", reference=REF)
    assert plan(routes=routes, project=other).mode == "record"


def test_a_route_without_usable_models_falls_back_to_the_reference():
    empty = TaskRoute(mode="route", chain=(), rule_version=1)
    p = plan(routes={"p1|classification": empty})
    assert p is not None and p.mode == "record"


def test_shadow_mode_answers_with_the_reference_and_lists_the_copies():
    p = plan(routes={"p1|classification": SHADOW})
    assert p is not None and p.mode == "shadow" and p.chain == (REF,)
    assert p.shadow == (TINY,) and p.shadow_rate == 0.25
    degenerate = TaskRoute(mode="shadow", shadow=(TINY,), shadow_rate=0.0)
    assert plan(routes={"p1|classification": degenerate}).mode == "record"


def test_the_shadow_share_is_all_or_nothing_per_request():
    p = plan(routes={"p1|classification": SHADOW})
    assert p is not None
    assert routing.shadow_targets(p, lambda: 0.1) == (TINY,)
    assert routing.shadow_targets(p, lambda: 0.25) == ()  # the share is strictly below the rate
    assert routing.shadow_targets(p, lambda: 0.9) == ()
    record = plan()
    assert record is not None and routing.shadow_targets(record, lambda: 0.0) == ()


def test_the_share_of_copies_follows_the_rate():
    p = plan(routes={"p1|classification": SHADOW})
    assert p is not None
    rng = random.Random(1)
    copies = sum(1 for _ in range(4000) if routing.shadow_targets(p, rng.random))
    assert 850 < copies < 1150  # about a quarter


def test_no_reference_and_no_route_means_there_is_nothing_to_answer():
    bare = ProjectInfo(project_id="p1", name="shop", reference=None)
    assert plan(project=bare) is None
    # but a rule or a pinned model still works without a reference
    assert plan(project=bare, routes={"p1|classification": ROUTE}).mode == "route"
    assert plan(project=bare, model="medium").mode == "pinned"


def test_the_validation_schema_travels_with_the_route():
    schema = {"type": "object"}
    route = TaskRoute(mode="route", chain=(TINY,), rule_version=1, validation_schema=schema)
    p = plan(routes={"p1|classification": route})
    assert p is not None and p.validation_schema == schema


def test_the_snapshot_survives_the_copy_in_redis():
    routes = {"p1|classification": ROUTE, "p1|extraction": SHADOW}
    original = snapshot(routes)
    again = Snapshot.from_json(original.to_json())
    assert again.routes == original.routes
    assert (
        again.targets == original.targets
        and again.target_ids_by_name == original.target_ids_by_name
    )
    assert again.route_of("p1", "classification") == ROUTE
    assert again.route_of("p1", None) is None
    # and the old format (a snapshot saved before routes existed) still loads
    old = (
        original.to_json()
        .replace('"routes"', '"routes_gone"')
        .replace('"targets"', '"targets_gone"')
    )
    assert Snapshot.from_json(old).routes == {}


# ---- the body is parsed once ------------------------------------------------------------------


def test_the_parsed_body_is_not_changed_by_building_it_for_a_model():
    parsed = parse_body(b'{"model": "gpt-4o", "messages": [], "stream": true}')
    first = build_prepared(parsed, "small")
    second = build_prepared(parsed, "medium")
    assert parsed["model"] == "gpt-4o" and "stream_options" not in parsed
    assert b'"model":"small"' in first.content and b'"model":"medium"' in second.content
    assert first.original["model"] == second.original["model"] == "gpt-4o"


# ---- sessions --------------------------------------------------------------------------------

OPENING = [
    {"role": "system", "content": "You are an agent."},
    {"role": "user", "content": "Book me a flight."},
]


def test_the_session_id_is_stable_across_the_steps_of_a_conversation():
    step0 = OPENING
    step1 = [*OPENING, {"role": "assistant", "content": None}, {"role": "tool", "content": "ok"}]
    step2 = [*step1, {"role": "assistant", "content": "x"}, {"role": "tool", "content": "y"}]
    ids = {derive_session_id(m) for m in (step0, step1, step2)}
    assert len(ids) == 1 and next(iter(ids)).startswith("auto-")
    assert [step_of(m) for m in (step0, step1, step2)] == [0, 1, 2]


def test_different_openings_give_different_ids():
    other = [OPENING[0], {"role": "user", "content": "Cancel my flight."}]
    assert derive_session_id(OPENING) != derive_session_id(other)
    assert derive_session_id([{"role": "system", "content": "no user yet"}]) is None
    assert derive_session_id("not a list") is None and derive_session_id([1]) is None


def test_a_client_supplied_id_wins_but_only_if_it_looks_like_an_id():
    assert session_of("conv-42", OPENING) == ("conv-42", 0)
    derived, step = session_of("bad id with spaces", OPENING)
    assert derived == derive_session_id(OPENING) and step == 0
    assert session_of("x" * 129, OPENING)[0] == derive_session_id(OPENING)
    assert session_of(None, "nonsense") == (None, None)


# ---- arguments of tool calls ----------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
            },
        },
    },
    {"type": "function", "function": {"name": "ping"}},  # a tool without parameters
]


def calls(*items):
    return chk.Answer(None, tuple(chk.ToolCall(n, a) for n, a in items))


@pytest.mark.parametrize(
    ("answer", "ok"),
    [
        (calls(("search", {"query": "x"})), True),
        (calls(("search", {"query": "x", "limit": 5}), ("ping", {})), True),
        (calls(("search", {})), False),  # a required argument is missing
        (calls(("search", {"query": 5})), False),  # wrong type
        (calls(("search", {"query": "x", "limit": 99})), False),  # out of range
        (calls(("delete_everything", {})), False),  # not a tool of the request
        (calls(("search", "query=x")), False),  # arguments are not an object
        (calls(("search", ["x"])), False),
    ],
)
def test_arguments_must_fit_the_tools_the_request_offered(answer, ok):
    assert (chk.tool_args_errors(answer, TOOLS) == []) is ok


def test_the_arguments_check_is_not_applicable_without_a_tool_call():
    out = chk.run_check("tool_args_valid", {}, chk.Answer("just text"), None, tools=TOOLS)
    assert out.passed is None and not out.applied
    bad = chk.run_check("tool_args_valid", {}, calls(("search", {})), None, tools=TOOLS)
    assert bad.passed is False and "missing 'query'" in bad.detail


def test_without_a_tools_list_only_the_shape_of_the_arguments_is_checked():
    assert chk.tool_args_errors(calls(("anything", {"a": 1})), None) == []
    assert chk.tool_args_errors(calls(("anything", "not an object")), None)


def test_a_tool_schema_with_unknown_keywords_does_not_break_the_check():
    tools = [
        {"type": "function", "function": {"name": "f", "parameters": {"$ref": "#/x", "if": {}}}}
    ]
    assert chk.tool_args_errors(calls(("f", {"a": 1})), tools) == []
    broken = [{"type": "function", "function": {"name": "f", "parameters": {"pattern": "("}}}]
    assert chk.tool_args_errors(calls(("f", {"a": 1})), broken) == []  # unreadable schema: skipped


def test_the_new_check_kind_is_a_valid_configuration():
    assert chk.validate_check_config("tool_args_valid", {}) == {}
    with pytest.raises(ValueError):
        chk.validate_check_config("tool_args_valid", {"anything": 1})


# ---- whole conversations ---------------------------------------------------------------------


def step(session, passed, *, status="ok", eval_status="ok"):
    return stats.ResultRow(
        status=status, eval_status=eval_status if status == "ok" else None,
        quality=1.0 if passed else 0.0, passed=passed if status == "ok" else None,
        session_id=session,
    )  # fmt: skip


def test_a_conversation_passes_only_if_every_step_does():
    rows = [
        step("a", True), step("a", True), step("a", True),  # all three right
        step("b", True), step("b", False),  # one wrong step
        step("c", True), step("c", True, status="error"),  # a failed call
    ]  # fmt: skip
    assert stats.session_outcomes(rows) == (3, 1)


def test_a_conversation_with_an_unjudged_step_is_left_out_not_counted_as_wrong():
    rows = [step("a", True), step("a", True, eval_status="judge_failed"), step("b", True)]
    assert stats.session_outcomes(rows) == (1, 1)  # only "b" could be judged


def test_a_step_that_did_not_fit_the_context_fails_the_conversation():
    rows = [step("a", True), step("a", True, status="context_exceeded")]
    assert stats.session_outcomes(rows) == (1, 0)


def test_requests_without_a_session_do_not_count():
    assert stats.session_outcomes([step(None, True), step(None, False)]) == (0, 0)


def test_the_aggregate_reports_conversations_and_valid_arguments():
    rows = [
        stats.ResultRow(status="ok", eval_status="ok", quality=1.0, passed=True,
                        valid_args=True, session_id="a"),
        stats.ResultRow(status="ok", eval_status="ok", quality=1.0, passed=True,
                        valid_args=False, session_id="a"),
        stats.ResultRow(status="ok", eval_status="ok", quality=0.0, passed=False,
                        valid_args=None, session_id="b"),
    ]  # fmt: skip
    agg = stats.aggregate(rows, 1)
    assert (agg.n_sessions, agg.n_sessions_passed) == (2, 1)
    assert agg.session_pass_rate == 0.5
    assert agg.session_pass_low is not None and agg.session_pass_high is not None
    assert agg.session_pass_low < 0.5 < agg.session_pass_high
    assert agg.valid_args_rate == 0.5  # one of the two answers with tool calls
    assert stats.aggregate([], 1).session_pass_rate is None


@given(
    st.lists(
        st.tuples(st.sampled_from(["a", "b", "c", None]), st.booleans()), min_size=0, max_size=40
    )
)
def test_conversation_counts_are_consistent(items):
    rows = [step(s, p) for s, p in items]
    judged, passed = stats.session_outcomes(rows)
    assert 0 <= passed <= judged <= len({s for s, _ in items if s is not None})
    if judged:
        assert stats.aggregate(rows, 1).session_pass_rate == passed / judged


def test_uuid_helper_for_sessions_is_stable():
    # the sampler gives a conversation a stable id built from its session id
    a = uuid.uuid5(uuid.NAMESPACE_URL, "session:abc")
    assert a == uuid.uuid5(uuid.NAMESPACE_URL, "session:abc")
