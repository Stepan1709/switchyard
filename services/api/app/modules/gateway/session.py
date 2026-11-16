"""Which conversation a request belongs to, and which step of it.

An agent calls the model many times for one task, and every call carries the whole history so
far. To judge whole conversations later, the calls of one conversation have to be recognised.

* The client can say so: the `x-session-id` header (anything up to 128 safe characters).
* Otherwise the id is derived from the start of the conversation, which every call of the same
  conversation repeats unchanged: the system messages and the first user message.
  Two different users who open with the same words get the same derived id; the calibration
  sampler throws such sessions out (it only accepts one request per step, with no gaps).

`step` is the number of assistant turns already in the history: 0 for the first call.
"""

import hashlib
import json
import re
from typing import Any

_SESSION_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def derive_session_id(messages: Any) -> str | None:
    """A stable id from the opening of the conversation, or None if it has no user message."""
    if not isinstance(messages, list):
        return None
    opening: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            return None
        opening.append([message.get("role"), message.get("content")])
        if message.get("role") == "user":
            digest = hashlib.sha256(
                json.dumps(opening, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            return f"auto-{digest[:32]}"
    return None


def step_of(messages: Any) -> int | None:
    """How many assistant turns precede this call."""
    if not isinstance(messages, list):
        return None
    return sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant")


def session_of(header: str | None, messages: Any) -> tuple[str | None, int | None]:
    """(session id, step). A header that does not look like an id is ignored, not trusted."""
    if header is not None and _SESSION_RE.fullmatch(header):
        return header, step_of(messages)
    return derive_session_id(messages), step_of(messages)
