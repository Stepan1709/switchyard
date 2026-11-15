"""The gateway hot path: authenticate, limit, forward to the provider, stream the answer back.

Rules that shape this code
* no database access: everything comes from the in-memory snapshot (core/snapshot.py);
* the request body is parsed once and only `model` / `stream_options` are changed;
* a streamed answer is forwarded chunk by chunk, never collected first;
* when the client goes away the provider request is cancelled (no tokens wasted);
* once a stream has started the status code is already sent, so failures after that point end
  the stream with an error event instead of switching to another answer;
* recording a finished request must never block or break the response.
"""

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app.core import ssrf
from app.core.config import Settings
from app.core.logging import correlation_id_var, get_logger
from app.core.snapshot import KeyInfo, LiveSnapshot, RouteTarget, SnapshotStore
from app.modules.gateway import errors
from app.modules.gateway.body import PreparedBody, prepare_body
from app.modules.gateway.errors import GatewayError
from app.modules.gateway.ratelimit import RateLimiter, RateResult
from app.modules.gateway.record import Recorder, RequestRecord
from app.modules.gateway.sse import SseFilter, StreamAssembler
from app.modules.projects import keys as client_keys

log = get_logger(__name__)

_TASK_TYPE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_DUMMY_HASH = "0" * 64  # compared for unknown key prefixes so that timing does not reveal them
# Provider statuses that are the client's own mistake and are passed through unchanged.
# Anything else (401/403 = the gateway's provider key is wrong, 404 = wrong model name, 5xx)
# is the operator's problem, not the client's, and becomes a 502.
_PASSTHROUGH = {400, 413, 422, 429}
MODE = "record"


def _ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 3)


class GatewayProxy:
    def __init__(
        self,
        settings: Settings,
        store: SnapshotStore,
        http: httpx.AsyncClient,
        limiter: RateLimiter,
        recorder: Recorder,
        resolver: ssrf.Resolver,
    ) -> None:
        self.settings = settings
        self.store = store
        self.http = http
        self.limiter = limiter
        self.recorder = recorder
        self.resolver = resolver
        self._pepper = settings.key_pepper.get_secret_value()
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._timeout = httpx.Timeout(
            connect=settings.gateway_connect_timeout,
            read=settings.gateway_idle_timeout,
            write=10.0,
            pool=settings.gateway_provider_queue_timeout,
        )

    # ---- helpers ----
    def _snapshot(self) -> LiveSnapshot:
        live = self.store.current
        if live is None:
            raise GatewayError(
                503,
                "The gateway is starting up.",
                "server_error",
                "not_ready",
                {"retry-after": "1"},
            )
        return live

    def authenticate(self, request: Request, live: LiveSnapshot) -> KeyInfo:
        header = request.headers.get("authorization")
        if not header:
            raise errors.missing_api_key()
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise errors.missing_api_key()
        parsed = client_keys.parse_key(token.strip())
        if parsed is None:
            raise errors.invalid_api_key()
        prefix, secret = parsed
        info = live.snapshot.keys.get(prefix)
        # An unknown prefix still goes through the same comparison, so response time does
        # not tell an attacker which prefixes exist.
        matches = client_keys.verify_secret(
            secret, info.secret_hash if info else _DUMMY_HASH, self._pepper
        )
        if info is None or not matches:
            raise errors.invalid_api_key()
        return info

    async def _rate_limit(self, key: KeyInfo) -> dict[str, str]:
        limit = key.rate_limit_rpm or self.settings.gateway_default_rpm
        result: RateResult | None = await self.limiter.hit(key.key_id, limit)
        if result is None:
            return {}
        headers = {
            "x-ratelimit-limit-requests": str(result.limit),
            "x-ratelimit-remaining-requests": str(result.remaining),
            "x-ratelimit-reset-requests": f"{result.reset_seconds}s",
        }
        if not result.allowed:
            raise GatewayError(
                429,
                f"Rate limit reached: {limit} requests per minute.",
                "rate_limit_error",
                "rate_limit_exceeded",
                {**headers, "retry-after": str(max(result.reset_seconds, 1))},
            )
        return headers

    async def _read_body(self, request: Request) -> bytes:
        limit = self.settings.gateway_max_body_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise errors.too_large(limit)  # refuse before reading anything
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:  # chunked uploads have no Content-Length: count as we read
                raise errors.too_large(limit)
            chunks.append(chunk)
        return b"".join(chunks)

    def _forwarded_headers(self, request: Request) -> dict[str, str]:
        prefixes = tuple(self.settings.forward_header_prefixes)
        if not prefixes:
            return {}
        return {k: v for k, v in request.headers.items() if k.lower().startswith(prefixes)}

    def _semaphore(self, provider_id: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(provider_id)
        if sem is None:
            sem = self._semaphores[provider_id] = asyncio.Semaphore(
                self.settings.gateway_provider_max_concurrency
            )
        return sem

    def _finish(self, rec: RequestRecord, started: float) -> None:
        rec.duration_ms = _ms(started)
        try:
            self.recorder.submit(rec)
        except Exception as exc:  # the answer to the client must never depend on bookkeeping
            log.error("record_failed", error=type(exc).__name__)

    # ---- entry point ----
    async def chat(self, request: Request) -> Response:
        started = time.perf_counter()
        request_id = str(uuid.uuid4())
        rec = RequestRecord(request_id=request_id, started_at=datetime.now(UTC), mode=MODE)
        token = correlation_id_var.set(request_id)
        base_headers = {"x-request-id": request_id, "x-switchyard-mode": MODE}
        try:
            try:
                live = self._snapshot()
                key = self.authenticate(request, live)
                rec.key_id, rec.project_id = key.key_id, key.project_id
                base_headers.update(await self._rate_limit(key))

                raw = await self._read_body(request)
                task_type = request.headers.get("x-task-type") or key.default_task_type
                if task_type is not None and not _TASK_TYPE_RE.fullmatch(task_type):
                    raise errors.bad_request(
                        "x-task-type must match [a-z0-9][a-z0-9_-]{0,63}.", "invalid_task_type"
                    )
                rec.task_type = task_type

                project = live.snapshot.projects[key.project_id]
                target = project.reference
                if target is None:
                    raise errors.no_reference_model()
                rec.model, rec.provider = target.model_name, target.provider_name
                rec.model_id, rec.provider_id = target.model_id, target.provider_id
                rec.mask_pii = project.mask_pii
                base_headers["x-switchyard-model"] = target.model_name

                prepared = prepare_body(raw, target.model_name)
                rec.stream, rec.requested_model = prepared.stream, prepared.requested_model
                capture = project.store_bodies
                if capture:
                    # A body bigger than the limit is not kept at all: a cut-off prompt or
                    # answer is useless for replaying and misleading to read.
                    if len(raw) <= self.settings.traffic_max_payload_bytes:
                        rec.request_payload = prepared.original
                        rec.payload_bytes = len(raw)
                    else:
                        rec.payload_skipped = True
                return await self._forward(
                    request, live, target, prepared, rec, base_headers, started, capture
                )
            except GatewayError as exc:
                rec.status = (
                    "rate_limited"
                    if exc.status == 429
                    else ("unauthorized" if exc.status == 401 else "error")
                )
                rec.http_status, rec.error_code = exc.status, exc.code
                self._finish(rec, started)
                return exc.response(base_headers)
        finally:
            correlation_id_var.reset(token)

    # ---- the provider call ----
    async def _forward(
        self,
        request: Request,
        live: LiveSnapshot,
        target: RouteTarget,
        prepared: PreparedBody,
        rec: RequestRecord,
        base_headers: dict[str, str],
        started: float,
        capture: bool,
    ) -> Response:
        sem = self._semaphore(target.provider_id)
        try:
            await asyncio.wait_for(sem.acquire(), self.settings.gateway_provider_queue_timeout)
        except TimeoutError as exc:
            raise errors.overloaded(target.provider_name) from exc

        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                sem.release()

        try:
            upstream = await self._send(request, live, target, prepared, rec, started)
        except BaseException:
            release()  # also on cancellation: a leaked slot would shrink the provider's capacity
            raise

        rec.http_status = upstream.status_code
        content_type = upstream.headers.get("content-type", "")
        is_stream = (
            prepared.stream and upstream.status_code == 200 and "text/event-stream" in content_type
        )

        if is_stream:
            return self._stream_response(
                upstream, prepared, rec, base_headers, started, release, capture
            )

        # Non-streaming answer, or an error answer: read it whole (bounded), then close.
        try:
            body = await self._read_limited(upstream)
        except GatewayError:
            release()
            raise
        finally:
            await upstream.aclose()
        release()
        return self._plain_response(
            upstream, body, content_type, rec, base_headers, started, capture
        )

    async def _send(
        self,
        request: Request,
        live: LiveSnapshot,
        target: RouteTarget,
        prepared: PreparedBody,
        rec: RequestRecord,
        started: float,
    ) -> httpx.Response:
        try:
            req = await ssrf.prepare(
                target.base_url,
                "/chat/completions",
                is_local=target.is_local,
                local_hosts=self.settings.local_provider_hosts,
                resolver=self.resolver,
            )
        except ssrf.SSRFError as exc:
            log.error("provider_address_rejected", provider=target.provider_name, reason=exc.detail)
            raise errors.upstream_unreachable() from exc

        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if prepared.stream else "application/json",
            "accept-encoding": "identity",  # we forward raw bytes: no compression to undo
            **self._forwarded_headers(request),
            **req.headers,
        }
        provider_key = live.provider_keys.get(target.provider_id)
        if target.auth_type == "bearer" and provider_key:
            headers["authorization"] = f"Bearer {provider_key}"

        outgoing = self.http.build_request(
            "POST",
            req.url,
            content=prepared.content,
            headers=headers,
            extensions=req.extensions,
            timeout=self._timeout,
        )
        rec.overhead_ms = _ms(started)  # everything the gateway did before the provider call
        # A stream's headers arrive almost at once, so the wait is short; a normal answer comes
        # only when the model has finished, so it may take as long as the total limit.
        wait = (
            self.settings.gateway_first_byte_timeout
            if prepared.stream
            else self.settings.gateway_total_timeout
        )
        try:
            async with asyncio.timeout(wait):
                return await self.http.send(outgoing, stream=True)
        except TimeoutError as exc:
            raise errors.upstream_timeout("no response") from exc
        except httpx.TimeoutException as exc:
            raise errors.upstream_timeout("connect or read") from exc
        except httpx.HTTPError as exc:
            log.warning(
                "upstream_unreachable", provider=target.provider_name, error=type(exc).__name__
            )
            raise errors.upstream_unreachable() from exc

    async def _read_limited(self, upstream: httpx.Response) -> bytes:
        limit = self.settings.gateway_max_response_bytes
        body = bytearray()
        try:
            async with asyncio.timeout(self.settings.gateway_total_timeout):
                async for chunk in upstream.aiter_raw():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise errors.upstream_failed(upstream.status_code)
        except TimeoutError as exc:
            raise errors.upstream_timeout("reading the answer") from exc
        except httpx.HTTPError as exc:
            raise errors.upstream_unreachable() from exc
        return bytes(body)

    def _plain_response(
        self,
        upstream: httpx.Response,
        body: bytes,
        content_type: str,
        rec: RequestRecord,
        base_headers: dict[str, str],
        started: float,
        capture: bool,
    ) -> Response:
        status = upstream.status_code
        if status >= 400 and status not in _PASSTHROUGH:
            log.warning("upstream_error", provider=rec.provider, http_status=status)
            rec.status, rec.error_code = "upstream_error", "upstream_error"
            rec.http_status = 502  # what the client received, not the provider's own status
            rec.extra["upstream_status"] = status
            self._finish(rec, started)
            return errors.upstream_failed(status).response(base_headers)

        headers = dict(base_headers)
        if status == 429 and "retry-after" in upstream.headers:
            headers["retry-after"] = upstream.headers["retry-after"]
        if status >= 400:
            rec.status = "rate_limited" if status == 429 else "error"
        else:
            rec.status = "ok"
            data = _json_object(body)
            usage = data.get("usage") if data else None
            if isinstance(usage, dict):
                rec.prompt_tokens = usage.get("prompt_tokens")
                rec.completion_tokens = usage.get("completion_tokens")
            if capture:
                if data is not None and len(body) <= self.settings.traffic_max_payload_bytes:
                    rec.response_payload = data
                    rec.payload_bytes += len(body)
                else:
                    rec.payload_skipped = True
        rec.ttft_ms = _ms(started)  # non-streaming: first byte and last byte coincide
        self._finish(rec, started)
        return Response(
            content=body,
            status_code=status,
            media_type=content_type.split(";")[0] or "application/json",
            headers=headers,
        )

    def _stream_response(
        self,
        upstream: httpx.Response,
        prepared: PreparedBody,
        rec: RequestRecord,
        base_headers: dict[str, str],
        started: float,
        release: Any,
        capture: bool,
    ) -> StreamingResponse:
        settings = self.settings
        finalized = False

        async def finalize() -> None:
            """Close the provider connection, free the slot, record. Runs exactly once.

            It is called from the generator's `finally` and, as a safety net, from the
            response's background task: a client that disconnects before the first chunk
            is read means the generator never starts and its `finally` never runs.
            """
            nonlocal finalized
            if finalized:
                return
            finalized = True
            with anyio.CancelScope(shield=True):  # we may be inside a cancelled task
                await upstream.aclose()  # closes the provider connection: generation stops
            release()
            if sse.usage:
                rec.prompt_tokens = sse.usage.get("prompt_tokens")
                rec.completion_tokens = sse.usage.get("completion_tokens")
            if assembler is not None and rec.status == "ok":
                if assembler.overflow:
                    rec.payload_skipped = True
                else:
                    rec.response_payload = assembler.result(sse.usage)
                    rec.payload_bytes += assembler.size
            self._finish(rec, started)

        assembler = StreamAssembler(settings.traffic_max_payload_bytes) if capture else None
        sse = SseFilter(prepared.strip_usage_chunk, assembler)

        async def pump() -> AsyncIterator[bytes]:
            rec.status = "client_disconnected"  # until proven otherwise
            try:
                async with asyncio.timeout(settings.gateway_total_timeout):
                    async for chunk in upstream.aiter_raw():
                        if rec.ttft_ms is None:
                            rec.ttft_ms = _ms(started)
                        for out in sse.feed(chunk):
                            yield out
                    tail = sse.finish()
                    if tail:
                        yield tail
                rec.status = "ok"
            except TimeoutError:
                rec.status, rec.error_code = "timeout", "upstream_timeout"
                yield _error_event("The upstream provider timed out.", "upstream_timeout")
            except httpx.HTTPError as exc:
                rec.status, rec.error_code = "upstream_error", "upstream_error"
                log.warning("stream_broken", provider=rec.provider, error=type(exc).__name__)
                yield _error_event("The upstream provider closed the stream.", "upstream_error")
            finally:
                await finalize()

        return StreamingResponse(
            pump(),
            status_code=upstream.status_code,
            media_type="text/event-stream",
            headers={**base_headers, "cache-control": "no-cache", "x-accel-buffering": "no"},
            background=BackgroundTask(finalize),
        )


def _error_event(message: str, code: str) -> bytes:
    body = {"error": {"message": message, "type": "api_error", "param": None, "code": code}}
    return f"data: {json.dumps(body)}\n\n".encode()


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def models_response(live: LiveSnapshot) -> JSONResponse:
    created = 0
    names = ["auto", *live.snapshot.model_names]
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": n, "object": "model", "created": created, "owned_by": "switchyard"}
                for n in names
            ],
        },
        headers={"x-switchyard-mode": MODE},
    )
