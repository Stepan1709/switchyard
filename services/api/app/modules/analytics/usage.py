"""Recomputing the daily summaries (`usage_daily`, `utilization_daily`) from the logs.

Both are upserts that read the log for a range of days: computing a day twice gives the same
rows, and a day whose log has already been dropped (retention removes whole months) produces
no rows, so what was stored before stays. Nothing here ever deletes.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.analytics.calc import union_length
from app.modules.analytics.models import LATENCY_BOUNDS_MS, NIL_UUID

_HIST = ", ".join(
    f"'le_{b}', count(*) FILTER (WHERE duration_ms <= {b})" for b in LATENCY_BOUNDS_MS
)

_USAGE_SQL = text(
    f"""
    INSERT INTO usage_daily (
        day, project_id, model_id, task_type, requests, ok, errors, prompt_tokens,
        completion_tokens, cost_usd, reference_cost_usd, duration_sum_ms, latency_hist)
    SELECT (started_at AT TIME ZONE 'UTC')::date AS day,
           project_id,
           coalesce(model_id, CAST(:nil AS uuid)),
           coalesce(task_type, ''),
           count(*),
           count(*) FILTER (WHERE status = 'ok'),
           count(*) FILTER (WHERE status IN ('error', 'upstream_error', 'timeout')),
           coalesce(sum(prompt_tokens), 0),
           coalesce(sum(completion_tokens), 0),
           coalesce(sum(cost_usd), 0),
           coalesce(sum(reference_cost_usd), 0),
           coalesce(sum(duration_ms), 0),
           jsonb_build_object({_HIST}, 'total', count(*) FILTER (WHERE duration_ms IS NOT NULL))
    FROM requests
    WHERE started_at >= :start AND started_at < :end
    GROUP BY 1, 2, 3, 4
    ON CONFLICT (day, project_id, model_id, task_type) DO UPDATE SET
        requests = EXCLUDED.requests, ok = EXCLUDED.ok, errors = EXCLUDED.errors,
        prompt_tokens = EXCLUDED.prompt_tokens, completion_tokens = EXCLUDED.completion_tokens,
        cost_usd = EXCLUDED.cost_usd, reference_cost_usd = EXCLUDED.reference_cost_usd,
        duration_sum_ms = EXCLUDED.duration_sum_ms, latency_hist = EXCLUDED.latency_hist,
        updated_at = now()
    """
)

# The intervals in which each model was handling a request, by source. The end of a replay or of
# a shadow copy is the moment its result was stored.
_INTERVALS = {
    "live": """
        SELECT model_id, extract(epoch FROM started_at) AS s,
               extract(epoch FROM started_at) + duration_ms / 1000.0 AS e
        FROM requests
        WHERE started_at >= :start AND started_at < :end AND model_id IS NOT NULL
          AND duration_ms IS NOT NULL""",
    "calibration": """
        SELECT model_id, extract(epoch FROM created_at) - latency_ms / 1000.0 AS s,
               extract(epoch FROM created_at) AS e
        FROM run_results
        WHERE created_at >= :start AND created_at < :end AND latency_ms IS NOT NULL""",
    "shadow": """
        SELECT model_id, extract(epoch FROM created_at) - latency_ms / 1000.0 AS s,
               extract(epoch FROM created_at) AS e
        FROM shadow_results
        WHERE created_at >= :start AND created_at < :end AND latency_ms IS NOT NULL""",
}

_UTIL_UPSERT = text(
    """
    INSERT INTO utilization_daily (day, model_id, source, busy_seconds, requests)
    VALUES (:day, :model_id, :source, :busy, :requests)
    ON CONFLICT (day, model_id, source) DO UPDATE SET
        busy_seconds = EXCLUDED.busy_seconds, requests = EXCLUDED.requests, updated_at = now()
    """
)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


async def recompute_usage(session: AsyncSession, first: date, last: date) -> None:
    """Rebuild the summaries of the days first..last (inclusive)."""
    start, _ = day_bounds(first)
    _, end = day_bounds(last)
    await session.execute(_USAGE_SQL, {"nil": NIL_UUID, "start": start, "end": end})


async def recompute_utilization(session: AsyncSession, first: date, last: date) -> None:
    """Rebuild the busy time per day, model and source (and the union of the sources)."""
    day = first
    while day <= last:
        start, end = day_bounds(day)
        lo, hi = start.timestamp(), end.timestamp()
        per_model: dict[Any, dict[str, list[tuple[float, float]]]] = {}
        for source, sql in _INTERVALS.items():
            rows = await session.execute(text(sql), {"start": start, "end": end})
            for r in rows:
                # extract(epoch ...) arrives as a Decimal: the arithmetic below is in floats
                per_model.setdefault(r.model_id, {}).setdefault(source, []).append(
                    (float(r.s), float(r.e))
                )
        for model_id, by_source in per_model.items():
            everything: list[tuple[float, float]] = []
            for source, intervals in by_source.items():
                everything += intervals
                await session.execute(
                    _UTIL_UPSERT,
                    {"day": day, "model_id": model_id, "source": source,
                     "busy": min(union_length(intervals, lo, hi), 86400.0),
                     "requests": len(intervals)},
                )  # fmt: skip
            await session.execute(
                _UTIL_UPSERT,
                {"day": day, "model_id": model_id, "source": "all",
                 "busy": min(union_length(everything, lo, hi), 86400.0),
                 "requests": len(everything)},
            )  # fmt: skip
        day += timedelta(days=1)


async def recompute(
    session_factory: async_sessionmaker[AsyncSession], first: date, last: date
) -> None:
    """Both summaries for the days first..last, in one transaction."""
    async with session_factory() as session, session.begin():
        await recompute_usage(session, first, last)
        await recompute_utilization(session, first, last)


async def recompute_recent(
    session_factory: async_sessionmaker[AsyncSession], days: int, now: datetime | None = None
) -> None:
    """The scheduled job: today and the `days - 1` days before it."""
    today = (now or datetime.now(UTC)).date()
    await recompute(session_factory, today - timedelta(days=max(days, 1) - 1), today)
