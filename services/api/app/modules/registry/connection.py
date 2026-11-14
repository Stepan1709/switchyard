"""Connection test of a provider: model list + one tiny completion, with latency.

The result describes what happened in short machine-friendly codes. Response bodies, headers
and the access key are never copied into it: a provider (or something pretending to be one)
could echo them back.
"""

import json
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.core.ssrf import Resolver, ResponseTooLarge, SSRFError, safe_request, system_resolver
from app.modules.registry.schemas import CallResult, ConnectionTestResult


def _error_for(exc: Exception) -> CallResult:
    if isinstance(exc, SSRFError):
        return CallResult(ok=False, error="address_blocked", detail=exc.detail)
    if isinstance(exc, httpx.TimeoutException):
        return CallResult(ok=False, error="timeout")
    if isinstance(exc, httpx.ConnectError):
        return CallResult(ok=False, error="connection_failed")
    if isinstance(exc, ResponseTooLarge):
        return CallResult(ok=False, error="response_too_large")
    if isinstance(exc, httpx.HTTPError):
        return CallResult(ok=False, error="http_error", detail=type(exc).__name__)
    return CallResult(ok=False, error="unexpected_error", detail=type(exc).__name__)


def _status_error(status: int) -> CallResult:
    if status in {401, 403}:
        return CallResult(
            ok=False,
            status_code=status,
            error="authentication_failed",
            detail="the key was rejected",
        )
    if status == 429:
        return CallResult(ok=False, status_code=status, error="rate_limited")
    if 300 <= status < 400:
        return CallResult(ok=False, status_code=status, error="unexpected_redirect")
    return CallResult(ok=False, status_code=status, error=f"http_{status}")


async def _call(
    client: httpx.AsyncClient,
    settings: Settings,
    base_url: str,
    is_local: bool,
    method: str,
    path: str,
    headers: dict[str, str],
    payload: Any,
    resolver: Resolver,
    read_timeout: float,
) -> tuple[CallResult, Any]:
    started = time.perf_counter()
    try:
        response, body = await safe_request(
            client,
            method,
            base_url,
            path,
            is_local=is_local,
            local_hosts=settings.local_provider_hosts,
            headers=headers,
            json=payload,
            request_timeout=httpx.Timeout(settings.provider_connect_timeout, read=read_timeout),
            max_bytes=settings.provider_max_response_bytes,
            resolver=resolver,
        )
    except Exception as exc:  # every failure becomes a short code, none escapes
        return _error_for(exc), None
    latency = round((time.perf_counter() - started) * 1000)
    if not 200 <= response.status_code < 300:
        result = _status_error(response.status_code)
        result.latency_ms = latency
        return result, None
    try:
        data = json.loads(body)
    except ValueError:
        return CallResult(
            ok=False, status_code=response.status_code, latency_ms=latency, error="invalid_response"
        ), None
    return CallResult(ok=True, status_code=response.status_code, latency_ms=latency), data


async def run_connection_test(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    base_url: str,
    is_local: bool,
    api_key: str | None,
    model_name: str | None,
    resolver: Resolver = system_resolver,
) -> ConnectionTestResult:
    headers = {"authorization": f"Bearer {api_key}"} if api_key else {}

    models_result, models_data = await _call(
        client, settings, base_url, is_local, "GET", "/models", headers, None, resolver, 10.0
    )
    if models_result.ok:
        listed = models_data.get("data") if isinstance(models_data, dict) else None
        if not isinstance(listed, list):
            models_result = CallResult(
                ok=False,
                status_code=models_result.status_code,
                latency_ms=models_result.latency_ms,
                error="invalid_response",
            )
        else:
            models_result.detail = f"{len(listed)} models listed"

    completion: CallResult | None = None
    if model_name and models_result.ok:
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        completion, data = await _call(
            client,
            settings,
            base_url,
            is_local,
            "POST",
            "/chat/completions",
            {**headers, "content-type": "application/json"},
            payload,
            resolver,
            settings.provider_read_timeout,
        )
        if completion.ok and not (isinstance(data, dict) and data.get("choices")):
            completion = CallResult(
                ok=False,
                status_code=completion.status_code,
                latency_ms=completion.latency_ms,
                error="invalid_response",
            )

    ok = models_result.ok and (completion is None or completion.ok)
    return ConnectionTestResult(
        ok=ok, models_endpoint=models_result, completion=completion, model=model_name
    )
