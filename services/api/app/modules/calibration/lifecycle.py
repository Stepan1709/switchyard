"""State changes of a run that end it. Shared by the API (cancel) and the worker (completion,
budget exhausted), so the event `calibration.run.finished` is written the same way everywhere.

The event goes through the outbox: it is saved in the same transaction as the status change,
so it exists if and only if the run really changed state.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import CALIBRATION_RUN_FINISHED
from app.core.outbox import enqueue
from app.modules.calibration.models import FINISHED_STATUSES
from app.modules.calibration.repository import CalibrationRepository

ACTIVE = ("running", "paused")


async def finish_run(
    session: AsyncSession,
    repo: CalibrationRepository,
    run_id: uuid.UUID,
    to_status: str,
    *,
    reason: str | None = None,
    from_statuses: tuple[str, ...] = ACTIVE,
) -> bool:
    """Move a run to an end state and announce it. True if this call did it (compare-and-set:
    when two workers finish the same run, only one of them gets True)."""
    assert to_status in FINISHED_STATUSES
    if not await repo.transition(run_id, from_statuses, to_status, reason=reason, finished=True):
        return False
    run = await repo.get_run(run_id)
    assert run is not None
    spent, calls = await repo.spending(run_id)
    enqueue(
        session,
        CALIBRATION_RUN_FINISHED,
        {
            "run_id": str(run_id),
            "project_id": str(run.project_id),
            "task_type": run.task_type,
            "status": to_status,
            "reason": reason,
            "total_jobs": run.total_jobs,
            "results": await repo.count_results(run_id),
            "spent_usd": str(spent),
            "calls": calls,
        },
        correlation_id=str(run_id),
    )
    return True


async def complete_if_done(
    session: AsyncSession, repo: CalibrationRepository, run_id: uuid.UUID
) -> bool:
    """Complete the run when every job has a result. Safe to call at any time and by anyone.

    The run row is locked first. Without the lock two workers that store the last two results
    at the same moment could each count only their own (the other transaction is not committed
    yet), both conclude "one is still missing", and the run would never complete. With the lock
    the second one waits for the first to commit, then counts again and sees both."""
    run = await repo.get_run(run_id, lock=True)
    if run is None or run.status != "running":
        return False
    if await repo.count_results(run_id) < run.total_jobs:
        return False
    return await finish_run(session, repo, run_id, "completed", from_statuses=("running",))
