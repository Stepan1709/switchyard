"""Gauges the worker refreshes on a timer: queue depths, the outbox and calibration progress.

These are state, not events, so they are read from the broker and the database every few
seconds instead of being counted on the way. A scrape sees the value of the last refresh.
"""

from collections.abc import Iterable

from aio_pika.abc import AbstractRobustConnection
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import metrics
from app.core.logging import get_logger
from app.core.messaging.topology import QueueNames

log = get_logger(__name__)

RUN_STATUSES = ("running", "paused", "completed", "cancelled", "budget_exceeded")


def queue_names(work_queues: Iterable[str], retry_stages: int) -> list[str]:
    """Every queue of every work queue: itself, its retry stages and its dead-letter queue."""
    names: list[str] = []
    for work in work_queues:
        q = QueueNames(work)
        names.append(work)
        names.extend(q.retry(n) for n in range(1, retry_stages + 1))
        names.append(q.dlq)
    return names


async def collect_queue_depths(connection: AbstractRobustConnection, names: list[str]) -> None:
    """Passive declare: asks the broker for the counts and creates nothing. A queue that does
    not exist closes the channel, so each one gets its own."""
    for name in names:
        try:
            channel = await connection.channel()
        except Exception as exc:
            log.warning("queue_metrics_unavailable", error=type(exc).__name__)
            return
        try:
            queue = await channel.declare_queue(name, passive=True)
            result = queue.declaration_result
            metrics.QUEUE_MESSAGES.labels(name).set(result.message_count or 0)
            metrics.QUEUE_CONSUMERS.labels(name).set(result.consumer_count or 0)
        except Exception as exc:  # not declared yet, or the broker is busy: skip this round
            log.debug("queue_metrics_skipped", queue=name, error=type(exc).__name__)
        finally:
            try:
                await channel.close()
            except Exception:  # the channel may already be closed by the broker
                log.debug("queue_metrics_channel_close_failed", queue=name)


async def collect_database_gauges(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        pending, oldest = (
            await session.execute(
                text(
                    "SELECT count(*), coalesce(extract(epoch FROM now() - min(created_at)), 0)"
                    " FROM outbox WHERE sent_at IS NULL"
                )
            )
        ).one()
        metrics.OUTBOX_PENDING.set(pending)
        metrics.OUTBOX_OLDEST.set(float(oldest))

        rows = (
            await session.execute(text("SELECT status, count(*) FROM calibration_runs GROUP BY 1"))
        ).all()
        counts: dict[str, int] = {str(r[0]): int(r[1]) for r in rows}
        for status in RUN_STATUSES:
            metrics.CALIBRATION_RUNS.labels(status).set(counts.get(status, 0))

        running = (
            await session.execute(
                text(
                    "SELECT r.id::text, r.task_type, r.total_jobs,"
                    " (SELECT count(*) FROM run_results x WHERE x.run_id = r.id) AS done"
                    " FROM calibration_runs r WHERE r.status IN ('running', 'paused')"
                )
            )
        ).all()
        # Only live runs have a series, and a finished run's series disappears with the clear.
        metrics.CALIBRATION_PROGRESS.clear()
        for run_id, task_type, total, done in running:
            if total:
                metrics.CALIBRATION_PROGRESS.labels(run_id, task_type).set(min(done / total, 1.0))
