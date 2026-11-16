"""The queue consumers the worker runs. Each is a ConsumerSpec(queue, routing keys, handler)."""

from app.core.config import Settings
from app.core.crypto import KeyRing
from app.core.events import (
    CALIBRATION_RUN_FINISHED,
    JUDGE_EXECUTE,
    REPLAY_EXECUTE,
    REQUEST_COMPLETED,
)
from app.core.messaging import ConsumerSpec
from app.core.resources import Resources
from app.modules.calibration.executor import ReplayExecutor
from app.modules.evaluation.evaluator import EvaluationStarter, Evaluator
from app.modules.traffic.writer import RequestLogWriter


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


def run_finished_spec(starter: EvaluationStarter, settings: Settings) -> ConsumerSpec:
    """The end of a run starts the evaluation of its answers."""
    return ConsumerSpec(settings.run_finished_queue, [CALIBRATION_RUN_FINISHED], starter.handle)


def judge_spec(evaluator: Evaluator, settings: Settings) -> ConsumerSpec:
    return ConsumerSpec(
        settings.judge_queue, [JUDGE_EXECUTE], evaluator.handle, prefetch=settings.judge_prefetch
    )


def build_consumers(res: Resources, settings: Settings) -> list[ConsumerSpec]:
    keyring = KeyRing.from_text(settings.master_keys.get_secret_value())
    return [
        request_log_spec(RequestLogWriter(res.session_factory, settings), settings),
        replay_spec(ReplayExecutor(res, settings, keyring), settings),
        run_finished_spec(EvaluationStarter(res, settings), settings),
        judge_spec(Evaluator(res, settings, keyring), settings),
    ]
