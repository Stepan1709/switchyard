import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

TaskType = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime


class MemberAdd(BaseModel):
    email: EmailStr


class MemberOut(BaseModel):
    admin_id: uuid.UUID
    email: str


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    default_task_type: str | None = TaskType
    rate_limit_rpm: int | None = Field(default=None, gt=0, le=1_000_000)
    monthly_budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=4)


class KeyUpdate(BaseModel):
    """Partial update: only the fields present in the request are changed (null clears one)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    default_task_type: str | None = TaskType
    rate_limit_rpm: int | None = Field(default=None, gt=0, le=1_000_000)
    monthly_budget_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=4)


class KeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    prefix: str
    default_task_type: str | None
    rate_limit_rpm: int | None
    monthly_budget_usd: Decimal | None
    revoked_at: datetime | None
    created_at: datetime


class KeyCreated(KeyOut):
    """The only response that contains the full key. It cannot be retrieved again."""

    key: str
