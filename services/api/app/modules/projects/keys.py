"""API key format, hashing and verification. Pure functions, no I/O."""

import hashlib
import hmac
import re
import secrets
import string

PREFIX_LENGTH = 8
SECRET_LENGTH = 32
# Letters and digits only: the underscore is the field separator, so urlsafe base64
# (which contains "_" and "-") would make parsing ambiguous.
_ALPHABET = string.ascii_letters + string.digits
_KEY_RE = re.compile(rf"swy_([A-Za-z0-9]{{{PREFIX_LENGTH}}})_([A-Za-z0-9]{{{SECRET_LENGTH}}})")


def generate_key() -> tuple[str, str, str]:
    """Returns (full key, prefix, secret). The full key is shown to the user exactly once."""
    prefix = "".join(secrets.choice(_ALPHABET) for _ in range(PREFIX_LENGTH))
    secret = "".join(secrets.choice(_ALPHABET) for _ in range(SECRET_LENGTH))
    return f"swy_{prefix}_{secret}", prefix, secret


def parse_key(raw: str) -> tuple[str, str] | None:
    # fullmatch, not match with `$`: `$` would also accept a trailing newline.
    match = _KEY_RE.fullmatch(raw)
    return (match.group(1), match.group(2)) if match else None


def hash_secret(secret: str, pepper: str) -> str:
    # HMAC with a server-side pepper: a leaked table alone cannot be used to test guesses.
    # A fast hash is fine because the secret has ~190 bits of randomness (unlike a password).
    return hmac.new(pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()


def verify_secret(secret: str, stored_hash: str, pepper: str) -> bool:
    # compare_digest: comparison time does not depend on where the first difference is.
    return hmac.compare_digest(hash_secret(secret, pepper), stored_hash)
