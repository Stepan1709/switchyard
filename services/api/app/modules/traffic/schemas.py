import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RequestSummary(BaseModel):
    """A traffic log row without the bodies (the list view)."""

    model_config = ConfigDict(from_attributes=True)

    request_id: uuid.UUID
    started_at: datetime
    key_id: uuid.UUID | None
    task_type: str | None
    mode: str
    stream: bool
    status: str
    http_status: int | None
    error_code: str | None
    requested_model: str | None
    model: str | None
    provider: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    ttft_ms: float | None
    duration_ms: float | None
    overhead_ms: float | None
    payload_skipped: bool
    payload_masked: bool


class RequestDetail(RequestSummary):
    """One row with the stored request and answer."""

    request_payload: dict[str, Any] | None
    response_payload: dict[str, Any] | None


class RequestPage(BaseModel):
    items: list[RequestSummary]
    next_cursor: str | None  # pass it back as `cursor` to get the next page


TaskMode = Literal["record", "shadow", "route"]


class TaskTypeCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")


class TaskTypeUpdate(BaseModel):
    mode: TaskMode


class TaskTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    mode: TaskMode
    auto_registered: bool
    created_at: datetime
