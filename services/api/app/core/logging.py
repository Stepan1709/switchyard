"""Structured JSON logging (structlog) with a correlation id.

Rules: JSON to stdout (12-factor), no log files from inside the app. Prompts, responses,
keys and tokens must never be logged at INFO and above.
"""

import logging
import sys
from contextvars import ContextVar

import structlog
from opentelemetry import trace
from structlog.typing import EventDict, Processor

# One id per request; it is also the id returned to the client as x-request-id.
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def _add_trace_context(_: object, __: str, event_dict: EventDict) -> EventDict:
    """trace_id and span_id of the active span, next to correlation_id, so that a log line
    can be found from a trace and the other way round."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def _add_correlation_id(_: object, __: str, event_dict: EventDict) -> EventDict:
    cid = correlation_id_var.get()
    if cid is not None:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging(service: str, env: str, level: str = "INFO") -> None:
    def add_service(_: object, __: str, event_dict: EventDict) -> EventDict:
        event_dict["service"] = service
        event_dict["env"] = env
        return event_dict

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        add_service,
        _add_correlation_id,
        _add_trace_context,
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    # Standard-library loggers (uvicorn, sqlalchemy, aio-pika...) use the same JSON formatter.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # httpx logs one INFO line (with the full URL) per outgoing request: too noisy, and URLs
    # of providers should not end up in logs by default.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Access logs are written by our middleware (no query strings, no bodies).
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
