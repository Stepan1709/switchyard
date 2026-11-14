"""Registry rules: providers (with encrypted access keys), models and price history."""

import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ssrf
from app.core.config import Settings
from app.core.crypto import KeyRing, mask_secret
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.logging import get_logger
from app.modules.audit import service as audit
from app.modules.auth.models import Admin
from app.modules.registry.connection import run_connection_test
from app.modules.registry.models import Model, ModelPrice, Provider
from app.modules.registry.repository import RegistryRepository
from app.modules.registry.schemas import (
    ConnectionTestResult,
    ModelCreate,
    ModelUpdate,
    PriceCreate,
    ProviderCreate,
    ProviderUpdate,
)

log = get_logger(__name__)


class ProviderService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        keyring: KeyRing,
        http: httpx.AsyncClient | None = None,
        resolver: ssrf.Resolver = ssrf.system_resolver,
    ) -> None:
        self.session = session
        self.settings = settings
        self.keyring = keyring
        self.http = http
        self.resolver = resolver
        self.repo = RegistryRepository(session)

    async def _validate_address(self, base_url: str, is_local: bool) -> None:
        # Raises SSRFError (422) for a forbidden address; DNS is resolved right now.
        target = ssrf.parse_base_url(
            base_url, is_local=is_local, local_hosts=self.settings.local_provider_hosts
        )
        await ssrf.resolve_checked(target, is_local=is_local, resolver=self.resolver)

    def _set_key(self, provider: Provider, api_key: str) -> None:
        # AAD = provider id: a ciphertext copied into another row will not decrypt there.
        provider.api_key_encrypted = self.keyring.encrypt(api_key, aad=str(provider.id))
        provider.api_key_mask = mask_secret(api_key)

    def _decrypted_key(self, provider: Provider) -> str | None:
        if provider.api_key_encrypted is None:
            return None
        return self.keyring.decrypt(provider.api_key_encrypted, aad=str(provider.id))

    async def _get(self, provider_id: uuid.UUID) -> Provider:
        provider = await self.repo.get_provider(provider_id)
        if provider is None:
            raise NotFound("Provider not found")
        return provider

    # CRUD -------------------------------------------------------------------------------
    async def create(self, admin: Admin, data: ProviderCreate, ip: str | None) -> Provider:
        key = data.api_key.get_secret_value() if data.api_key else None
        if data.auth_type == "none" and key:
            raise Unprocessable("api_key must not be set when auth_type is 'none'")
        if data.auth_type == "bearer" and not key:
            raise Unprocessable("api_key is required when auth_type is 'bearer'")
        await self._validate_address(data.base_url, data.is_local)

        provider = Provider(
            id=uuid.uuid4(),  # known before INSERT: it is part of the encryption context
            name=data.name,
            base_url=data.base_url.strip().rstrip("/"),
            auth_type=data.auth_type,
            is_local=data.is_local,
        )
        if key:
            self._set_key(provider, key)
        self.repo.add_provider(provider)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise Conflict("A provider with this name already exists") from exc
        audit.record(
            self.session,
            action="provider.create",
            actor_id=admin.id,
            target_type="provider",
            target_id=provider.id,
            details={"name": provider.name, "is_local": provider.is_local},
            ip=ip,
        )
        await self.session.commit()
        await self.session.refresh(provider)
        return provider

    async def list_providers(self) -> list[Provider]:
        return await self.repo.list_providers()

    async def get(self, provider_id: uuid.UUID) -> Provider:
        return await self._get(provider_id)

    async def update(
        self, admin: Admin, provider_id: uuid.UUID, data: ProviderUpdate, ip: str | None
    ) -> Provider:
        provider = await self._get(provider_id)
        changes = data.model_dump(exclude_unset=True, exclude={"api_key", "clear_api_key"})
        changed = set(changes)

        new_url = changes.get("base_url", provider.base_url)
        new_local = changes.get("is_local", provider.is_local)
        if "base_url" in changes or "is_local" in changes:
            await self._validate_address(new_url, new_local)
        for field, value in changes.items():
            if value is None and field in {"name", "base_url", "auth_type", "is_local", "enabled"}:
                continue  # these columns are not nullable: null means "leave unchanged"
            setattr(provider, field, value.strip().rstrip("/") if field == "base_url" else value)

        if data.clear_api_key:
            provider.api_key_encrypted = None
            provider.api_key_mask = None
            changed.add("api_key")
        if data.api_key is not None:
            self._set_key(provider, data.api_key.get_secret_value())
            changed.add("api_key")
        if provider.auth_type == "none" and provider.api_key_encrypted is not None:
            raise Unprocessable(
                "Remove the api_key (clear_api_key) before setting auth_type 'none'"
            )
        if provider.auth_type == "bearer" and provider.api_key_encrypted is None:
            raise Unprocessable("api_key is required when auth_type is 'bearer'")

        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise Conflict("A provider with this name already exists") from exc
        # Only field names are audited: never the key, never its mask.
        audit.record(
            self.session,
            action="provider.update",
            actor_id=admin.id,
            target_type="provider",
            target_id=provider.id,
            details={"fields": sorted(changed)},
            ip=ip,
        )
        await self.session.commit()
        await self.session.refresh(provider)
        return provider

    async def delete(self, admin: Admin, provider_id: uuid.UUID, ip: str | None) -> None:
        provider = await self._get(provider_id)
        if await self.repo.count_models(provider_id) > 0:
            raise Conflict("Delete or move the provider's models first")
        await self.repo.delete_provider(provider)
        audit.record(
            self.session,
            action="provider.delete",
            actor_id=admin.id,
            target_type="provider",
            target_id=provider_id,
            details={"name": provider.name},
            ip=ip,
        )
        await self.session.commit()

    # connection test ------------------------------------------------------------------------
    async def test_connection(
        self, admin: Admin, provider_id: uuid.UUID, model_name: str | None, ip: str | None
    ) -> ConnectionTestResult:
        if self.http is None:
            raise RuntimeError("an HTTP client is required to test a connection")
        provider = await self._get(provider_id)
        model_name = model_name or await self.repo.first_model_name(provider_id)
        # The key lives in memory only for the duration of this call.
        result = await run_connection_test(
            self.http,
            self.settings,
            base_url=provider.base_url,
            is_local=provider.is_local,
            api_key=self._decrypted_key(provider),
            model_name=model_name,
            resolver=self.resolver,
        )
        failed = result.completion if (result.completion and not result.completion.ok) else None
        failed = failed or (None if result.models_endpoint.ok else result.models_endpoint)
        provider.last_check_at = datetime.now(UTC)
        provider.last_check_ok = result.ok
        provider.last_check_latency_ms = result.models_endpoint.latency_ms
        provider.last_check_error = failed.error if failed else None
        audit.record(
            self.session,
            action="provider.test",
            actor_id=admin.id,
            target_type="provider",
            target_id=provider.id,
            details={"ok": result.ok},
            ip=ip,
        )
        await self.session.commit()
        log.info("provider_tested", provider_id=str(provider.id), ok=result.ok)
        return result


class ModelService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = RegistryRepository(session)

    async def _get(self, model_id: uuid.UUID) -> Model:
        model = await self.repo.get_model(model_id)
        if model is None:
            raise NotFound("Model not found")
        return model

    async def create(self, admin: Admin, data: ModelCreate, ip: str | None) -> Model:
        if await self.repo.get_provider(data.provider_id) is None:
            raise NotFound("Provider not found")
        if data.max_output_tokens and data.max_output_tokens > data.context_window:
            raise Unprocessable("max_output_tokens cannot exceed context_window")
        model = Model(**data.model_dump())
        self.repo.add_model(model)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise Conflict("This provider already has a model with this name") from exc
        audit.record(
            self.session,
            action="model.create",
            actor_id=admin.id,
            target_type="model",
            target_id=model.id,
            details={"name": model.name, "provider_id": str(model.provider_id)},
            ip=ip,
        )
        await self.session.commit()
        await self.session.refresh(model)
        return model

    async def list_models(self, provider_id: uuid.UUID | None = None) -> list[Model]:
        return await self.repo.list_models(provider_id)

    async def get(self, model_id: uuid.UUID) -> Model:
        return await self._get(model_id)

    async def update(
        self, admin: Admin, model_id: uuid.UUID, data: ModelUpdate, ip: str | None
    ) -> Model:
        model = await self._get(model_id)
        changes = data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            if value is None and field not in {"max_output_tokens", "idle_threshold_pct"}:
                continue  # only these two columns may be cleared
            setattr(model, field, value)
        if model.max_output_tokens and model.max_output_tokens > model.context_window:
            await self.session.rollback()
            raise Unprocessable("max_output_tokens cannot exceed context_window")
        audit.record(
            self.session,
            action="model.update",
            actor_id=admin.id,
            target_type="model",
            target_id=model.id,
            details={"fields": sorted(changes)},
            ip=ip,
        )
        await self.session.commit()
        await self.session.refresh(model)
        return model

    # prices -----------------------------------------------------------------------------
    async def set_price(
        self, admin: Admin, model_id: uuid.UUID, data: PriceCreate, ip: str | None
    ) -> ModelPrice:
        """Start a new price version; the previous one is closed at the same instant."""
        model = await self._get(model_id)
        per_token = data.input_per_1m is not None or data.output_per_1m is not None
        if model.cost_type == "per_token":
            if (
                data.input_per_1m is None
                or data.output_per_1m is None
                or data.monthly_cost_usd is not None
            ):
                raise Unprocessable("A per_token model needs input_per_1m and output_per_1m only")
        elif data.monthly_cost_usd is None or per_token:
            raise Unprocessable("A fixed_monthly model needs monthly_cost_usd only")

        start = data.effective_from or datetime.now(UTC)
        if start.tzinfo is None:
            raise Unprocessable("effective_from must include a time zone")

        current = await self.repo.get_open_price_for_update(model_id)
        if current is not None:
            if start <= current.valid_from:
                raise Unprocessable("effective_from must be later than the current price start")
            current.valid_to = start
            await self.session.flush()  # close the old interval before opening the new one
        price = ModelPrice(
            model_id=model_id,
            valid_from=start,
            input_per_1m=data.input_per_1m,
            output_per_1m=data.output_per_1m,
            monthly_cost_usd=data.monthly_cost_usd,
            created_by_id=admin.id,
        )
        self.repo.add_price(price)
        await self.session.flush()
        audit.record(
            self.session,
            action="model.price_set",
            actor_id=admin.id,
            target_type="model",
            target_id=model_id,
            details={"valid_from": start.isoformat()},
            ip=ip,
        )
        await self.session.commit()
        await self.session.refresh(price)
        return price

    async def list_prices(self, model_id: uuid.UUID) -> list[ModelPrice]:
        await self._get(model_id)
        return await self.repo.list_prices(model_id)

    async def price_at(self, model_id: uuid.UUID, at: datetime | None) -> ModelPrice:
        await self._get(model_id)
        when = at or datetime.now(UTC)
        if when.tzinfo is None:
            raise Unprocessable("at must include a time zone")
        price = await self.repo.price_at(model_id, when)
        if price is None:
            raise NotFound("No price was valid at that time")
        return price
