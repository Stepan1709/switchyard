"""Errors that handlers raise to tell the consumer what to do with a message."""


class TemporaryError(Exception):
    """Something that may succeed later (timeout, 429/5xx from a provider, a busy resource).

    The message is retried with a growing delay and goes to the DLQ when retries run out.
    """


class PermanentError(Exception):
    """A message that can never succeed (bad payload, violated business rule).

    It goes to the dead-letter queue immediately: retrying would only waste time.
    """


class PublishError(Exception):
    """The broker did not confirm the message (it is down, slow or refused it)."""


class UnroutableError(PublishError):
    """The broker accepted the message but no queue is bound to its routing key.

    With a mandatory publish the broker returns such a message instead of dropping it silently.
    It is not an outage: nobody is subscribed (yet).
    """
