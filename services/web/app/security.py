"""CSRF checks and security headers.

The session cookie is SameSite=Lax (not sent on cross-site POSTs) and every unsafe request must
also carry the CSRF token of the session: a hidden form field, or the `x-csrf-token` header that
HTMX adds to every request. The Origin header is compared with the host as a third layer.
"""

import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# No inline scripts; Alpine evaluates expressions with Function(), hence 'unsafe-eval'.
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)

HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def same_origin(request: Request) -> bool:
    """A browser sends Origin on cross-origin and on all POST requests; it must be this host."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("host", "")
    forwarded = request.headers.get("x-forwarded-host", "")
    allowed = {h for h in (host, forwarded) if h}
    return urlsplit(origin).netloc in allowed


async def check_csrf(request: Request, expected: str) -> None:
    """Raise 403 unless an unsafe request carries the session's token (and the right Origin)."""
    if request.method in SAFE_METHODS:
        return
    if not same_origin(request):
        raise HTTPException(status_code=403, detail="Cross-origin request refused")
    supplied = request.headers.get("x-csrf-token")
    if supplied is None:
        form = await request.form()  # cached on the request: the handler can still read it
        value = form.get("csrf_token")
        supplied = value if isinstance(value, str) else ""
    if not expected or not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="The form has expired: reload the page")
