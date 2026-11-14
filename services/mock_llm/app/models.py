"""The simulated models. They differ in quality, speed and context size on purpose, so that
calibration (comparing cheap models against an expensive one) has something to find."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MockModel:
    name: str
    tier: int
    context_window: int
    max_output_tokens: int
    # Probability that the model gives the "canonical" answer (see engine.py).
    accuracy: float
    # Simulated time to first token and generation speed.
    ttft_ms: float
    tokens_per_second: float


MODELS: dict[str, MockModel] = {
    m.name: m
    for m in (
        MockModel("mock-tiny", 1, 4_096, 512, 0.60, 40, 200),
        MockModel("mock-mid", 2, 16_384, 1_024, 0.85, 120, 100),
        MockModel("mock-strong", 3, 128_000, 4_096, 0.97, 300, 50),
        MockModel("mock-judge", 3, 32_768, 1_024, 0.99, 200, 80),
    )
}
JUDGE_MODEL = "mock-judge"
