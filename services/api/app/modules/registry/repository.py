"""SQL for providers, models and price versions."""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.registry.models import Model, ModelPrice, Provider


class RegistryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # providers --------------------------------------------------------------------------
    def add_provider(self, provider: Provider) -> None:
        self.session.add(provider)

    async def get_provider(self, provider_id: uuid.UUID) -> Provider | None:
        return await self.session.get(Provider, provider_id)

    async def list_providers(self) -> list[Provider]:
        stmt = select(Provider).order_by(Provider.name)
        return list((await self.session.execute(stmt)).scalars())

    async def count_models(self, provider_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Model).where(Model.provider_id == provider_id)
        return (await self.session.execute(stmt)).scalar_one()

    async def first_model_name(self, provider_id: uuid.UUID) -> str | None:
        stmt = (
            select(Model.name)
            .where(Model.provider_id == provider_id, Model.enabled.is_(True))
            .order_by(Model.tier, Model.name)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar()

    async def delete_provider(self, provider: Provider) -> None:
        await self.session.delete(provider)

    # models -----------------------------------------------------------------------------
    def add_model(self, model: Model) -> None:
        self.session.add(model)

    async def get_model(self, model_id: uuid.UUID) -> Model | None:
        return await self.session.get(Model, model_id)

    async def list_models(self, provider_id: uuid.UUID | None = None) -> list[Model]:
        stmt = select(Model).order_by(Model.tier, Model.name)
        if provider_id is not None:
            stmt = stmt.where(Model.provider_id == provider_id)
        return list((await self.session.execute(stmt)).scalars())

    # prices -----------------------------------------------------------------------------
    def add_price(self, price: ModelPrice) -> None:
        self.session.add(price)

    async def get_open_price_for_update(self, model_id: uuid.UUID) -> ModelPrice | None:
        stmt = (
            select(ModelPrice)
            .where(ModelPrice.model_id == model_id, ModelPrice.valid_to.is_(None))
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalar()

    async def list_prices(self, model_id: uuid.UUID) -> list[ModelPrice]:
        stmt = (
            select(ModelPrice)
            .where(ModelPrice.model_id == model_id)
            .order_by(ModelPrice.valid_from.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def price_at(self, model_id: uuid.UUID, at: datetime) -> ModelPrice | None:
        stmt = (
            select(ModelPrice)
            .where(
                ModelPrice.model_id == model_id,
                ModelPrice.valid_from <= at,
                (ModelPrice.valid_to.is_(None)) | (ModelPrice.valid_to > at),
            )
            .order_by(ModelPrice.valid_from.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar()
