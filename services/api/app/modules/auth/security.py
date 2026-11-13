"""Password hashing, JWT access tokens and opaque refresh tokens."""

import asyncio
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.errors import Unauthorized

_hasher = PasswordHasher()  # Argon2id with the library's current recommended parameters
# A valid hash of a random password: verifying against it takes as long as a real check, so
# the response time does not reveal whether an email exists.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def _hash_sync(password: str) -> str:
    return _hasher.hash(password)


def _verify_sync(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# Argon2 is deliberately CPU- and memory-heavy: it runs in a worker thread so that the
# event loop keeps serving other requests (no blocking CPU work inside coroutines).
async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hash_sync, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    ok = await asyncio.to_thread(_verify_sync, password_hash or _DUMMY_HASH, password)
    # Unknown email: the dummy hash was still verified (same timing), but never counts as success.
    return ok and password_hash is not None


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@dataclass(frozen=True)
class AccessClaims:
    admin_id: uuid.UUID
    role: str


def issue_access_token(admin_id: uuid.UUID, role: str, secret: str, ttl: int) -> str:
    now = int(time.time())
    payload = {
        "sub": str(admin_id),
        "role": role,
        "typ": "access",
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str, secret: str) -> AccessClaims:
    try:
        # algorithms is pinned: accepting "none" or an asymmetric alias would be a classic hole.
        data = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "sub", "typ"]},
        )
        if data["typ"] != "access":
            raise Unauthorized("Invalid token")
        return AccessClaims(admin_id=uuid.UUID(data["sub"]), role=str(data.get("role", "")))
    except (jwt.PyJWTError, ValueError) as exc:
        raise Unauthorized("Invalid or expired token") from exc


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    # SHA-256 is enough here: the token is 256 bits of randomness, not a human password.
    return hashlib.sha256(token.encode()).hexdigest()
