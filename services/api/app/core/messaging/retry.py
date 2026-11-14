"""What to do when a handler fails: a pure function, easy to test."""

from dataclasses import dataclass

from app.core.messaging.envelope import InvalidMessage
from app.core.messaging.errors import PermanentError, TemporaryError


@dataclass(frozen=True)
class Decision:
    action: str  # "retry" | "dead_letter"
    reason: str
    next_attempt: int = 0  # for "retry": 1-based number of the retry queue to use


def decide(exc: BaseException, attempt: int, max_retries: int) -> Decision:
    """`attempt` is how many retries already happened; `max_retries` is the retry queue count."""
    if isinstance(exc, TemporaryError):
        if attempt < max_retries:
            return Decision("retry", "temporary", attempt + 1)
        return Decision("dead_letter", "retries_exhausted")
    if isinstance(exc, PermanentError):
        return Decision("dead_letter", "permanent")
    if isinstance(exc, InvalidMessage):
        return Decision("dead_letter", "invalid_message")
    # An exception nobody planned for is treated as permanent: blindly retrying a bug just
    # repeats the bug. The reason is recorded so an operator can look at it.
    return Decision("dead_letter", "unexpected")
