"""OpenAI-compatible mock provider: `uvicorn --factory app.main:create_app`.

Endpoints: GET /v1/models, POST /v1/chat/completions (stream and non-stream), health checks.

Failure injection is per request, through headers (so a test controls exactly one call):
  x-mock-fail: 500 | 503 | 429 | timeout     answer with that error (timeout = hang)
  x-mock-fail-models: a,b                    apply x-mock-fail only to these models
  x-mock-delay-ms: N                         extra delay before processing
  x-mock-output-tokens: N                    pad the answer to about N tokens
  x-mock-drop-after-chunks: N                (stream) break the connection after N chunks
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from app import engine
from app.config import Settings
from app.models import MODELS, MockModel

log = logging.getLogger("mock_llm")


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[Any] | None = None


class ChatRequest(BaseModel):
    # Unknown fields (temperature, top_p, user, ...) are accepted and ignored, as providers do.
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[Message]
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None


def error(
    status: int,
    message: str,
    type_: str,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {"error": {"message": message, "type": type_, "param": None, "code": code}}
    return JSONResponse(body, status_code=status, headers=headers)


class DroppedConnection(Exception):
    """Raised on purpose in the middle of a stream to simulate a provider that breaks."""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Switchyard mock LLM provider")
    # Counters for tests: did the gateway really stop a stream when its client left?
    stats: dict[str, Any] = {
        "requests": 0, "streams_started": 0, "streams_completed": 0, "streams_aborted": 0,
        "models": {},  # calls that reached a model, by model name
    }  # fmt: skip

    async def sleep(seconds: float) -> None:
        scaled = seconds * settings.time_scale
        if scaled > 0:
            await asyncio.sleep(scaled)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        return error(400, f"Invalid request: {loc}: {first['msg']}", "invalid_request_error")

    def check_auth(request: Request) -> JSONResponse | None:
        if not settings.api_key:
            return None
        if request.headers.get("authorization") != f"Bearer {settings.api_key}":
            return error(
                401, "Incorrect API key provided", "invalid_request_error", "invalid_api_key"
            )
        return None

    async def inject_failure(request: Request, model: str | None = None) -> JSONResponse | None:
        delay = request.headers.get("x-mock-delay-ms")
        if delay and delay.isdigit():
            await sleep(int(delay) / 1000)
        only = request.headers.get("x-mock-fail-models")
        if only and model not in {m.strip() for m in only.split(",")}:
            return None  # the failure is meant for other models
        fail = request.headers.get("x-mock-fail", "").lower()
        if fail in {"500", "503"}:
            return error(int(fail), "The mock server is failing on purpose", "server_error")
        if fail == "429":
            return error(
                429,
                "Rate limit reached",
                "rate_limit_error",
                "rate_limit_exceeded",
                {"retry-after": "1"},
            )
        if fail == "timeout":
            await asyncio.sleep(settings.timeout_seconds)  # not scaled: it must outlast clients
            return error(504, "Gateway timeout", "server_error")
        return None

    @app.get("/_stats")
    async def get_stats() -> dict[str, Any]:
        return stats

    @app.get("/health/live")
    @app.get("/health/ready")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models(request: Request) -> Any:
        if (denied := check_auth(request)) is not None:
            return denied
        if (failure := await inject_failure(request)) is not None:
            return failure
        return {
            "object": "list",
            "data": [
                {
                    "id": m.name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "switchyard-mock",
                    "context_window": m.context_window,
                    "tier": m.tier,
                }
                for m in MODELS.values()
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request, body: ChatRequest) -> Any:
        stats["requests"] += 1
        if (denied := check_auth(request)) is not None:
            return denied
        if (failure := await inject_failure(request, body.model)) is not None:
            return failure

        model = MODELS.get(body.model)
        if model is None:
            return error(
                404,
                f"The model `{body.model}` does not exist",
                "invalid_request_error",
                "model_not_found",
            )
        stats["models"][body.model] = stats["models"].get(body.model, 0) + 1
        messages = [m.model_dump() for m in body.messages]
        used = engine.prompt_tokens(messages)
        if used > model.context_window:
            return error(
                400,
                f"This model's maximum context length is {model.context_window} tokens, "
                f"however you requested {used} tokens. Please reduce the length of the messages.",
                "invalid_request_error",
                "context_length_exceeded",
            )

        header = request.headers.get("x-mock-output-tokens", "")
        completion = engine.generate(
            model,
            messages,
            tools=body.tools,
            tool_choice=body.tool_choice,
            response_format=body.response_format,
            max_tokens=body.max_tokens,
            output_tokens=min(int(header), 4000) if header.isdigit() else None,
        )
        out_tokens = engine.completion_tokens(completion)
        usage = {
            "prompt_tokens": used,
            "completion_tokens": out_tokens,
            "total_tokens": used + out_tokens,
        }
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        log.info("chat model=%s stream=%s prompt_tokens=%d", model.name, body.stream, used)

        if not body.stream:
            await sleep(model.ttft_ms / 1000 + out_tokens / model.tokens_per_second)
            message: dict[str, Any] = {"role": "assistant", "content": completion.content}
            if completion.tool_calls:
                message["tool_calls"] = completion.tool_calls
            return {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model.name,
                "choices": [
                    {"index": 0, "message": message, "finish_reason": completion.finish_reason}
                ],
                "usage": usage,
            }

        drop = request.headers.get("x-mock-drop-after-chunks", "")
        include_usage = bool((body.stream_options or {}).get("include_usage"))
        stream = _counted(
            _stream(
                sleep,
                model,
                completion,
                cid,
                created,
                usage,
                include_usage,
                int(drop) if drop.isdigit() else None,
            ),
            stats,
        )
        return StreamingResponse(
            stream, media_type="text/event-stream", headers={"cache-control": "no-cache"}
        )

    return app


async def _counted(inner: AsyncIterator[bytes], stats: dict[str, int]) -> AsyncIterator[bytes]:
    """Count streams that finished and streams that were cut off (client gone, drop)."""
    stats["streams_started"] += 1
    finished = False
    try:
        async for piece in inner:
            yield piece
        finished = True
    finally:
        stats["streams_completed" if finished else "streams_aborted"] += 1


def _chunk(cid: str, created: int, model: str, delta: dict[str, Any], finish: str | None) -> bytes:
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


async def _stream(
    sleep: Any,
    model: MockModel,
    completion: engine.Completion,
    cid: str,
    created: int,
    usage: dict[str, int],
    include_usage: bool,
    drop_after: int | None,
) -> AsyncIterator[bytes]:
    sent = 0

    def check_drop() -> None:
        if drop_after is not None and sent >= drop_after:
            raise DroppedConnection

    await sleep(model.ttft_ms / 1000)  # time to first token
    yield _chunk(cid, created, model.name, {"role": "assistant", "content": ""}, None)

    per_token = 1 / model.tokens_per_second
    if completion.tool_calls:
        for index, call in enumerate(completion.tool_calls):
            head = {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["function"]["name"], "arguments": ""},
                    }
                ]
            }
            yield _chunk(cid, created, model.name, head, None)
            args = call["function"]["arguments"]
            for i in range(0, len(args), 8):
                await sleep(per_token)
                check_drop()
                piece = {
                    "tool_calls": [{"index": index, "function": {"arguments": args[i : i + 8]}}]
                }
                yield _chunk(cid, created, model.name, piece, None)
                sent += 1
    else:
        for piece_text in engine.split_tokens(completion.content or ""):
            await sleep(per_token)
            check_drop()
            yield _chunk(cid, created, model.name, {"content": piece_text}, None)
            sent += 1

    yield _chunk(cid, created, model.name, {}, completion.finish_reason)
    if include_usage:
        final = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model.name,
            "choices": [],
            "usage": usage,
        }
        yield f"data: {json.dumps(final)}\n\n".encode()
    yield b"data: [DONE]\n\n"
