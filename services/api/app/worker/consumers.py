"""The list of queue consumers the worker runs.

Empty for now: real consumers (traffic log writer, replay, judge ...) are added together with
the features that need them. Each one is a ConsumerSpec(queue name, routing keys, handler).
"""

from app.core.config import Settings
from app.core.messaging import ConsumerSpec
from app.core.resources import Resources


def build_consumers(res: Resources, settings: Settings) -> list[ConsumerSpec]:
    return []
