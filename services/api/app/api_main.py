"""Entry point of the control-plane process: `uvicorn --factory app.api_main:create_app`.

The app is built by a factory (not at import time) so that importing this module in tools
and tests never requires secrets or touches the environment.
"""

from fastapi import FastAPI

from app.core.config import Settings
from app.core.errors import install_error_handlers
from app.core.lifespan import make_lifespan
from app.core.logging import configure_logging
from app.core.middleware import CorrelationIdMiddleware
from app.modules.auth.router import router as auth_router
from app.modules.health.router import router as health_router
from app.modules.projects.router import router as projects_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings(service_name="api")
    settings.require_auth_secrets()  # refuse to start with missing or weak secrets
    # Configured at import time so startup messages are JSON as well.
    configure_logging(settings.service_name, settings.env, settings.log_level)
    app = FastAPI(title="Switchyard API", lifespan=make_lifespan(settings))
    app.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(projects_router)
    return app
