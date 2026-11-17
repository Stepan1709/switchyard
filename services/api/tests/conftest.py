"""Shared by unit and integration tests."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# A tracer provider can be set only once per process, so every test module shares this one.
SPAN_EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPAN_EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """The spans finished during the test (the list is emptied before it starts)."""
    SPAN_EXPORTER.clear()
    return SPAN_EXPORTER
