"""Connection test result codes, driven by a fake transport (no network)."""

import ipaddress

import httpx
import pytest

from app.core.config import Settings
from app.modules.registry.connection import run_connection_test

SETTINGS = Settings()


async def resolver(host, port):
    return [ipaddress.ip_address("93.184.216.34")]


async def run(handler, model="m", local=False, url="https://api.example.com/v1", key="sk-abc"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await run_connection_test(
            client,
            SETTINGS,
            base_url=url,
            is_local=local,
            api_key=key,
            model_name=model,
            resolver=resolver,
        )


def ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "m"}, {"id": "n"}]})
    return httpx.Response(200, json={"choices": [{"message": {"content": "p"}}]})


async def test_success_reports_both_calls_and_sends_the_key():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return ok_handler(request)

    result = await run(handler)
    assert result.ok and result.model == "m"
    assert result.models_endpoint.detail == "2 models listed"
    assert result.completion is not None and result.completion.ok
    assert seen == ["Bearer sk-abc", "Bearer sk-abc"]


async def test_no_authorization_header_without_a_key():
    seen = []

    def handler(request):
        seen.append("authorization" in request.headers)
        return ok_handler(request)

    await run(handler, key=None)
    assert seen == [False, False]


async def test_without_a_model_only_the_list_is_checked():
    result = await run(ok_handler, model=None)
    assert result.ok and result.completion is None


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (429, "rate_limited"),
        (302, "unexpected_redirect"),
        (500, "http_500"),
        (404, "http_404"),
    ],
)
async def test_http_errors_become_short_codes(status, code):
    result = await run(lambda r: httpx.Response(status, text="secret body sk-abc"))
    assert not result.ok and result.models_endpoint.error == code
    assert result.completion is None  # no completion is attempted after a failed model list
    assert "sk-abc" not in result.model_dump_json()  # response bodies are never copied


async def test_invalid_json_and_wrong_shapes_are_invalid_responses():
    assert (
        await run(lambda r: httpx.Response(200, text="<html>"))
    ).models_endpoint.error == "invalid_response"
    assert (
        await run(lambda r: httpx.Response(200, json={"models": []}))
    ).models_endpoint.error == "invalid_response"
    assert (
        await run(lambda r: httpx.Response(200, json=[1, 2]))
    ).models_endpoint.error == "invalid_response"

    def no_choices(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"id": "x"})

    result = await run(no_choices)
    assert result.models_endpoint.ok and result.completion.error == "invalid_response"
    assert not result.ok


async def test_timeouts_and_connection_errors():
    def timeout(request):
        raise httpx.ReadTimeout("slow")

    def refused(request):
        raise httpx.ConnectError("refused")

    def other(request):
        raise httpx.DecodingError("bad encoding")

    assert (await run(timeout)).models_endpoint.error == "timeout"
    assert (await run(refused)).models_endpoint.error == "connection_failed"
    odd = (await run(other)).models_endpoint
    assert odd.error == "http_error" and odd.detail == "DecodingError"


async def test_oversized_response_is_reported():
    small = Settings(provider_max_response_bytes=100)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000))
    ) as client:
        result = await run_connection_test(
            client, small, base_url="https://api.example.com/v1", is_local=False,
            api_key=None, model_name=None, resolver=resolver,
        )  # fmt: skip
    assert result.models_endpoint.error == "response_too_large"


async def test_blocked_address_is_reported_without_a_request():
    called = []

    def handler(request):
        called.append(request)
        return ok_handler(request)

    result = await run(handler, url="http://api.example.com/v1")  # http for a non-local host
    assert result.models_endpoint.error == "address_blocked" and not called


async def test_unexpected_exception_does_not_escape():
    def broken(request):
        raise RuntimeError("boom")

    result = await run(broken)
    assert result.models_endpoint.error == "unexpected_error"
    assert result.models_endpoint.detail == "RuntimeError"
