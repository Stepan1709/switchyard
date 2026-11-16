"""Storing the ratings that clients send with `POST /v1/feedback`.

The gateway only queues them (it must not depend on the database). Here the rating is tied to
the request: the request has to exist AND belong to the project of the key that rated it.
Anything else is dropped, because a client must not be able to rate, or even probe, requests
of other projects.

Order is not guaranteed and a client may change its mind, so a rating only replaces an older
one (by the time the client sent it), and a repeated message changes nothing.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.messaging import Message, PermanentError, TemporaryError

log = get_logger(__name__)

_FIND = text(
    """
    SELECT started_at, model_id, task_type FROM requests
    WHERE request_id = :request_id AND project_id = :project_id
    LIMIT 1
    """
)
_UPSERT = text(
    """
    INSERT INTO feedback (id, request_id, project_id, key_id, request_started_at, model_id,
                          task_type, rating, comment, created_at, updated_at)
    VALUES (gen_random_uuid(), :request_id, :project_id, :key_id, :started_at, :model_id,
            :task_type, :rating, :comment, :at, :at)
    ON CONFLICT (request_id) DO UPDATE
        SET rating = EXCLUDED.rating, comment = EXCLUDED.comment, updated_at = EXCLUDED.updated_at
        WHERE feedback.updated_at <= EXCLUDED.updated_at
    """
)


def parse(payload: Any) -> dict[str, Any]:
    """The checked fields of a feedback event; PermanentError when it cannot be one."""
    try:
        rating = payload["rating"]
        if isinstance(rating, bool) or rating not in (1, -1):
            raise ValueError("rating")
        comment = payload.get("comment")
        if comment is not None and (not isinstance(comment, str) or len(comment) > 1000):
            raise ValueError("comment")
        key_id = payload.get("key_id")
        return {
            "request_id": uuid.UUID(payload["request_id"]),
            "project_id": uuid.UUID(payload["project_id"]),
            "key_id": uuid.UUID(key_id) if key_id else None,
            "rating": rating,
            "comment": comment,
            "at": datetime.fromisoformat(payload["submitted_at"]).astimezone(UTC),
        }
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PermanentError("invalid feedback.submitted payload") from exc


class FeedbackWriter:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._retries = len(settings.retry_delays_ms)
        self.stored = 0
        self.dropped = 0

    async def handle(self, message: Message) -> None:
        fields = parse(message.payload)
        try:
            async with self._session_factory() as session, session.begin():
                request = (
                    await session.execute(
                        _FIND,
                        {"request_id": fields["request_id"], "project_id": fields["project_id"]},
                    )
                ).first()
                if request is None:
                    # The log may simply be a little behind the client (it rates as soon as it
                    # has the answer): try again after a pause before giving up.
                    if message.attempt < self._retries:
                        raise TemporaryError("the request is not in the log yet")
                    self.dropped += 1
                    log.warning("feedback_dropped", request_id=str(fields["request_id"]))
                    return
                await session.execute(
                    _UPSERT,
                    {
                        **fields,
                        "started_at": request.started_at,
                        "model_id": request.model_id,
                        "task_type": request.task_type,
                    },
                )
        except SQLAlchemyError as exc:
            raise TemporaryError(f"{type(exc).__name__} while storing feedback") from exc
        self.stored += 1
