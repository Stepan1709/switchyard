"""Providers and models: the registry, connection test and prices."""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form
from fastapi.responses import Response

from app.api_client import ApiError
from app.forms import checked, number, optional_int, optional_text
from app.ui import Ctx, admin_action, page

router = APIRouter()


async def _current_price(ctx: Ctx, model_id: str) -> dict[str, Any] | None:
    try:
        price: dict[str, Any] = await ctx.api.get(f"/models/{model_id}/price")
        return price
    except ApiError as exc:
        if exc.status == 404:  # no price yet
            return None
        raise


@router.get("/models")
async def registry(ctx: Annotated[Ctx, Depends(page)]) -> Response:
    providers, models = await asyncio.gather(ctx.api.get("/providers"), ctx.api.get("/models"))
    prices = await asyncio.gather(*[_current_price(ctx, m["id"]) for m in models])
    by_provider = {p["id"]: p["name"] for p in providers}
    rows = [
        {**m, "provider_name": by_provider.get(m["provider_id"], "?"), "price": price}
        for m, price in zip(models, prices, strict=True)
    ]
    options = [(p["id"], p["name"]) for p in providers if p["enabled"]]
    return await ctx.render(
        "models.html", page="models", providers=providers, provider_options=options, models=rows
    )


# ---- providers ----------------------------------------------------------------------------------
@router.post("/models/providers")
async def create_provider(
    ctx: Annotated[Ctx, Depends(admin_action)],
    name: Annotated[str, Form()],
    base_url: Annotated[str, Form()],
    auth_type: Annotated[str, Form()] = "bearer",
    api_key: Annotated[str, Form()] = "",
    is_local: Annotated[str, Form()] = "",
    max_concurrency: Annotated[str, Form()] = "",
    max_rps: Annotated[str, Form()] = "",
) -> Response:
    body: dict[str, Any] = {
        "name": name.strip(),
        "base_url": base_url.strip(),
        "auth_type": auth_type,
        "is_local": checked(is_local),
        "api_key": optional_text(api_key),
        "calibration_max_concurrency": optional_int(max_concurrency),
        "calibration_max_rps": optional_text(max_rps),
    }
    await ctx.api.send("POST", "/providers", body)
    await ctx.flash("ok", f"Provider {name.strip()} was added.")
    return ctx.redirect("/models")


@router.post("/models/providers/{provider_id}/test")
async def test_provider(ctx: Annotated[Ctx, Depends(admin_action)], provider_id: str) -> Response:
    """Check the connection; the answer replaces the button (HTMX fragment)."""
    result = await ctx.api.send("POST", f"/providers/{provider_id}/test", {})
    return await ctx.render("partials/provider_test.html", result=result)


@router.post("/models/providers/{provider_id}/toggle")
async def toggle_provider(
    ctx: Annotated[Ctx, Depends(admin_action)], provider_id: str, enabled: Annotated[str, Form()]
) -> Response:
    await ctx.api.send("PATCH", f"/providers/{provider_id}", {"enabled": checked(enabled)})
    await ctx.flash("ok", "The provider was " + ("enabled." if checked(enabled) else "disabled."))
    return ctx.redirect("/models")


@router.post("/models/providers/{provider_id}/delete")
async def delete_provider(ctx: Annotated[Ctx, Depends(admin_action)], provider_id: str) -> Response:
    await ctx.api.send("DELETE", f"/providers/{provider_id}")
    await ctx.flash("ok", "The provider was deleted.")
    return ctx.redirect("/models")


# ---- models -------------------------------------------------------------------------------------
@router.post("/models/models")
async def create_model(
    ctx: Annotated[Ctx, Depends(admin_action)],
    provider_id: Annotated[str, Form()],
    name: Annotated[str, Form()],
    context_window: Annotated[int, Form()],
    tier: Annotated[int, Form()] = 1,
    max_output_tokens: Annotated[str, Form()] = "",
    cost_type: Annotated[str, Form()] = "per_token",
    idle_threshold_pct: Annotated[str, Form()] = "",
    supports_tools: Annotated[str, Form()] = "",
    supports_json: Annotated[str, Form()] = "",
    supports_stream: Annotated[str, Form()] = "",
    input_per_1m: Annotated[str, Form()] = "",
    output_per_1m: Annotated[str, Form()] = "",
    monthly_cost_usd: Annotated[str, Form()] = "",
) -> Response:
    body: dict[str, Any] = {
        "provider_id": provider_id,
        "name": name.strip(),
        "tier": tier,
        "context_window": context_window,
        "max_output_tokens": optional_int(max_output_tokens),
        "cost_type": cost_type,
        "idle_threshold_pct": optional_text(idle_threshold_pct),
        "supports_tools": checked(supports_tools),
        "supports_json": checked(supports_json),
        "supports_stream": checked(supports_stream),
    }
    model = await ctx.api.send("POST", "/models", body)
    price = _price_body(cost_type, input_per_1m, output_per_1m, monthly_cost_usd)
    if price:
        await ctx.api.send("POST", f"/models/{model['id']}/prices", price)
    await ctx.flash("ok", f"Model {name.strip()} was added.")
    return ctx.redirect("/models")


def _price_body(
    cost_type: str, input_per_1m: str, output_per_1m: str, monthly: str
) -> dict[str, Any] | None:
    if cost_type == "fixed_monthly":
        value = optional_text(monthly)
        return {"monthly_cost_usd": number(value)} if value else None
    cheap, dear = optional_text(input_per_1m), optional_text(output_per_1m)
    if cheap is None and dear is None:
        return None
    return {"input_per_1m": number(cheap or "0"), "output_per_1m": number(dear or "0")}


@router.post("/models/models/{model_id}/price")
async def set_price(
    ctx: Annotated[Ctx, Depends(admin_action)],
    model_id: str,
    cost_type: Annotated[str, Form()],
    input_per_1m: Annotated[str, Form()] = "",
    output_per_1m: Annotated[str, Form()] = "",
    monthly_cost_usd: Annotated[str, Form()] = "",
) -> Response:
    body = _price_body(cost_type, input_per_1m, output_per_1m, monthly_cost_usd)
    if body is None:
        raise ApiError(422, "No price", "Enter a price.")
    await ctx.api.send("POST", f"/models/{model_id}/prices", body)
    await ctx.flash("ok", "A new price is in effect from now; older requests keep their price.")
    return ctx.redirect("/models")


@router.post("/models/models/{model_id}/toggle")
async def toggle_model(
    ctx: Annotated[Ctx, Depends(admin_action)], model_id: str, enabled: Annotated[str, Form()]
) -> Response:
    await ctx.api.send("PATCH", f"/models/{model_id}", {"enabled": checked(enabled)})
    await ctx.flash("ok", "The model was " + ("enabled." if checked(enabled) else "disabled."))
    return ctx.redirect("/models")
