"""Publishing with broker confirmation.

A publish returns only after the broker has accepted the message (publisher confirm). With
`mandatory=True` a message that no queue can receive is returned and reported as an error
instead of being silently dropped.
"""

from typing import Any

from aio_pika.abc import AbstractChannel, AbstractExchange
from aio_pika.exceptions import AMQPError, DeliveryError
from aiormq.abc import DeliveredMessage
from pamqp import commands as amqp_commands

from app.core.messaging.envelope import Message
from app.core.messaging.errors import PublishError, UnroutableError
from app.core.messaging.topology import Exchanges, declare_exchanges


class Publisher:
    def __init__(self, channel: AbstractChannel, exchanges: Exchanges) -> None:
        self._channel = channel
        self._exchanges = exchanges

    @classmethod
    async def create(cls, channel: AbstractChannel) -> "Publisher":
        return cls(channel, await declare_exchanges(channel))

    async def _publish(
        self,
        exchange: AbstractExchange,
        routing_key: str,
        message: Message,
        extra_headers: dict[str, Any] | None,
        confirm_timeout: float,
    ) -> None:
        try:
            result = await exchange.publish(
                message.to_amqp(extra_headers), routing_key, mandatory=True, timeout=confirm_timeout
            )
        except DeliveryError as exc:
            # The broker refused the message or no queue matched the routing key.
            raise PublishError(f"message {message.message_id} was not delivered: {exc}") from exc
        except (AMQPError, TimeoutError, OSError) as exc:
            raise PublishError(f"publish of {message.message_id} failed: {exc!r}") from exc
        # aio-pika does not raise for a returned (unroutable) mandatory message: the confirm
        # result is then a Basic.Return instead of a Basic.Ack. Without this check such a
        # message would look delivered.
        if isinstance(result, DeliveredMessage) and isinstance(
            result.delivery, amqp_commands.Basic.Return
        ):
            raise UnroutableError(
                f"message {message.message_id} was returned: no queue is bound to {routing_key!r}"
            )

    async def publish_event(self, message: Message, *, confirm_timeout: float = 10.0) -> None:
        """Publish to the events exchange; the routing key is the message type."""
        await self._publish(self._exchanges.events, message.type, message, None, confirm_timeout)

    async def publish_dlx(
        self,
        routing_key: str,
        message: Message,
        *,
        extra_headers: dict[str, Any] | None = None,
        confirm_timeout: float = 10.0,
    ) -> None:
        """Publish to the DLX: used for retries, dead letters and returning messages."""
        await self._publish(
            self._exchanges.dlx, routing_key, message, extra_headers, confirm_timeout
        )
