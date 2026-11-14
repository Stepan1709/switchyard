"""Imports every module that defines tables, so `Base.metadata` is complete.

Alembic autogenerate and the models-vs-migrations test need all tables registered. Plain
side-effect imports look "unused" to linters, which then delete them; here the modules are
used (they are listed), so nothing can be removed by accident. Add new model modules here.
"""

from types import ModuleType

from app.core import idempotency, inbox, outbox
from app.modules.audit import models as audit_models
from app.modules.auth import models as auth_models
from app.modules.projects import models as projects_models
from app.modules.registry import models as registry_models

ALL_MODEL_MODULES: tuple[ModuleType, ...] = (
    idempotency,
    inbox,
    outbox,
    audit_models,
    auth_models,
    projects_models,
    registry_models,
)
