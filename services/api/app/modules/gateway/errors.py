"""Errors in the OpenAI format: {"error": {"message", "type", "param", "code"}}.

Applications written for the OpenAI API parse this shape, so the gateway uses it for every
error it produces itself (authentication, limits, upstream failures).
"""

from typing import Any

from fastapi.responses import JSONResponse


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        type_: str,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type_
        self.code = code
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.type,
                "param": None,
                "code": self.code,
            }
        }

    def response(self, extra_headers: dict[str, str] | None = None) -> JSONResponse:
        return JSONResponse(
            self.body(),
            status_code=self.status,
            headers={**self.headers, **(extra_headers or {})},
        )


def invalid_api_key() -> GatewayError:
    return GatewayError(
        401,
        "Incorrect API key provided. Check the key and try again.",
        "invalid_request_error",
        "invalid_api_key",
        {"www-authenticate": "Bearer"},
    )


def missing_api_key() -> GatewayError:
    return GatewayError(
        401,
        "You did not provide an API key. Send it as 'Authorization: Bearer <key>'.",
        "invalid_request_error",
        "invalid_api_key",
        {"www-authenticate": "Bearer"},
    )


def rate_limited(limit: int, retry_after: int) -> GatewayError:
    return GatewayError(
        429,
        f"Rate limit reached: {limit} requests per minute. Try again in {retry_after}s.",
        "rate_limit_error",
        "rate_limit_exceeded",
        {"retry-after": str(max(retry_after, 1))},
    )


def bad_request(message: str, code: str = "invalid_request") -> GatewayError:
    return GatewayError(400, message, "invalid_request_error", code)


def too_large(limit: int) -> GatewayError:
    return GatewayError(
        413,
        f"The request body is larger than the limit of {limit} bytes.",
        "invalid_request_error",
        "request_too_large",
    )


def no_reference_model() -> GatewayError:
    return GatewayError(
        503,
        "This project has no usable reference model configured. Ask an administrator to set one.",
        "server_error",
        "no_reference_model",
    )


def overloaded(provider: str) -> GatewayError:
    return GatewayError(
        503,
        f"Too many concurrent requests to the provider '{provider}'. Try again shortly.",
        "server_error",
        "provider_overloaded",
        {"retry-after": "1"},
    )


def upstream_failed(status: int) -> GatewayError:
    return GatewayError(
        502,
        f"The upstream provider failed (HTTP {status}).",
        "api_error",
        "upstream_error",
    )


def upstream_unreachable() -> GatewayError:
    return GatewayError(
        502,
        "The upstream provider could not be reached.",
        "api_error",
        "upstream_unreachable",
    )


def upstream_timeout(what: str) -> GatewayError:
    return GatewayError(
        504, f"The upstream provider timed out ({what}).", "api_error", "upstream_timeout"
    )
