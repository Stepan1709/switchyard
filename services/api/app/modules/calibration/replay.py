"""Sending one recorded request to a candidate model and describing what happened.

Rules of a replay
* Tools are NEVER executed. The request is sent as recorded (with the tool definitions), and if
  the candidate answers with a tool call, that call is stored and compared later; nothing runs
  it and no follow-up request is made.
* The call is made as a stream even if the original was not: only a stream shows the time to
  the first token. The answer is assembled into the same shape as a normal response.
* Every outcome is classified, because the worker reacts differently:
    ok                a result to store
    context_exceeded  the prompt does not fit the model: stored, not counted as bad quality
    refused           the provider rejected the content (4xx): stored with the code
    temporary         timeout, 429, 5xx, connection problem: the job is retried later
    error             not worth retrying (bad key, blocked address): stored as an error
"""

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core import ssrf
from app.core.config import Settings
from app.modules.calibration import pricing
from app.modules.gateway.sse import SseFilter, StreamAssembler

_CONTEXT_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "too long",
    "too many tokens",
)
_MAX_ERROR_BYTES = 16 * 1024


@dataclass(frozen=True)
class Target:
    """Where a candidate lives: enough to call it, without touching the database again."""

    base_url: str
    is_local: bool
    auth_type: str
    api_key: str | None
    model_name: str
    supports_stream: bool = True


@dataclass
class Outcome:
    kind: str  # ok | context_exceeded | refused | temporary | error
    http_status: int | None = None
    error_code: str | None = None
    response: dict[str, Any] | None = None
    finish_reason: str | None = None
    has_tool_calls: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tokens_estimated: bool = False
    ttft_ms: float | None = None
    latency_ms: float | None = None


def build_body(request: dict[str, Any], target: Target) -> dict[str, Any]:
    """The recorded request with the model replaced. Everything else stays as it was."""
    body = dict(request)
    body["model"] = target.model_name
    if target.supports_stream:
        body["stream"] = True
        options = body.get("stream_options")
        body["stream_options"] = {
            **(options if isinstance(options, dict) else {}),
            "include_usage": True,
        }
    else:
        body["stream"] = False
        body.pop("stream_options", None)
    return body


def _ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 3)


def _error_text(data: bytes) -> str:
    try:
        parsed = json.loads(data)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            return f"{err.get('code') or ''} {err.get('message') or ''}".lower()
        if isinstance(err, str):
            return err.lower()
    except ValueError:
        pass
    return data[:300].decode(errors="replace").lower()


def classify_status(status: int, body: bytes) -> Outcome:
    """Turn an HTTP error answer of the provider into an outcome."""
    if status == 429:
        return Outcome("temporary", status, "rate_limited")
    if status >= 500:
        return Outcome("temporary", status, f"http_{status}")
    if status in (401, 403):
        return Outcome("error", status, "authentication_failed")
    if status == 404:
        return Outcome("error", status, "model_not_found")
    if status in (400, 413, 422):
        text = _error_text(body)
        if any(hint in text for hint in _CONTEXT_HINTS):
            return Outcome("context_exceeded", status, "context_length_exceeded")
        return Outcome("refused", status, "rejected_by_provider")
    return Outcome("error", status, f"http_{status}")


def _first_choice(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


def finish_outcome(
    response: dict[str, Any],
    usage: dict[str, Any] | None,
    request: dict[str, Any],
    *,
    ttft_ms: float | None,
    latency_ms: float,
    http_status: int,
) -> Outcome:
    """Build the outcome of a good answer; tokens are estimated when the provider sent no usage."""
    choice = _first_choice(response)
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    prompt = usage.get("prompt_tokens") if usage else None
    completion = usage.get("completion_tokens") if usage else None
    estimated = not (isinstance(prompt, int) and isinstance(completion, int))
    if estimated:
        prompt = pricing.estimate_prompt_tokens(request)
        text = content if isinstance(content, str) else ""
        if tool_calls:
            text += json.dumps(tool_calls, ensure_ascii=False)
        completion = pricing.estimate_text_tokens(text)
    return Outcome(
        "ok",
        http_status,
        response=response,
        finish_reason=choice.get("finish_reason"),
        has_tool_calls=bool(tool_calls),
        prompt_tokens=int(prompt or 0),
        completion_tokens=int(completion or 0),
        tokens_estimated=estimated,
        ttft_ms=ttft_ms,
        latency_ms=latency_ms,
    )


async def call_candidate(
    http: httpx.AsyncClient,
    settings: Settings,
    target: Target,
    request: dict[str, Any],
    *,
    resolver: ssrf.Resolver = ssrf.system_resolver,
) -> Outcome:
    """Send the recorded request to the candidate and describe the result. Never raises for
    problems of the provider; those become an Outcome."""
    started = time.perf_counter()
    try:
        prepared = await ssrf.prepare(
            target.base_url,
            "/chat/completions",
            is_local=target.is_local,
            local_hosts=settings.local_provider_hosts,
            resolver=resolver,
        )
    except ssrf.SSRFError:
        return Outcome("error", None, "address_blocked")

    body = build_body(request, target)
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream" if body["stream"] else "application/json",
        "accept-encoding": "identity",
        **prepared.headers,
    }
    if target.auth_type == "bearer" and target.api_key:
        headers["authorization"] = f"Bearer {target.api_key}"
    outgoing = http.build_request(
        "POST",
        prepared.url,
        content=json.dumps(body).encode(),
        headers=headers,
        extensions=prepared.extensions,
        timeout=httpx.Timeout(
            connect=settings.provider_connect_timeout,
            read=settings.calibration_first_byte_timeout,
            write=settings.http_write_timeout,
            pool=settings.http_pool_timeout,
        ),
    )
    try:
        async with asyncio.timeout(settings.calibration_total_timeout):
            return await _exchange(http, settings, outgoing, request, started)
    except TimeoutError:
        return Outcome("temporary", None, "timeout", latency_ms=_ms(started))
    except httpx.TimeoutException:
        return Outcome("temporary", None, "timeout", latency_ms=_ms(started))
    except httpx.HTTPError as exc:
        return Outcome("temporary", None, type(exc).__name__.lower(), latency_ms=_ms(started))


async def _exchange(
    http: httpx.AsyncClient,
    settings: Settings,
    outgoing: httpx.Request,
    request: dict[str, Any],
    started: float,
) -> Outcome:
    response = await http.send(outgoing, stream=True)
    try:
        if response.status_code != 200:
            data = bytearray()
            async for chunk in response.aiter_raw():
                data.extend(chunk)
                if len(data) > _MAX_ERROR_BYTES:
                    break
            outcome = classify_status(response.status_code, bytes(data))
            outcome.latency_ms = _ms(started)
            return outcome

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            raw = bytearray()
            async for chunk in response.aiter_raw():
                raw.extend(chunk)
                if len(raw) > settings.provider_max_response_bytes:
                    return Outcome("error", 200, "response_too_large", latency_ms=_ms(started))
            try:
                parsed = json.loads(raw)
            except ValueError:
                return Outcome("error", 200, "invalid_response", latency_ms=_ms(started))
            if not isinstance(parsed, dict):
                return Outcome("error", 200, "invalid_response", latency_ms=_ms(started))
            usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else None
            latency = _ms(started)
            return finish_outcome(
                parsed, usage, request, ttft_ms=latency, latency_ms=latency, http_status=200
            )

        assembler = StreamAssembler(settings.provider_max_response_bytes)
        sse = SseFilter(strip_usage_chunk=False, assembler=assembler)
        ttft: float | None = None
        async for chunk in response.aiter_raw():
            if ttft is None and chunk:
                ttft = _ms(started)
            sse.feed(chunk)
        latency = _ms(started)
        assembled = assembler.result(sse.usage)
        if assembled is None:
            return Outcome("error", 200, "invalid_response", ttft_ms=ttft, latency_ms=latency)
        return finish_outcome(
            assembled, sse.usage, request, ttft_ms=ttft, latency_ms=latency, http_status=200
        )
    finally:
        await response.aclose()
