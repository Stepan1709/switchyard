"""Login throttling on Redis: failed attempts per email and per client IP (fixed window)."""

from redis.asyncio import Redis

# Increment and set the expiry in one atomic step. With two separate commands a crash between
# INCR and EXPIRE would leave a counter that never expires and locks the user out forever.
_INCR_WITH_TTL = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""


class LoginLimiter:
    def __init__(
        self, redis: Redis, *, max_per_email: int, max_per_ip: int, window_seconds: int
    ) -> None:
        self._redis = redis
        self._max_email = max_per_email
        self._max_ip = max_per_ip
        self._window = window_seconds
        self._incr = redis.register_script(_INCR_WITH_TTL)

    @staticmethod
    def _keys(email: str, ip: str) -> tuple[str, str]:
        return f"login_fail:email:{email}", f"login_fail:ip:{ip}"

    async def retry_after(self, email: str, ip: str) -> int:
        """Seconds until the next attempt is allowed; 0 when the caller may try."""
        email_key, ip_key = self._keys(email, ip)
        wait = 0
        for key, limit in ((email_key, self._max_email), (ip_key, self._max_ip)):
            count = await self._redis.get(key)
            if count is not None and int(count) >= limit:
                wait = max(wait, max(await self._redis.ttl(key), 1))
        return wait

    async def register_failure(self, email: str, ip: str) -> None:
        for key in self._keys(email, ip):
            await self._incr(keys=[key], args=[self._window])

    async def reset(self, email: str) -> None:
        # Only the email counter: the IP counter keeps counting other people's failures too.
        await self._redis.delete(self._keys(email, "")[0])
