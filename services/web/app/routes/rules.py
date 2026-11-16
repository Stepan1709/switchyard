"""Routing rules: proposed, live, history, rollback and the mode of every task type."""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form
from fastapi.responses import Response

from app.api_client import ApiError
from app.forms import number, optional_text
from app.routes.analytics import _health
from app.ui import Ctx, admin_action, page

router = APIRouter()

MODES = ("record", "shadow", "route")


@router.get("/rules")
async def rules(ctx: Annotated[Ctx, Depends(page)]) -> Response:
    pid = await ctx.project_id()
    task_types, models = await asyncio.gather(
        ctx.api.get(f"/projects/{pid}/task-types"), ctx.api.get("/models")
    )
    histories = await asyncio.gather(
        *[ctx.api.get(f"/projects/{pid}/task-types/{t['name']}/rules") for t in task_types]
    )
    health = {h["task_type"]: h for h in await _health(ctx, pid, task_types)}
    names = {m["id"]: m["name"] for m in models}
    cards = []
    for task, history in zip(task_types, histories, strict=True):
        active = next((r for r in history if r["status"] == "active"), None)
        cards.append(
            {
                "task": task,
                "active": active,
                "proposed": [r for r in history if r["status"] == "proposed"],
                "history": [r for r in history if r["status"] not in ("proposed", "active")],
                "has_previous": any(r["status"] == "superseded" for r in history),
                "health": health.get(task["name"]),
                "shadow_names": [names.get(i, "?") for i in task["shadow_model_ids"]],
            }
        )
    return await ctx.render(
        "rules.html",
        page="rules",
        cards=cards,
        models=[m for m in models if m["enabled"]],
        model_options=[("", "(none)")] + [(m["id"], m["name"]) for m in models if m["enabled"]],
        modes=MODES,
        idem=ctx.new_idempotency_key(),
    )


@router.post("/rules/task/{task}/mode")
async def set_mode(
    ctx: Annotated[Ctx, Depends(admin_action)],
    task: str,
    mode: Annotated[str, Form()],
    shadow_model_ids: Annotated[list[str] | None, Form()] = None,
    shadow_rate_pct: Annotated[str, Form()] = "0",
) -> Response:
    if mode not in MODES:
        raise ApiError(422, "Invalid mode", "The mode must be record, shadow or route.")
    pid = await ctx.project_id()
    rate = float(number(shadow_rate_pct or "0")) / 100
    await ctx.api.send(
        "PATCH",
        f"/projects/{pid}/task-types/{task}",
        {"mode": mode, "shadow_model_ids": shadow_model_ids or [], "shadow_rate": rate},
    )
    await ctx.flash("ok", f"{task} now works in {mode} mode.")
    return ctx.redirect(f"/rules#{task}")


@router.post("/rules/task/{task}/create")
async def create_rule(
    ctx: Annotated[Ctx, Depends(admin_action)],
    task: str,
    first: Annotated[str, Form()],
    second: Annotated[str, Form()] = "",
    third: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> Response:
    chain = [m for m in (first, second, third) if m]
    if len(set(chain)) != len(chain):
        raise ApiError(422, "Duplicate model", "A model can appear in the chain only once.")
    pid = await ctx.project_id()
    rule = await ctx.api.send(
        "POST",
        f"/projects/{pid}/task-types/{task}/rules",
        {"chain": chain, "note": optional_text(note)},
    )
    await ctx.flash("ok", f"Rule version {rule['version']} was proposed.")
    return ctx.redirect(f"/rules#{task}")


@router.post("/rules/task/{task}/rollback")
async def rollback(ctx: Annotated[Ctx, Depends(admin_action)], task: str) -> Response:
    pid = await ctx.project_id()
    rule = await ctx.api.send("POST", f"/projects/{pid}/task-types/{task}/rules/rollback")
    await ctx.flash("ok", f"Rolled back: version {rule['version']} is live again.")
    return ctx.redirect(f"/rules#{task}")


@router.post("/rules/{rule_id}/activate")
async def activate(
    ctx: Annotated[Ctx, Depends(admin_action)],
    rule_id: str,
    task: Annotated[str, Form()],
    expected: Annotated[str, Form()] = "",
    idem: Annotated[str, Form()] = "",
) -> Response:
    pid = await ctx.project_id()
    expected_version = int(expected) if expected.strip().isdigit() else None
    try:
        rule: dict[str, Any] = await ctx.api.send(
            "POST",
            f"/projects/{pid}/rules/{rule_id}/activate",
            {"expected_active_version": expected_version},
            idempotency_key=optional_text(idem),
        )
    except ApiError as exc:
        if exc.is_conflict:
            raise ApiError(
                409,
                "Changed meanwhile",
                "The live rule changed since you opened this page. Check it and decide again.",
            ) from exc
        raise
    await ctx.flash("ok", f"Version {rule['version']} is live now.")
    return ctx.redirect(f"/rules#{task}")


@router.post("/rules/{rule_id}/reject")
async def reject(
    ctx: Annotated[Ctx, Depends(admin_action)], rule_id: str, task: Annotated[str, Form()]
) -> Response:
    pid = await ctx.project_id()
    await ctx.api.send("POST", f"/projects/{pid}/rules/{rule_id}/reject")
    await ctx.flash("ok", "The proposal was rejected.")
    return ctx.redirect(f"/rules#{task}")
