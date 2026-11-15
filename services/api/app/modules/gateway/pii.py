"""Masking personal data in stored traffic: e-mails, phone numbers, bank card numbers.

Used only for the copy that goes to the traffic log; the client always gets the real answer.
It is a safety net, not a guarantee: patterns find the common formats, they cannot recognise
names or addresses. Cards are recognised by length AND the Luhn checksum, so ordinary long
numbers (order ids, timestamps) are left alone.

The work is CPU-bound and can be large, so callers run it outside the event loop.
"""

import re
from typing import Any

EMAIL = "[EMAIL]"
PHONE = "[PHONE]"
CARD = "[CARD]"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
# A run of digits that may be separated by single spaces or dashes: candidates for card numbers.
_CARD_CANDIDATE_RE = re.compile(r"(?<!\d)\d(?:[ \-]?\d){12,18}(?!\d)")
# A phone-like run: optional +, then digits with spaces, dots, dashes and brackets in between.
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\w.])[+(]?\d[\d\s().\-]{7,}\d(?![\w])")
_DATE_START_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def luhn_valid(digits: str) -> bool:
    """The Luhn checksum used by payment card numbers."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _mask_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group())
    if 13 <= len(digits) <= 19 and luhn_valid(digits):
        return CARD
    return match.group()


def _mask_phone(match: re.Match[str]) -> str:
    text = match.group()
    digits = re.sub(r"\D", "", text)
    # 10-15 digits is the range of real phone numbers (E.164 allows up to 15). Shorter runs
    # are usually amounts, dates or ids.
    if not 10 <= len(digits) <= 15:
        return text
    if _DATE_START_RE.match(text):  # "2026-11-15 12:30" must not be read as a phone number
        return text
    # A long run of bare digits is more likely a timestamp or an id than a phone number
    # (1731700000000 is a millisecond timestamp). Without a "+" or separators, only
    # 10-12 digits count as a phone number.
    written_like_a_phone = text.startswith("+") or bool(re.search(r"[\s().\-]", text))
    return PHONE if written_like_a_phone or len(digits) <= 12 else text


def mask_text(text: str) -> str:
    if not text:
        return text
    masked = _EMAIL_RE.sub(EMAIL, text)
    masked = _CARD_CANDIDATE_RE.sub(_mask_card, masked)  # cards before phones: they overlap
    return _PHONE_CANDIDATE_RE.sub(_mask_phone, masked)


def mask_json(value: Any) -> Any:
    """A copy of a JSON-like structure with every string value masked (keys stay as they are)."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, list):
        return [mask_json(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_json(v) for k, v in value.items()}
    return value
