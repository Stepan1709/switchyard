"""Jinja filters: money, percentages, durations and dates as people read them."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

DASH = "\u2013"  # an en dash: no value


def _number(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def money(value: Any, digits: int | None = None) -> str:
    """$1,234.50. Small amounts get more digits: a request costs fractions of a cent."""
    n = _number(value)
    if n is None:
        return DASH
    if digits is None:
        a = abs(n)
        digits = 2 if a >= 100 else 4 if a >= 0.01 or a == 0 else 6
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.{digits}f}"


def pct(value: Any, digits: int = 1) -> str:
    """A share (0..1) as a percentage."""
    n = _number(value)
    return DASH if n is None else f"{n * 100:.{digits}f}%"


def pct_points(value: Any, digits: int = 1) -> str:
    """A number that already is a percentage."""
    n = _number(value)
    return DASH if n is None else f"{n:.{digits}f}%"


def ms(value: Any) -> str:
    n = _number(value)
    if n is None:
        return DASH
    return f"{n / 1000:.1f} s" if n >= 10_000 else f"{n:,.0f} ms"


def num(value: Any) -> str:
    n = _number(value)
    return DASH if n is None else f"{n:,.0f}"


def decimal(value: Any, digits: int = 2) -> str:
    n = _number(value)
    return DASH if n is None else f"{n:,.{digits}f}"


def dt(value: Any) -> str:
    if not value:
        return DASH
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return parsed.strftime("%Y-%m-%d %H:%M")


def short(value: Any, n: int = 8) -> str:
    return DASH if not value else str(value)[:n]


FILTERS = {
    "money": money,
    "pct": pct,
    "pct_points": pct_points,
    "ms": ms,
    "num": num,
    "decimal": decimal,
    "dt": dt,
    "short": short,
}
