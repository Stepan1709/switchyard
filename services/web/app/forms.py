"""Small helpers for HTML forms (everything arrives as text)."""

from decimal import Decimal, InvalidOperation

from app.api_client import ApiError


def checked(value: str | None) -> bool:
    """A checkbox is sent only when ticked; the toggle buttons send true/false."""
    return (value or "").strip().lower() in {"1", "true", "on", "yes"}


def optional_text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def optional_int(value: str | None) -> int | None:
    text = optional_text(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ApiError(422, "Not a number", f"'{text}' is not a whole number.") from exc


def number(value: str | None) -> str:
    """A decimal as text (the api reads it exactly, without float rounding)."""
    text = (value or "").strip().replace(",", ".")
    try:
        Decimal(text)
    except InvalidOperation as exc:
        raise ApiError(422, "Not a number", f"'{text}' is not a number.") from exc
    return text
