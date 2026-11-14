"""The message format shared by every publisher and consumer.

Body: the JSON payload only. Everything else travels in AMQP properties:
  message_id      UUID, created once and never changed when the message is sent again
                  (it is what consumers use to drop duplicates)
  correlation_id  the request/run this message belongs to (shows up in logs)
  type            event name (routing key) or command name
  headers         x-schema-version, x-retry (attempts so far) and error details in the DLQ
Messages are persistent. Schema changes must stay backward compatible.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

SCHEMA_VERSION = 1
H_SCHEMA = "x-schema-version"
H_RETRY = "x-retry"


class InvalidMessage(Exception):
    """The message cannot be decoded; it is dead-lettered as a poison message."""


@dataclass
class Message:
    type: str
    payload: Any
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str | None = None
    schema_version: int = SCHEMA_VERSION
    headers: dict[str, Any] = field(default_factory=dict)

    @property
    def attempt(self) -> int:
        """How many times processing already failed and was retried (0 = first delivery)."""
        return int(self.headers.get(H_RETRY, 0) or 0)

    def to_amqp(self, extra_headers: dict[str, Any] | None = None) -> aio_pika.Message:
        headers = {**self.headers, H_SCHEMA: self.schema_version, **(extra_headers or {})}
        return aio_pika.Message(
            body=json.dumps(self.payload, separators=(",", ":"), default=str).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=self.message_id,
            correlation_id=self.correlation_id,
            type=self.type,
            headers=headers,
            timestamp=datetime.now(UTC),
        )

    @classmethod
    def from_incoming(cls, incoming: AbstractIncomingMessage) -> "Message":
        if not incoming.message_id:
            raise InvalidMessage("message_id is missing")
        try:
            uuid.UUID(incoming.message_id)
        except ValueError as exc:
            raise InvalidMessage("message_id is not a UUID") from exc
        try:
            payload = json.loads(incoming.body)
        except ValueError as exc:
            raise InvalidMessage("body is not valid JSON") from exc
        headers = dict(incoming.headers or {})
        return cls(
            type=incoming.type or "",
            payload=payload,
            message_id=incoming.message_id,
            correlation_id=incoming.correlation_id,
            schema_version=int(str(headers.get(H_SCHEMA, SCHEMA_VERSION))),
            headers=headers,
        )
