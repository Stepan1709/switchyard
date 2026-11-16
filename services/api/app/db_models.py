"""Imports every module that defines tables, so `Base.metadata` is complete.

Alembic autogenerate and the models-vs-migrations test need all tables registered. Plain
side-effect imports look "unused" to linters, which then delete them; here the modules are
used (they are listed), so nothing can be removed by accident. Add new model modules here.
"""

import re
from types import ModuleType
from typing import Any

from app.core import idempotency, inbox, outbox
from app.modules.analytics import models as analytics_models
from app.modules.audit import models as audit_models
from app.modules.auth import models as auth_models
from app.modules.calibration import models as calibration_models
from app.modules.evaluation import models as evaluation_models
from app.modules.feedback import models as feedback_models
from app.modules.projects import models as projects_models
from app.modules.registry import models as registry_models
from app.modules.routing import models as routing_models
from app.modules.traffic import models as traffic_models

ALL_MODEL_MODULES: tuple[ModuleType, ...] = (
    idempotency,
    inbox,
    outbox,
    analytics_models,
    audit_models,
    auth_models,
    calibration_models,
    evaluation_models,
    feedback_models,
    projects_models,
    registry_models,
    routing_models,
    traffic_models,
)


_PARTITION_RE = re.compile(r"requests_\d{4}_\d{2}")


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Alembic filter: monthly partitions of `requests` are created by code, not by migrations,
    so they (and their indexes) must not be reported as differences."""
    if type_ == "table" and name and _PARTITION_RE.fullmatch(name):
        return False
    table = getattr(obj, "table", None)
    return not (table is not None and _PARTITION_RE.fullmatch(table.name or ""))
