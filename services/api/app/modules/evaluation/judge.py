"""The LLM judge: how it is asked, how its answer is checked, and what happens when it misbehaves.

* The prompt comes from the database in versions; the code only fills it in.
* Everything written by the candidate (and the request, which users wrote) is DATA. It sits
  between markers that contain a value the candidate could not have known, and the system
  prompt says that nothing inside the markers is an instruction.
* The reply must be a small JSON object. It is validated with Pydantic; if it is not valid
  the judge is asked again (up to `retries` times), and if it still fails the evaluation is
  recorded as `judge_failed`. A judge that fails is NOT a candidate that scores zero.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class GradeReply(BaseModel):
    score: int = Field(ge=1, le=5)
    verdict: Literal["pass", "fail"]
    rationale: str = Field(default="", max_length=2000)


class PairReply(BaseModel):
    winner: Literal["A", "B", "tie"]
    rationale: str = Field(default="", max_length=2000)


def render_template(template: str, values: dict[str, str]) -> str:
    """Fill {{name}} placeholders in ONE pass. Inserted text is never scanned again, so a
    candidate answer that contains "{{candidate}}" cannot expand into anything."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def boundary_for(result_id: uuid.UUID, prompt_version: int) -> str:
    """A marker the candidate could not know when it wrote its answer, but that is the same
    every time this result is judged (a verdict must be reproducible)."""
    digest = hashlib.sha256(f"{result_id}:{prompt_version}".encode()).hexdigest()
    return f"DATA-{digest[:12]}"


def _clip(text: str, limit: int) -> str:
    return (
        text
        if len(text) <= limit
        else text[:limit] + f"\n[... {len(text) - limit} more characters]"
    )


def render_request(request: dict[str, Any], limit: int) -> str:
    """The conversation as plain text, for the judge to read."""
    lines: list[str] = []
    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):  # [{"type": "text", "text": "..."}]
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        text = content if isinstance(content, str) else ""
        if message.get("tool_calls"):
            text += " " + json.dumps(message["tool_calls"], ensure_ascii=False)
        lines.append(f"{message.get('role', '?')}: {text}".strip())
    tools = [
        t.get("function", {}).get("name") for t in request.get("tools") or [] if isinstance(t, dict)
    ]
    if tools:
        lines.append(f"(tools available: {', '.join(str(t) for t in tools)})")
    return _clip("\n".join(lines), limit)


def build_grade_messages(
    system_prompt: str,
    user_template: str,
    *,
    task_type: str,
    rubric: str,
    request: str,
    truth_label: str,
    truth: str,
    candidate: str,
    boundary: str,
    limit: int,
) -> list[dict[str, str]]:
    user = render_template(
        user_template,
        {
            "task_type": task_type,
            "rubric": f"Rubric: {rubric}" if rubric else "",
            "request": request,
            "truth_label": truth_label,
            "truth": _clip(truth, limit),
            "candidate": _clip(candidate, limit),
            "boundary": boundary,
        },
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]


def build_pairwise_messages(
    system_prompt: str,
    user_template: str,
    *,
    task_type: str,
    rubric: str,
    request: str,
    truth: str,
    candidate: str,
    candidate_first: bool,
    boundary: str,
    limit: int,
) -> list[dict[str, str]]:
    """The two answers as A and B. Which of them is the candidate is decided by the caller
    (and asked in both orders); the judge is not told."""
    a, b = (candidate, truth) if candidate_first else (truth, candidate)
    user = render_template(
        user_template,
        {
            "task_type": task_type,
            "rubric": f"Rubric: {rubric}" if rubric else "",
            "request": request,
            "answer_a": _clip(a, limit),
            "answer_b": _clip(b, limit),
            "boundary": boundary,
        },
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_reply[T: BaseModel](model: type[T], text: str | None) -> T | None:
    """The judge's reply as `model`, or None. Tolerates text around the JSON object."""
    if not text:
        return None
    candidates = [text.strip()]
    found = _JSON_OBJECT.search(text)
    if found:
        candidates.append(found.group(0))
    for raw in candidates:
        try:
            return model.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            continue
    return None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0


@dataclass
class AskResult[T: BaseModel]:
    reply: T | None
    usage: Usage
    # why there is no reply: "invalid_reply" | "temporary:<code>" | "<provider error code>"
    error: str | None = None


# A call returns (outcome kind, reply text, prompt tokens, completion tokens, error code).
Call = Callable[[list[dict[str, str]]], Awaitable[tuple[str, str | None, int, int, str | None]]]


async def ask[T: BaseModel](
    call: Call, messages: list[dict[str, str]], model: type[T], retries: int
) -> AskResult[T]:
    """Ask the judge; when the reply is not the agreed JSON, say so and ask again."""
    usage = Usage()
    conversation = list(messages)
    for _ in range(retries + 1):
        kind, text, prompt, completion, code = await call(conversation)
        usage.calls += 1
        usage.prompt_tokens += prompt
        usage.completion_tokens += completion
        if kind == "temporary":
            return AskResult(None, usage, f"temporary:{code}")
        if kind != "ok":
            return AskResult(None, usage, code or kind)
        reply = parse_reply(model, text)
        if reply is not None:
            return AskResult(reply, usage)
        conversation = [
            *messages,
            {"role": "assistant", "content": (text or "")[:2000]},
            {
                "role": "user",
                "content": "That reply is not valid. Answer with the JSON object only, in "
                "exactly the format described.",
            },
        ]
    return AskResult(None, usage, "invalid_reply")
