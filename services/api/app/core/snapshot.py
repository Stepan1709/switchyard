"""The gateway's in-memory snapshot of what it needs to serve requests.

The hot path must not touch the database. Instead the gateway holds an immutable snapshot:
client keys (prefix and hash), projects, and the reference model of each project with its
provider address and (encrypted) access key. A background task rebuilds it from PostgreSQL
every few seconds and swaps the reference atomically, so a request always sees one consistent
version and never waits for a lock.

Resilience
* the last good snapshot is copied to Redis; a gateway that starts while PostgreSQL is down
  loads that copy and keeps serving;
* if a refresh fails, the previous snapshot stays in use (stale data beats no service);
* the copy in Redis contains only ciphertext of provider keys. They are decrypted in the
  gateway's memory when a snapshot is installed, and never written anywhere.

The loader uses plain SQL on purpose: core must not import feature modules.
"""

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crypto import DecryptionError, KeyRing
from app.core.logging import get_logger

log = get_logger(__name__)

REDIS_KEY = "snapshot:current"


@dataclass(frozen=True)
class RouteTarget:
    """Where traffic of a project goes: one model of one provider."""

    model_id: str
    model_name: str  # the name the provider knows
    provider_id: str
    provider_name: str
    base_url: str
    is_local: bool
    auth_type: str
    api_key_encrypted: str | None
    context_window: int


@dataclass(frozen=True)
class KeyInfo:
    key_id: str
    prefix: str
    secret_hash: str
    project_id: str
    default_task_type: str | None
    rate_limit_rpm: int | None


@dataclass(frozen=True)
class ProjectInfo:
    project_id: str
    name: str
    reference: RouteTarget | None  # None until an administrator sets a usable reference model


@dataclass(frozen=True)
class Snapshot:
    version: str
    created_at: str
    keys: Mapping[str, KeyInfo]  # by key prefix
    projects: Mapping[str, ProjectInfo]  # by project id
    model_names: tuple[str, ...]  # enabled models, for GET /v1/models

    # ---- (de)serialisation for the Redis copy ---------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Snapshot":
        data = json.loads(raw)
        return cls(
            version=data["version"],
            created_at=data["created_at"],
            keys={p: KeyInfo(**k) for p, k in data["keys"].items()},
            projects={
                pid: ProjectInfo(
                    project_id=p["project_id"],
                    name=p["name"],
                    reference=RouteTarget(**p["reference"]) if p["reference"] else None,
                )
                for pid, p in data["projects"].items()
            },
            model_names=tuple(data["model_names"]),
        )


def _version_of(content: dict[str, Any]) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


_KEYS_SQL = text(
    """
    SELECT k.id, k.prefix, k.secret_hash, k.project_id, k.default_task_type, k.rate_limit_rpm,
           p.name AS project_name, p.reference_model_id
    FROM api_keys k JOIN projects p ON p.id = k.project_id
    WHERE k.revoked_at IS NULL
    """
)
_MODELS_SQL = text(
    """
    SELECT m.id, m.name, m.context_window, m.enabled AS model_enabled,
           pr.id AS provider_id, pr.name AS provider_name, pr.base_url, pr.is_local,
           pr.auth_type, pr.api_key_encrypted, pr.enabled AS provider_enabled
    FROM models m JOIN providers pr ON pr.id = m.provider_id
    """
)


async def build_snapshot(session: AsyncSession) -> Snapshot:
    """Read everything the gateway needs in two queries (one consistent view)."""
    key_rows = (await session.execute(_KEYS_SQL)).mappings().all()
    model_rows = (await session.execute(_MODELS_SQL)).mappings().all()

    targets: dict[str, RouteTarget] = {}
    names: set[str] = set()
    for m in model_rows:
        if not (m["model_enabled"] and m["provider_enabled"]):
            continue  # a disabled model or provider is not a usable target
        names.add(m["name"])
        targets[str(m["id"])] = RouteTarget(
            model_id=str(m["id"]),
            model_name=m["name"],
            provider_id=str(m["provider_id"]),
            provider_name=m["provider_name"],
            base_url=m["base_url"],
            is_local=m["is_local"],
            auth_type=m["auth_type"],
            api_key_encrypted=m["api_key_encrypted"],
            context_window=m["context_window"],
        )

    keys: dict[str, KeyInfo] = {}
    projects: dict[str, ProjectInfo] = {}
    for k in key_rows:
        pid = str(k["project_id"])
        keys[k["prefix"]] = KeyInfo(
            key_id=str(k["id"]),
            prefix=k["prefix"],
            secret_hash=k["secret_hash"],
            project_id=pid,
            default_task_type=k["default_task_type"],
            rate_limit_rpm=k["rate_limit_rpm"],
        )
        ref = k["reference_model_id"]
        projects[pid] = ProjectInfo(
            project_id=pid,
            name=k["project_name"],
            reference=targets.get(str(ref)) if ref else None,
        )

    model_names = tuple(sorted(names))
    # The version depends on the content only, so an unchanged database gives the same version
    # and the gateway can skip the swap and the write to Redis.
    version = _version_of(
        {
            "keys": {p: asdict(v) for p, v in keys.items()},
            "projects": {p: asdict(v) for p, v in projects.items()},
            "models": model_names,
        }
    )
    return Snapshot(
        version=version,
        created_at=datetime.now(UTC).isoformat(),
        keys=keys,
        projects=projects,
        model_names=model_names,
    )


@dataclass(frozen=True)
class LiveSnapshot:
    """A snapshot plus the decrypted provider keys (memory only, never serialised)."""

    snapshot: Snapshot
    provider_keys: Mapping[str, str | None] = field(default_factory=dict)


class SnapshotStore:
    """Holds the current snapshot and refreshes it. Reads are plain attribute access."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        keyring: KeyRing,
        *,
        refresh_seconds: float = 15.0,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._keyring = keyring
        self._refresh_seconds = refresh_seconds
        self._stop = asyncio.Event()
        self.current: LiveSnapshot | None = None
        self.source: str = "none"  # "database" | "redis"
        self.last_refresh_ok: bool = False
        self.refreshes = 0

    # ---- installing -----------------------------------------------------------------------
    def _install(self, snapshot: Snapshot, source: str) -> None:
        keys: dict[str, str | None] = {}
        for project in snapshot.projects.values():
            ref = project.reference
            if ref is None or ref.provider_id in keys:
                continue
            if ref.api_key_encrypted is None:
                keys[ref.provider_id] = None
                continue
            try:
                keys[ref.provider_id] = self._keyring.decrypt(
                    ref.api_key_encrypted, aad=ref.provider_id
                )
            except DecryptionError:
                # One provider with an unreadable key must not take the whole gateway down.
                log.error("provider_key_undecryptable", provider_id=ref.provider_id)
                keys[ref.provider_id] = None
        self.current = LiveSnapshot(snapshot, keys)  # the atomic swap
        self.source = source
        log.info(
            "snapshot_installed",
            version=snapshot.version,
            source=source,
            keys=len(snapshot.keys),
            projects=len(snapshot.projects),
        )

    # ---- loading --------------------------------------------------------------------------
    async def refresh(self) -> bool:
        """Rebuild from PostgreSQL. Keeps the old snapshot if that fails."""
        try:
            async with self._session_factory() as session:
                fresh = await build_snapshot(session)
        except Exception as exc:
            self.last_refresh_ok = False
            log.warning("snapshot_refresh_failed", error=type(exc).__name__)
            if self.current is None:
                return await self._load_from_redis()
            return False
        self.last_refresh_ok = True
        self.refreshes += 1
        if self.current is None or self.current.snapshot.version != fresh.version:
            self._install(fresh, "database")
            await self._save_to_redis(fresh)
        return True

    async def _save_to_redis(self, snapshot: Snapshot) -> None:
        try:
            await self._redis.set(REDIS_KEY, snapshot.to_json())
        except Exception as exc:  # Redis is a convenience copy, not the source of truth
            log.warning("snapshot_redis_save_failed", error=type(exc).__name__)

    async def _load_from_redis(self) -> bool:
        try:
            raw = await self._redis.get(REDIS_KEY)
            if raw is None:
                return False
            self._install(Snapshot.from_json(raw), "redis")
            return True
        except Exception as exc:
            log.warning("snapshot_redis_load_failed", error=type(exc).__name__)
            return False

    async def run(self) -> None:
        """Refresh periodically until stopped. The caller loads the first snapshot itself.

        While there is no snapshot at all, retries quickly instead of waiting a full period.
        """
        while not self._stop.is_set():
            wait = self._refresh_seconds if self.current is not None else 2.0
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), wait)
            if not self._stop.is_set():
                await self.refresh()

    def stop(self) -> None:
        self._stop.set()
