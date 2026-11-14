"""What the gateway remembers about each request, handed to a recorder when it finishes.

The record carries metadata only (ids, model, timings, token counts, status). Prompts and
answers are never part of it. The traffic log pipeline (a later stage) plugs in through the
`Recorder` protocol; for now requests are written to the structured log.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.core.logging import get_logger

log = get_logger("gateway")


@dataclass
class RequestRecord:
    request_id: str
    started_at: datetime
    key_id: str | None = None
    project_id: str | None = None
    task_type: str | None = None
    requested_model: str | None = None
    model: str | None = None  # the model that answered
    provider: str | None = None
    mode: str = "record"
    stream: bool = False
    # ok | error | rate_limited | unauthorized | client_disconnected | timeout | upstream_error
    status: str = "error"
    http_status: int | None = None
    error_code: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    ttft_ms: float | None = (
        None  # time from the start of the request to the first byte of the answer
    )
    duration_ms: float | None = None
    overhead_ms: float | None = None  # gateway work before the request left for the provider
    extra: dict[str, object] = field(default_factory=dict)


class Recorder(Protocol):
    def submit(self, record: RequestRecord) -> None: ...


class LogRecorder:
    """Writes one structured log line per request (no content)."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def submit(self, record: RequestRecord) -> None:
        if not self._enabled:
            return
        log.info(
            "gateway_request",
            request_id=record.request_id,
            project_id=record.project_id,
            task_type=record.task_type,
            model=record.model,
            provider=record.provider,
            stream=record.stream,
            status=record.status,
            http_status=record.http_status,
            error_code=record.error_code,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            ttft_ms=record.ttft_ms,
            duration_ms=record.duration_ms,
            overhead_ms=record.overhead_ms,
        )
