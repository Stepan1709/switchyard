"""The queue consumers the worker runs. Each is a ConsumerSpec(queue, routing keys, handler)."""

from app.core.config import Settings
from app.core.crypto import KeyRing
from app.core.events import CALIBRATION_RUN_FINISHED, REPLAY_EXECUTE, REQUEST_COMPLETED
from app.core.logging import get_logger
from app.core.messaging import ConsumerSpec, Message
from app.core.resources import Resources
from app.modules.calibration.executor import ReplayExecutor
from app.modules.traffic.writer import RequestLogWriter

log = get_logger(__name__)


def request_log_spec(writer: RequestLogWriter, settings: Settings) -> ConsumerSpec:
    return ConsumerSpec(
        settings.request_log_queue,
        [REQUEST_COMPLETED],
        writer.handle,
        # Enough messages in flight to fill a batch: they wait for the batch to be written.
        prefetch=settings.traffic_batch_size * 2,
        on_start=writer.start,
        on_stop=writer.stop,
    )


def replay_spec(executor: ReplayExecutor, settings: Settings) -> ConsumerSpec:
    # A small prefetch: a job can wait for a provider slot or run for minutes, and messages
    # that were prefetched but not started cannot be taken by another worker meanwhile.
    return ConsumerSpec(
        settings.replay_queue, [REPLAY_EXECUTE], executor.handle, prefetch=settings.replay_prefetch
    )


async def _log_run_finished(message: Message) -> None:
    """Placeholder consumer of the end-of-run event. Quality evaluation and aggregation will
    subscribe to the same event; until then it is only recorded in the log."""
    payload = message.payload if isinstance(message.payload, dict) else {}
    log.info(
        "calibration_run_finished",
        run_id=payload.get("run_id"),
        status=payload.get("status"),
        results=payload.get("results"),
        spent_usd=payload.get("spent_usd"),
    )


def run_finished_spec(settings: Settings) -> ConsumerSpec:
    return ConsumerSpec(settings.run_finished_queue, [CALIBRATION_RUN_FINISHED], _log_run_finished)


def build_consumers(res: Resources, settings: Settings) -> list[ConsumerSpec]:
    keyring = KeyRing.from_text(settings.master_keys.get_secret_value())
    return [
        request_log_spec(RequestLogWriter(res.session_factory, settings), settings),
        replay_spec(ReplayExecutor(res, settings, keyring), settings),
        run_finished_spec(settings),
    ]
