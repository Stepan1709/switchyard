"""Admin REST for the provider and model registry.

The registry is global (shared by all projects): any signed-in administrator or viewer may
read it, only the admin role may change it.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.core import ssrf
from app.core.crypto import KeyRing
from app.core.deps import HttpClientDep, SessionDep
from app.modules.auth.deps import AdminOnly, CurrentAdmin, IdempotencyDep, SettingsDep, client_ip
from app.modules.registry.schemas import (
    ConnectionTestRequest,
    ConnectionTestResult,
    ModelCreate,
    ModelOut,
    ModelUpdate,
    PriceCreate,
    PriceOut,
    ProviderCreate,
    ProviderOut,
    ProviderUpdate,
)
from app.modules.registry.service import ModelService, ProviderService

router = APIRouter(tags=["registry"])


def get_keyring(request: Request) -> KeyRing:
    keyring: KeyRing = request.app.state.keyring
    return keyring


def get_provider_service(
    request: Request, session: SessionDep, settings: SettingsDep, http: HttpClientDep
) -> ProviderService:
    # Tests may replace the DNS resolver through app.state to simulate DNS answers.
    resolver = getattr(request.app.state, "ssrf_resolver", ssrf.system_resolver)
    return ProviderService(session, settings, get_keyring(request), http, resolver)


def get_model_service(session: SessionDep) -> ModelService:
    return ModelService(session)


ProviderServiceDep = Annotated[ProviderService, Depends(get_provider_service)]
ModelServiceDep = Annotated[ModelService, Depends(get_model_service)]


# ---- providers ----
@router.post("/providers", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
async def create_provider(
    body: ProviderCreate,
    request: Request,
    admin: AdminOnly,
    service: ProviderServiceDep,
    idem: IdempotencyDep,
) -> Any:
    async def call() -> ProviderOut:
        return ProviderOut.model_validate(await service.create(admin, body, client_ip(request)))

    return await idem.run(call, status_code=status.HTTP_201_CREATED)


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(_: CurrentAdmin, service: ProviderServiceDep) -> list[ProviderOut]:
    return [ProviderOut.model_validate(p) for p in await service.list_providers()]


@router.get("/providers/{provider_id}", response_model=ProviderOut)
async def get_provider(
    provider_id: uuid.UUID, _: CurrentAdmin, service: ProviderServiceDep
) -> ProviderOut:
    return ProviderOut.model_validate(await service.get(provider_id))


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
async def update_provider(
    provider_id: uuid.UUID,
    body: ProviderUpdate,
    request: Request,
    admin: AdminOnly,
    service: ProviderServiceDep,
) -> ProviderOut:
    provider = await service.update(admin, provider_id, body, client_ip(request))
    return ProviderOut.model_validate(provider)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: uuid.UUID, request: Request, admin: AdminOnly, service: ProviderServiceDep
) -> Response:
    await service.delete(admin, provider_id, client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/providers/{provider_id}/test", response_model=ConnectionTestResult)
async def test_provider(
    provider_id: uuid.UUID,
    request: Request,
    admin: AdminOnly,
    service: ProviderServiceDep,
    body: ConnectionTestRequest | None = None,
) -> ConnectionTestResult:
    model = body.model if body else None
    return await service.test_connection(admin, provider_id, model, client_ip(request))


# ---- models ----
@router.post("/models", response_model=ModelOut, status_code=status.HTTP_201_CREATED)
async def create_model(
    body: ModelCreate,
    request: Request,
    admin: AdminOnly,
    service: ModelServiceDep,
    idem: IdempotencyDep,
) -> Any:
    async def call() -> ModelOut:
        return ModelOut.model_validate(await service.create(admin, body, client_ip(request)))

    return await idem.run(call, status_code=status.HTTP_201_CREATED)


@router.get("/models", response_model=list[ModelOut])
async def list_models(
    _: CurrentAdmin, service: ModelServiceDep, provider_id: uuid.UUID | None = None
) -> list[ModelOut]:
    return [ModelOut.model_validate(m) for m in await service.list_models(provider_id)]


@router.get("/models/{model_id}", response_model=ModelOut)
async def get_model(model_id: uuid.UUID, _: CurrentAdmin, service: ModelServiceDep) -> ModelOut:
    return ModelOut.model_validate(await service.get(model_id))


@router.patch("/models/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: uuid.UUID,
    body: ModelUpdate,
    request: Request,
    admin: AdminOnly,
    service: ModelServiceDep,
) -> ModelOut:
    return ModelOut.model_validate(await service.update(admin, model_id, body, client_ip(request)))


@router.post(
    "/models/{model_id}/prices", response_model=PriceOut, status_code=status.HTTP_201_CREATED
)
async def set_price(
    model_id: uuid.UUID,
    body: PriceCreate,
    request: Request,
    admin: AdminOnly,
    service: ModelServiceDep,
    idem: IdempotencyDep,
) -> Any:
    async def call() -> PriceOut:
        price = await service.set_price(admin, model_id, body, client_ip(request))
        return PriceOut.model_validate(price)

    return await idem.run(call, status_code=status.HTTP_201_CREATED)


@router.get("/models/{model_id}/prices", response_model=list[PriceOut])
async def list_prices(
    model_id: uuid.UUID, _: CurrentAdmin, service: ModelServiceDep
) -> list[PriceOut]:
    return [PriceOut.model_validate(p) for p in await service.list_prices(model_id)]


@router.get("/models/{model_id}/price", response_model=PriceOut)
async def price_at(
    model_id: uuid.UUID,
    _: CurrentAdmin,
    service: ModelServiceDep,
    at: datetime | None = None,
) -> PriceOut:
    """The price that was valid at `at` (default: now)."""
    return PriceOut.model_validate(await service.price_at(model_id, at))
