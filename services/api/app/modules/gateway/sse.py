"""Server-sent events passing through the gateway.

The stream is forwarded as it arrives, without waiting for the end and without re-encoding
the events. While it passes, the filter looks for the `usage` object (token counts) and, when
the gateway had to switch `include_usage` on itself, removes that one extra event so the
client sees the stream it would have got from the provider directly.

Two modes:
* strip=False  chunks are forwarded unchanged, the instant they arrive; the filter only
               reads a copy to find the usage;
* strip=True   events are forwarded whole (a half-received event is held back until it is
               complete, which takes microseconds), except the injected usage-only event.
"""

import json
from typing import Any

MAX_EVENT_BYTES = 1024 * 1024  # an "event" that never ends is flushed instead of buffered forever
_SEPARATORS = (b"\n\n", b"\r\n\r\n")


def _next_boundary(buf: bytes, start: int) -> tuple[int, int] | None:
    """(index where the event ends, length of the separator) for the earliest boundary."""
    best: tuple[int, int] | None = None
    for sep in _SEPARATORS:
        idx = buf.find(sep, start)
        if idx != -1 and (best is None or idx < best[0]):
            best = (idx, len(sep))
    return best


def _data_json(event: bytes) -> Any:
    """The JSON of an event's data lines, or None for [DONE] / comments / non-JSON."""
    lines = [line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:")]
    if not lines:
        return None
    payload = b"\n".join(lines)
    if payload.strip() == b"[DONE]":
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return None


class StreamAssembler:
    """Rebuilds the final answer of a streamed chat completion from its chunks.

    The result has the shape of a normal (non-streaming) response, so stored answers look the
    same whichever way the client asked for them. It stops collecting past `max_bytes`: such an
    answer is not stored at all (a cut-off answer would be misleading), see `overflow`.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max = max_bytes
        self._size = 0
        self.overflow = False
        self._meta: dict[str, Any] = {}
        self._choices: dict[int, dict[str, Any]] = {}

    def add(self, chunk: dict[str, Any]) -> None:
        for key in ("id", "model", "created"):
            if key in chunk and key not in self._meta:
                self._meta[key] = chunk[key]
        for choice in chunk.get("choices") or []:
            index = int(choice.get("index", 0))
            slot = self._choices.setdefault(
                index, {"role": "assistant", "content": [], "tool_calls": {}, "finish": None}
            )
            delta = choice.get("delta") or {}
            if delta.get("role"):
                slot["role"] = delta["role"]
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                self._take(len(piece))
                if not self.overflow:
                    slot["content"].append(piece)
            for call in delta.get("tool_calls") or []:
                entry = slot["tool_calls"].setdefault(
                    int(call.get("index", 0)),
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if call.get("id"):
                    entry["id"] = call["id"]
                function = call.get("function") or {}
                if function.get("name"):
                    entry["function"]["name"] += function["name"]
                if function.get("arguments"):
                    self._take(len(function["arguments"]))
                    if not self.overflow:
                        entry["function"]["arguments"] += function["arguments"]
            if choice.get("finish_reason"):
                slot["finish"] = choice["finish_reason"]

    @property
    def size(self) -> int:
        return self._size

    def _take(self, size: int) -> None:
        self._size += size
        if self._size > self._max:
            self.overflow = True

    def result(self, usage: dict[str, Any] | None) -> dict[str, Any] | None:
        if self.overflow or not self._choices:
            return None
        choices = []
        for index in sorted(self._choices):
            slot = self._choices[index]
            message: dict[str, Any] = {
                "role": slot["role"],
                "content": "".join(slot["content"]) if slot["content"] else None,
            }
            if slot["tool_calls"]:
                message["tool_calls"] = [slot["tool_calls"][i] for i in sorted(slot["tool_calls"])]
            choices.append({"index": index, "message": message, "finish_reason": slot["finish"]})
        return {
            **self._meta,
            "object": "chat.completion",
            "choices": choices,
            "usage": usage,
        }


class SseFilter:
    def __init__(self, strip_usage_chunk: bool, assembler: StreamAssembler | None = None) -> None:
        self._strip = strip_usage_chunk
        self._assembler = assembler
        self._buf = b""
        self.usage: dict[str, Any] | None = None
        self.events = 0
        self.stripped = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        """Take a chunk from the provider; return what to send on to the client."""
        self._buf += chunk
        forwarded: list[bytes] = []
        consumed = 0
        while (boundary := _next_boundary(self._buf, consumed)) is not None:
            end, sep_len = boundary  # `end` is an absolute index in the buffer
            event = self._buf[consumed:end]
            full = self._buf[consumed : end + sep_len]
            consumed = end + sep_len
            self.events += 1
            if self._inspect(event) and self._strip:
                self.stripped += 1
                continue  # the injected usage-only event: dropped
            forwarded.append(full)
        self._buf = self._buf[consumed:]
        if len(self._buf) > MAX_EVENT_BYTES:  # protect memory from a never-ending "event"
            forwarded.append(self._buf)
            self._buf = b""

        if not self._strip:
            return [chunk]  # unchanged and immediate; the scan above was only a read
        return forwarded

    def finish(self) -> bytes:
        """Whatever is left when the provider closes the stream (normally nothing)."""
        rest, self._buf = self._buf, b""
        # Without stripping every chunk was already forwarded as it arrived; the buffer was
        # only a copy for reading usage, so handing it over again would duplicate bytes.
        return rest if self._strip else b""

    def _inspect(self, event: bytes) -> bool:
        """Remember usage if the event carries it. True if it is a usage-only event."""
        # Cheap test first: without an assembler only events that mention usage are parsed.
        if self._assembler is None and b'"usage"' not in event:
            return False
        data = _data_json(event)
        if not isinstance(data, dict):
            return False
        if self._assembler is not None:
            self._assembler.add(data)
        usage = data.get("usage")
        if isinstance(usage, dict):
            self.usage = usage
            return data.get("choices") == []
        return False
