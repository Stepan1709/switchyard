import json

from app import engine
from app.models import MODELS

TINY, MID, STRONG, JUDGE = (
    MODELS[n] for n in ("mock-tiny", "mock-mid", "mock-strong", "mock-judge")
)


def ask(model, text, **kw):
    return engine.generate(model, [{"role": "user", "content": text}], **kw)


def perfect(model):
    return type(model)(**{**model.__dict__, "accuracy": 1.0})


def test_same_request_always_gives_the_same_answer():
    for model in MODELS.values():
        assert ask(model, "hello there") == ask(model, "hello there")


def test_default_labels_and_labels_from_the_prompt():
    assert ask(STRONG, "great product").content in engine.DEFAULT_LABELS
    out = ask(STRONG, "Classify this.\nLabels: billing, shipping, other").content
    assert out in {"billing", "shipping", "other"}


def test_quality_is_ordered_and_close_to_configured_accuracy():
    prompts = [f"Review number {i}. Options: a, b, c, d" for i in range(600)]

    def accuracy(model):
        # compared with the answer a "perfect" model (accuracy 1.0) gives
        same = sum(ask(model, p).content == ask(perfect(model), p).content for p in prompts)
        return same / len(prompts)

    tiny, mid, strong = accuracy(TINY), accuracy(MID), accuracy(STRONG)
    assert tiny < mid < strong
    for got, want in ((tiny, 0.60), (mid, 0.85), (strong, 0.97)):
        assert abs(got - want) < 0.07, (got, want)


def test_wrong_answers_are_still_valid_labels():
    prompts = [f"item {i}" for i in range(200)]
    wrong = [p for p in prompts if ask(TINY, p).content != ask(perfect(TINY), p).content]
    assert wrong  # tiny does make mistakes
    for p in wrong:
        assert ask(TINY, p).content in engine.DEFAULT_LABELS


def test_json_mode_returns_valid_json():
    out = ask(MID, "x", response_format={"type": "json_object"})
    assert json.loads(out.content)["label"] in engine.DEFAULT_LABELS


def test_judge_returns_structured_verdict():
    data = json.loads(ask(JUDGE, "Rate this answer").content)
    assert 1 <= data["score"] <= 5
    assert data["verdict"] in {"pass", "fail"} and data["rationale"]


def test_judge_compares_two_answers_when_asked_to():
    prompt = "TASK: PAIRWISE COMPARISON " + chr(10) + "ANSWER A: x " + chr(10) + "ANSWER B: y"
    first = json.loads(ask(JUDGE, prompt).content)
    assert first["winner"] in {"A", "B", "tie"} and first["rationale"]
    assert json.loads(ask(JUDGE, prompt).content) == first  # deterministic
    seen = {json.loads(ask(JUDGE, f"{prompt} {i}").content)["winner"] for i in range(40)}
    assert seen == {"A", "B", "tie"}  # every outcome is possible


TOOLS = [
    {
        "type": "function",
        "function": {"name": "search", "parameters": {"properties": {"query": {"type": "string"}}}},
    },
    {
        "type": "function",
        "function": {"name": "calc", "parameters": {"properties": {"expr": {"type": "string"}}}},
    },
]


def test_tool_call_is_returned_with_json_arguments():
    out = ask(STRONG, "find cats", tools=TOOLS)
    assert out.finish_reason == "tool_calls" and out.content is None
    call = out.tool_calls[0]
    name = call["function"]["name"]
    assert name in {"search", "calc"}
    args = json.loads(call["function"]["arguments"])
    assert list(args) == [("query" if name == "search" else "expr")]


def test_forced_tool_choice_is_respected_and_none_disables_tools():
    forced = {"type": "function", "function": {"name": "calc"}}
    out = ask(TINY, "q", tools=TOOLS, tool_choice=forced)
    assert out.tool_calls[0]["function"]["name"] == "calc"
    assert ask(TINY, "q", tools=TOOLS, tool_choice="none").tool_calls is None


def test_tool_choice_quality_differs_between_models():
    prompts = [f"task {i}" for i in range(300)]

    def agree(model):
        return sum(
            ask(model, p, tools=TOOLS).tool_calls[0]["function"]["name"]
            == ask(perfect(model), p, tools=TOOLS).tool_calls[0]["function"]["name"]
            for p in prompts
        )

    assert agree(TINY) < agree(STRONG)


def test_max_tokens_truncates_and_reports_length():
    out = ask(STRONG, "x", output_tokens=200, max_tokens=5)
    assert out.finish_reason == "length" and engine.count_tokens(out.content) <= 5


def test_output_padding_reaches_requested_size():
    assert engine.count_tokens(ask(STRONG, "x", output_tokens=100).content) >= 100


def test_split_tokens_restores_the_text():
    text = "alpha  beta\ngamma delta "
    assert "".join(engine.split_tokens(text)) == text


def test_token_counting_is_monotonic():
    assert engine.count_tokens("") == 1
    assert engine.count_tokens("a" * 40) > engine.count_tokens("a" * 4)
