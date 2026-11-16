"""Readable text out of stored OpenAI-style requests and answers."""

import json
from typing import Any

LIMIT = 700


def clip(text: str, limit: int = LIMIT) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _content_text(content: Any) -> str:
    """`content` is a string or a list of parts (text, image_url...)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, dict):
                parts.append(f"[{part.get('type', 'part')}]")
        return "\n".join(parts)
    return ""


def prompt_text(payload: dict[str, Any] | None) -> str:
    """The last user message: what the model was asked."""
    if not payload:
        return ""
    for message in reversed(payload.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return clip(_content_text(message.get("content")))
    return ""


def answer_text(payload: dict[str, Any] | None) -> str:
    """The text of an answer, or the tool calls it chose (name and arguments)."""
    if not payload:
        return ""
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    calls = message.get("tool_calls") or []
    lines = []
    text = _content_text(message.get("content"))
    if text:
        lines.append(text)
    for call in calls:
        fn = call.get("function", {}) if isinstance(call, dict) else {}
        lines.append(f"→ {fn.get('name', '?')}({_compact(fn.get('arguments'))})")
    return clip("\n".join(lines))


def _compact(arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return str(arguments)
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
