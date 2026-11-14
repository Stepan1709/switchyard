"""Preparing the request body for the provider.

The gateway changes as little as possible: the `model` field is replaced with the model that
will really answer, and for streams `stream_options.include_usage` is switched on (providers
report token usage only in that case). Every other field (messages, tools, tool_choice,
response_format, temperature, unknown extras...) is passed through untouched.
"""

import json
from dataclasses import dataclass
from typing import Any

from app.modules.gateway.errors import GatewayError, bad_request


@dataclass(frozen=True)
class PreparedBody:
    content: bytes  # what is sent to the provider
    stream: bool
    requested_model: str | None  # what the client asked for (recorded, then replaced)
    # True when the client did NOT ask for usage in the stream and the gateway injected the
    # option itself: the extra usage-only chunk must then be removed from the stream.
    strip_usage_chunk: bool


def prepare_body(raw: bytes, target_model: str) -> PreparedBody:
    try:
        data: Any = json.loads(raw)
    except ValueError as exc:
        raise bad_request("The request body is not valid JSON.", "invalid_json") from exc
    if not isinstance(data, dict):
        raise bad_request("The request body must be a JSON object.", "invalid_body")

    stream = data.get("stream", False)
    if not isinstance(stream, bool):
        raise GatewayError(
            400, "'stream' must be a boolean.", "invalid_request_error", "invalid_stream"
        )

    requested = data.get("model")
    data["model"] = target_model

    strip = False
    if stream:
        options = data.get("stream_options")
        if not isinstance(options, dict):
            options = {}
        if not options.get("include_usage"):
            options = {**options, "include_usage": True}
            strip = True
        data["stream_options"] = options

    content = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    return PreparedBody(
        content=content,
        stream=stream,
        requested_model=requested if isinstance(requested, str) else None,
        strip_usage_chunk=strip,
    )
