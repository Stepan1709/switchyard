"""Entry point of the control-plane process: `uvicorn app.api_main:app`."""

from fastapi import FastAPI

from app.core.config import Settings
from app.core.errors import install_error_handlers
from app.core.lifespan import make_lifespan
from app.core.logging import configure_logging
from app.core.middleware import CorrelationIdMiddleware
from app.modules.health.router import router as health_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings(service_name="api")
    # Configured at import time so startup messages are JSON as well.
    configure_logging(settings.service_name, settings.env, settings.log_level)
    app = FastAPI(title="Switchyard API", lifespan=make_lifespan(settings))
    app.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(app)
    app.include_router(health_router)
    return app


app = create_app()
