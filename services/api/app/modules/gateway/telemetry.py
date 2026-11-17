"""The gateway's request metrics, fed by the same record that feeds the log and the traffic
event: one place knows what a finished request looked like."""

from app.core import metrics
from app.modules.gateway.record import RequestRecord

NONE = "none"


class MetricsRecorder:
    """Implements the `Recorder` protocol. Cheap and never raises."""

    def submit(self, record: RequestRecord) -> None:
        model = record.model or NONE
        metrics.GATEWAY_REQUESTS.labels(
            record.status, record.mode, record.provider or NONE, model
        ).inc()
        if record.duration_ms is not None:
            metrics.GATEWAY_DURATION.labels("true" if record.stream else "false").observe(
                record.duration_ms / 1000
            )
        if record.ttft_ms is not None:
            metrics.GATEWAY_TTFT.labels(model).observe(record.ttft_ms / 1000)
        if record.overhead_ms is not None:
            metrics.GATEWAY_OVERHEAD.observe(record.overhead_ms / 1000)
        if record.prompt_tokens:
            metrics.GATEWAY_TOKENS.labels("prompt", model).inc(record.prompt_tokens)
        if record.completion_tokens:
            metrics.GATEWAY_TOKENS.labels("completion", model).inc(record.completion_tokens)
        if record.cost_usd:
            metrics.GATEWAY_COST.labels(model).inc(float(record.cost_usd))
