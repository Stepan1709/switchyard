"""Token estimates and the cost of one request. Pure functions.

Money uses Decimal, never float: prices are decimal numbers and sums of many small costs must
not drift.
"""

import json
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

MILLION = Decimal(1_000_000)
_CHARS_PER_TOKEN = 3.5  # a deliberately pessimistic English/mixed-text average


@dataclass(frozen=True)
class Price:
    """The price version that was valid at the moment of the call."""

    cost_type: str  # per_token | fixed_monthly
    input_per_1m: Decimal | None = None
    output_per_1m: Decimal | None = None


def cost_usd(price: Price, prompt_tokens: int, completion_tokens: int) -> Decimal:
    """USD for one request. A server paid by the month has no marginal cost per request: its
    effective price per request (monthly cost / requests served) is computed by the analytics."""
    if price.cost_type != "per_token" or price.input_per_1m is None or price.output_per_1m is None:
        return Decimal(0)
    raw = (prompt_tokens * price.input_per_1m + completion_tokens * price.output_per_1m) / MILLION
    return raw.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def _chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def estimate_prompt_tokens(request: dict[str, Any]) -> int:
    """Rough size of the prompt: messages, tool definitions and response format together."""
    chars = _chars(request.get("messages")) + _chars(request.get("tools"))
    chars += _chars(request.get("response_format"))
    return max(1, math.ceil(chars / _CHARS_PER_TOKEN))


def estimate_text_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN)) if text else 0


def requested_max_tokens(request: dict[str, Any]) -> int | None:
    for key in ("max_completion_tokens", "max_tokens"):
        value = request.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def estimate_completion_tokens(
    request: dict[str, Any], reference_tokens: int | None, default_max: int
) -> int:
    """What the answer will probably cost, used to reserve budget before the call.

    The recorded reference answer is the best guide (a candidate rarely writes much more than
    twice as much); the request's own max_tokens is a hard ceiling."""
    ceiling = requested_max_tokens(request) or default_max
    guess = max(2 * reference_tokens, 64) if reference_tokens else ceiling
    return min(guess, ceiling)


def to_micro(amount: Decimal) -> int:
    """USD -> integer millionths of a dollar (what Redis counters hold), rounded up."""
    return int((amount * MILLION).to_integral_value(rounding="ROUND_CEILING"))
