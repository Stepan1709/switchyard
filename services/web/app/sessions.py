"""Server-side sessions (BFF): the browser holds only a random id, tokens stay in Redis.

The cookie value is never stored as is: the Redis key is its SHA-256, so a dump of Redis does
not give anybody a working cookie. Every request of a logged-in user prolongs the session
(sliding expiry); a visitor who has not logged in yet gets a short-lived anonymous session
that only carries the CSRF token of the login form.
"""

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis

from app.config import Settings

PREFIX = "web:session:"
LOCK_PREFIX = "web:refresh-lock:"


def new_id() -> str:
    return secrets.token_urlsafe(32)


def _key(session_id: str) -> str:
    return PREFIX + hashlib.sha256(session_id.encode()).hexdigest()


@dataclass
class Session:
    id: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def authenticated(self) -> bool:
        return bool(self.data.get("access_token"))

    @property
    def csrf(self) -> str:
        return str(self.data.get("csrf", ""))

    @property
    def email(self) -> str:
        return str(self.data.get("email", ""))

    @property
    def role(self) -> str:
        return str(self.data.get("role", "viewer"))

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def project_id(self) -> str | None:
        value = self.data.get("project_id")
        return str(value) if value else None


class SessionStore:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    async def _write(self, session: Session, ttl: int) -> None:
        await self.redis.set(_key(session.id), json.dumps(session.data), ex=ttl)

    async def create_anonymous(self) -> Session:
        session = Session(new_id(), {"csrf": secrets.token_urlsafe(24)})
        await self._write(session, self.settings.anonymous_ttl_seconds)
        return session

    async def create(self, tokens: dict[str, Any], me: dict[str, Any]) -> Session:
        """A new id for every login (so an id planted before the login is worthless)."""
        session = Session(
            new_id(),
            {
                "csrf": secrets.token_urlsafe(24),
                "email": me["email"],
                "role": me["role"],
                **self._token_fields(tokens),
            },
        )
        await self._write(session, self.settings.session_ttl_seconds)
        return session

    @staticmethod
    def _token_fields(tokens: dict[str, Any]) -> dict[str, Any]:
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "access_expires_at": time.time() + int(tokens["expires_in"]),
        }

    async def load(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        raw = await self.redis.get(_key(session_id))
        if raw is None:
            return None
        session = Session(session_id, json.loads(raw))
        if session.authenticated:
            await self.redis.expire(_key(session_id), self.settings.session_ttl_seconds)
        return session

    async def save(self, session: Session) -> None:
        """Write the data back without changing how long the session lives."""
        await self.redis.set(_key(session.id), json.dumps(session.data), keepttl=True)

    async def set_tokens(self, session: Session, tokens: dict[str, Any]) -> None:
        session.data.update(self._token_fields(tokens))
        await self.save(session)

    async def destroy(self, session_id: str | None) -> None:
        if session_id:
            await self.redis.delete(_key(session_id))

    # The api rotates refresh tokens and treats a reused one as theft, so two parallel page
    # requests must not both refresh: one does, the other waits and takes the new tokens.
    async def acquire_refresh(self, session_id: str) -> bool:
        key = LOCK_PREFIX + hashlib.sha256(session_id.encode()).hexdigest()
        return bool(await self.redis.set(key, "1", nx=True, ex=15))

    async def release_refresh(self, session_id: str) -> None:
        await self.redis.delete(LOCK_PREFIX + hashlib.sha256(session_id.encode()).hexdigest())
