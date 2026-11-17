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
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import metrics
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
    # Prices of the moment (decimal strings), so the gateway can cost a request without
    # touching the database. A server paid by the month has none: its cost per request is 0.
    cost_type: str = "per_token"
    price_input_per_1m: str | None = None
    price_output_per_1m: str | None = None


@dataclass(frozen=True)
class TaskRoute:
    """What the gateway does for one (project, task type) that is not plain `record`."""

    mode: str  # shadow | route
    chain: tuple[RouteTarget, ...] = ()  # route: [primary, fallback, ...] (usable models only)
    rule_version: int | None = None
    shadow: tuple[RouteTarget, ...] = ()  # shadow: models that get a copy
    shadow_rate: float = 0.0
    # The schema a non-streamed answer must fit, from the first required json_schema check of
    # the task type: an answer that does not fit makes the gateway try the next model.
    validation_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class KeyInfo:
    key_id: str
    prefix: str
    secret_hash: str
    project_id: str
    default_task_type: str | None
    rate_limit_rpm: int | None
    monthly_budget_usd: str | None = None  # decimal string; None = no budget


@dataclass(frozen=True)
class ProjectInfo:
    project_id: str
    name: str
    reference: RouteTarget | None  # None until an administrator sets a usable reference model
    store_bodies: bool = True  # keep prompts and answers in the traffic log
    mask_pii: bool = False  # mask e-mails, phones and cards in what is kept


@dataclass(frozen=True)
class Snapshot:
    version: str
    created_at: str
    keys: Mapping[str, KeyInfo]  # by key prefix
    projects: Mapping[str, ProjectInfo]  # by project id
    model_names: tuple[str, ...]  # enabled models, for GET /v1/models
    # Every usable model by id, and the id behind each name (a name shared by two providers
    # resolves to the first provider by name), so a pinned `model` can be found without the
    # database.
    targets: Mapping[str, RouteTarget] = field(default_factory=dict)
    target_ids_by_name: Mapping[str, str] = field(default_factory=dict)
    # "project_id|task_type" -> route. Task types in plain record mode are absent.
    routes: Mapping[str, TaskRoute] = field(default_factory=dict)

    def route_of(self, project_id: str, task_type: str | None) -> TaskRoute | None:
        return self.routes.get(f"{project_id}|{task_type}") if task_type else None

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
                    store_bodies=p.get("store_bodies", True),
                    mask_pii=p.get("mask_pii", False),
                )
                for pid, p in data["projects"].items()
            },
            model_names=tuple(data["model_names"]),
            targets={i: RouteTarget(**t) for i, t in data.get("targets", {}).items()},
            target_ids_by_name=dict(data.get("target_ids_by_name", {})),
            routes={
                k: TaskRoute(
                    mode=r["mode"],
                    chain=tuple(RouteTarget(**t) for t in r["chain"]),
                    rule_version=r["rule_version"],
                    shadow=tuple(RouteTarget(**t) for t in r["shadow"]),
                    shadow_rate=r["shadow_rate"],
                    validation_schema=r["validation_schema"],
                )
                for k, r in data.get("routes", {}).items()
            },
        )


def _version_of(content: dict[str, Any]) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


_KEYS_SQL = text(
    """
    SELECT k.id, k.prefix, k.secret_hash, k.project_id, k.default_task_type, k.rate_limit_rpm,
           k.monthly_budget_usd,
           p.name AS project_name, p.reference_model_id, p.store_bodies, p.mask_pii
    FROM api_keys k JOIN projects p ON p.id = k.project_id
    WHERE k.revoked_at IS NULL
    """
)
_MODELS_SQL = text(
    """
    SELECT m.id, m.name, m.context_window, m.enabled AS model_enabled, m.cost_type,
           pp.input_per_1m, pp.output_per_1m,
           pr.id AS provider_id, pr.name AS provider_name, pr.base_url, pr.is_local,
           pr.auth_type, pr.api_key_encrypted, pr.enabled AS provider_enabled
    FROM models m JOIN providers pr ON pr.id = m.provider_id
    LEFT JOIN model_prices pp ON pp.model_id = m.id AND pp.valid_from <= now()
                              AND (pp.valid_to IS NULL OR pp.valid_to > now())
    """
)


_ROUTES_SQL = text(
    """
    SELECT tt.project_id, tt.name, tt.mode, tt.shadow_model_ids, tt.shadow_rate,
           r.version AS rule_version, r.chain
    FROM task_types tt
    LEFT JOIN routing_rules r
           ON r.project_id = tt.project_id AND r.task_type = tt.name AND r.status = 'active'
    WHERE tt.mode <> 'record'
    """
)
_SCHEMAS_SQL = text(
    """
    SELECT DISTINCT ON (project_id, task_type) project_id, task_type, config
    FROM task_checks WHERE kind = 'json_schema' AND required
    ORDER BY project_id, task_type, position, created_at, id
    """
)


async def build_snapshot(session: AsyncSession) -> Snapshot:
    """Read everything the gateway needs in two queries (one consistent view)."""
    key_rows = (await session.execute(_KEYS_SQL)).mappings().all()
    model_rows = (await session.execute(_MODELS_SQL)).mappings().all()
    route_rows = (await session.execute(_ROUTES_SQL)).mappings().all()
    schema_rows = (await session.execute(_SCHEMAS_SQL)).mappings().all()

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
            cost_type=m["cost_type"],
            price_input_per_1m=None if m["input_per_1m"] is None else str(m["input_per_1m"]),
            price_output_per_1m=None if m["output_per_1m"] is None else str(m["output_per_1m"]),
        )

    by_name: dict[str, str] = {}
    for m in sorted(model_rows, key=lambda r: (r["provider_name"], str(r["id"]))):
        if str(m["id"]) in targets:
            by_name.setdefault(m["name"], str(m["id"]))

    schemas = {
        f"{r['project_id']}|{r['task_type']}": r["config"].get("schema") for r in schema_rows
    }
    routes: dict[str, TaskRoute] = {}
    for r in route_rows:
        key = f"{r['project_id']}|{r['name']}"
        chain = tuple(targets[str(i)] for i in (r["chain"] or []) if str(i) in targets)
        shadow = tuple(targets[str(i)] for i in (r["shadow_model_ids"] or []) if str(i) in targets)
        routes[key] = TaskRoute(
            mode=r["mode"],
            chain=chain if r["mode"] == "route" else (),
            rule_version=r["rule_version"] if r["mode"] == "route" else None,
            shadow=shadow if r["mode"] == "shadow" else (),
            shadow_rate=float(r["shadow_rate"]) if r["mode"] == "shadow" else 0.0,
            validation_schema=schemas.get(key),
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
            monthly_budget_usd=(
                None if k["monthly_budget_usd"] is None else str(k["monthly_budget_usd"])
            ),
        )
        ref = k["reference_model_id"]
        projects[pid] = ProjectInfo(
            project_id=pid,
            name=k["project_name"],
            reference=targets.get(str(ref)) if ref else None,
            store_bodies=k["store_bodies"],
            mask_pii=k["mask_pii"],
        )

    model_names = tuple(sorted(names))
    # The version depends on the content only, so an unchanged database gives the same version
    # and the gateway can skip the swap and the write to Redis.
    version = _version_of(
        {
            "keys": {p: asdict(v) for p, v in keys.items()},
            "projects": {p: asdict(v) for p, v in projects.items()},
            "models": model_names,
            "targets": {i: asdict(t) for i, t in targets.items()},
            "routes": {k: asdict(v) for k, v in routes.items()},
        }
    )
    return Snapshot(
        version=version,
        created_at=datetime.now(UTC).isoformat(),
        keys=keys,
        projects=projects,
        model_names=model_names,
        targets=targets,
        target_ids_by_name=by_name,
        routes=routes,
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
        self.confirmed_at = 0.0  # monotonic time of the last successful refresh or load

    # ---- installing -----------------------------------------------------------------------
    def _install(self, snapshot: Snapshot, source: str) -> None:
        keys: dict[str, str | None] = {}
        usable = list(snapshot.targets.values())
        usable += [p.reference for p in snapshot.projects.values() if p.reference is not None]
        for ref in usable:
            if ref.provider_id in keys:
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
        self.confirmed_at = time.monotonic()
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
            metrics.SNAPSHOT_REFRESHES.labels("failed").inc()
            log.warning("snapshot_refresh_failed", error=type(exc).__name__)
            if self.current is None:
                return await self._load_from_redis()
            return False
        self.last_refresh_ok = True
        self.refreshes += 1
        self.confirmed_at = time.monotonic()  # the database agrees: the snapshot is fresh
        metrics.SNAPSHOT_REFRESHES.labels("ok").inc()
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
