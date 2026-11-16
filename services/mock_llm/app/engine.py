"""Deterministic "model" behaviour. No randomness: the same request always gives the same
answer, so tests and calibration runs are reproducible.

Idea: a request has one canonical answer (a label chosen by a hash of the prompt). A model
with accuracy 0.85 returns that answer for ~85% of prompts and a different label for the
rest, and which prompts it gets wrong is also decided by a hash, so it is stable.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from app.models import JUDGE_MODEL, MockModel

DEFAULT_LABELS = ["positive", "negative", "neutral"]
_LABELS_RE = re.compile(r"(?:labels|options|categories)\s*:\s*([^\n]+)", re.IGNORECASE)
_FILLER = [
    *("lorem", "ipsum", "dolor", "sit", "amet", "consectetur", "adipiscing", "elit"),
    *("sed", "do", "eiusmod", "tempor", "incididunt", "ut", "labore", "et", "dolore"),
    *("magna", "aliqua"),
]


@dataclass(frozen=True)
class Completion:
    content: str | None
    tool_calls: list[dict[str, Any]] | None
    finish_reason: str  # "stop" | "length" | "tool_calls"


def _h(*parts: str) -> int:
    digest = hashlib.sha256("\x1f".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _unit(*parts: str) -> float:
    """A stable pseudo-random number in [0, 1) derived from the inputs."""
    return _h(*parts) / 2**64


def count_tokens(text: str) -> int:
    # A rough, deterministic approximation (about 4 characters per token).
    return max(1, math.ceil(len(text) / 4))


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal style: [{"type": "text", "text": "..."}]
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def prompt_tokens(messages: list[dict[str, Any]]) -> int:
    # +4 per message mimics the per-message framing overhead of chat formats.
    return sum(count_tokens(message_text(m)) + 4 for m in messages) + 2


def _labels(text: str) -> list[str]:
    match = _LABELS_RE.search(text)
    if match:
        found = [x.strip().strip(".") for x in re.split(r"[,|;]", match.group(1))]
        found = [x for x in found if x]
        if len(found) >= 2:
            return found
    return DEFAULT_LABELS


def _pick_label(model: MockModel, key: str) -> str:
    labels = _labels(key)
    canonical = _h("label", key) % len(labels)
    if _unit("correct", model.name, key) < model.accuracy:
        return labels[canonical]
    # A wrong answer is still deterministic: a different label chosen by hash.
    offset = 1 + _h("wrong", model.name, key) % (len(labels) - 1)
    return labels[(canonical + offset) % len(labels)]


def _judge(key: str) -> str:
    if "TASK: PAIRWISE COMPARISON" in key:  # two answers in, a winner out
        winner = ("A", "B", "tie")[_h("pair", key) % 3]
        return json.dumps({"winner": winner, "rationale": f"deterministic mock winner {winner}"})
    score = 1 + _h("judge", key) % 5
    verdict = "pass" if score >= 4 else "fail"
    return json.dumps(
        {"score": score, "verdict": verdict, "rationale": f"deterministic mock verdict {score}/5"}
    )


def _first_property(tool: dict[str, Any]) -> tuple[str, str]:
    params = tool.get("function", {}).get("parameters", {}) or {}
    props = params.get("properties", {}) or {}
    if not props:
        return "input", "string"
    name = next(iter(props))
    return name, str(props[name].get("type", "string"))


def _choose_tool(
    model: MockModel, key: str, tools: list[dict[str, Any]], tool_choice: Any
) -> dict[str, Any]:
    forced = tool_choice.get("function", {}).get("name") if isinstance(tool_choice, dict) else None
    index = next((i for i, t in enumerate(tools) if t["function"]["name"] == forced), None)
    if index is None:
        index = _h("tool", key) % len(tools)
        if len(tools) > 1 and _unit("correct-tool", model.name, key) >= model.accuracy:
            index = (index + 1) % len(tools)  # picks the wrong tool
    tool = tools[index]
    arg_name, arg_type = _first_property(tool)
    value: Any = key[:60] if arg_type == "string" else 1
    call_id = f"call_{_h('call', key, tool['function']['name']):016x}"[:21]
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": tool["function"]["name"], "arguments": json.dumps({arg_name: value})},
    }


def _pad(text: str, tokens: int) -> str:
    words = text.split()
    while count_tokens(" ".join(words)) < tokens:
        words.append(_FILLER[len(words) % len(_FILLER)])
    return " ".join(words)


def generate(
    model: MockModel,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    response_format: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    output_tokens: int | None = None,
) -> Completion:
    key = next((message_text(m) for m in reversed(messages) if m.get("role") == "user"), "")

    if tools and tool_choice != "none":
        call = _choose_tool(model, key, tools, tool_choice)
        return Completion(None, [call], "tool_calls")

    if model.name == JUDGE_MODEL:
        text = _judge(key)
    else:
        label = _pick_label(model, key)
        wants_json = (response_format or {}).get("type") in {"json_object", "json_schema"}
        text = json.dumps({"label": label}) if wants_json else label
        if output_tokens and not wants_json:
            text = _pad(text, output_tokens)  # long answers for streaming and load tests

    limit = min(max_tokens or model.max_output_tokens, model.max_output_tokens)
    if count_tokens(text) > limit:
        text = text[: limit * 4]
        return Completion(text, None, "length")
    return Completion(text, None, "stop")


def completion_tokens(completion: Completion) -> int:
    if completion.tool_calls:
        return sum(count_tokens(c["function"]["arguments"]) + 3 for c in completion.tool_calls)
    return count_tokens(completion.content or "")


def split_tokens(text: str) -> list[str]:
    """Pieces for streaming: words with their trailing whitespace, so joining restores the text."""
    return re.findall(r"\S+\s*|\s+", text)
