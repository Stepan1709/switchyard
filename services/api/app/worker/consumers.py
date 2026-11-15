"""The queue consumers the worker runs. Each is a ConsumerSpec(queue, routing keys, handler)."""

from app.core.config import Settings
from app.core.events import REQUEST_COMPLETED
from app.core.messaging import ConsumerSpec
from app.core.resources import Resources
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


def build_consumers(res: Resources, settings: Settings) -> list[ConsumerSpec]:
    return [request_log_spec(RequestLogWriter(res.session_factory, settings), settings)]
