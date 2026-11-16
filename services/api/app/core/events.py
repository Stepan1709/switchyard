"""Event contracts shared by publishers and consumers.

Changes must stay backward compatible (add optional fields, never rename or remove): old and
new processes run side by side during a deploy, and old messages may still sit in a queue.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

REQUEST_COMPLETED = "request.completed"
# Calibration: a command (one replay job) and an event (a run reached an end state).
REPLAY_EXECUTE = "replay.execute"
CALIBRATION_RUN_FINISHED = "calibration.run.finished"
SHADOW_EXECUTE = "shadow.execute"  # send a copy of a live request to a candidate model
RULES_CHANGED = "rules.changed"  # a routing rule or a task type mode changed
JUDGE_EXECUTE = "judge.execute"  # evaluate one replay result (checks and, if needed, the judge)


class RequestCompleted(BaseModel):
    """One finished request through the gateway: what the traffic log stores."""

    model_config = ConfigDict(extra="ignore")  # tolerate fields added by a newer publisher

    request_id: uuid.UUID
    started_at: datetime
    project_id: uuid.UUID
    key_id: uuid.UUID | None = None
    task_type: str | None = None
    mode: str = "record"
    stream: bool = False
    status: str
    http_status: int | None = None
    error_code: str | None = None
    requested_model: str | None = None
    model: str | None = None
    model_id: uuid.UUID | None = None
    provider: str | None = None
    provider_id: uuid.UUID | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    ttft_ms: float | None = None
    duration_ms: float | None = None
    overhead_ms: float | None = None
    request_payload: dict[str, Any] | None = None
    response_payload: dict[str, Any] | None = None
    payload_skipped: bool = False
    payload_masked: bool = False
    # the conversation and the step in it (see gateway/session.py)
    session_id: str | None = None
    step: int | None = None
    # Not a column: models that should get a copy of this request (shadow mode). The writer
    # turns it into jobs in the same transaction that stores the request.
    shadow_model_ids: list[uuid.UUID] = []
