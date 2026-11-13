"""FastAPI lifespan: open resources at start, close them on shutdown.

On SIGTERM uvicorn first stops accepting connections and lets in-flight requests finish
(--timeout-graceful-shutdown); only then does the code after `yield` run.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.resources import open_resources

log = get_logger(__name__)


async def _bootstrap_admin(app: FastAPI, settings: Settings) -> None:
    """Create the first administrator from ADMIN_EMAIL / ADMIN_PASSWORD if none exists yet."""
    if not settings.admin_email or not settings.admin_password.get_secret_value():
        return
    # Imported here: core must not depend on feature modules at import time.
    from app.modules.auth.service import AuthService

    async with app.state.resources.session_factory() as session:
        await AuthService(session, settings).ensure_first_admin(
            settings.admin_email, settings.admin_password.get_secret_value()
        )


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.resources = await open_resources(settings)
        await _bootstrap_admin(app, settings)
        log.info("startup_complete")
        try:
            yield
        finally:
            log.info("shutdown_started")
            await app.state.resources.aclose()

    return lifespan
