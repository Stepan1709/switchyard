"""Projects, their keys, members and settings; creating administrators."""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form
from fastapi.responses import Response

from app.forms import checked, number, optional_int, optional_text
from app.ui import Ctx, admin_action, page

router = APIRouter()


@router.get("/projects")
async def projects(ctx: Annotated[Ctx, Depends(page)]) -> Response:
    project = await ctx.project()
    models = await ctx.api.get("/models")
    keys: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    task_types: list[dict[str, Any]] = []
    if project:
        pid = project["id"]
        keys, members, task_types = await asyncio.gather(
            ctx.api.get(f"/projects/{pid}/keys"),
            ctx.api.get(f"/projects/{pid}/members"),
            ctx.api.get(f"/projects/{pid}/task-types"),
        )
    reveal = ctx.session.data.pop("reveal", None)
    if reveal:
        await ctx.store.save(ctx.session)  # shown once: it must not come back on reload
    return await ctx.render(
        "projects.html",
        page="projects",
        keys=keys,
        members=members,
        task_options=[("", "(none)")] + [(t["name"], t["name"]) for t in task_types],
        model_options=[("", "(none)")] + [(m["id"], m["name"]) for m in models if m["enabled"]],
        reveal=reveal,
    )


@router.post("/projects")
async def create_project(
    ctx: Annotated[Ctx, Depends(admin_action)],
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
) -> Response:
    created = await ctx.api.send(
        "POST", "/projects", {"name": name.strip(), "description": optional_text(description)}
    )
    ctx.session.data["project_id"] = created["id"]
    await ctx.store.save(ctx.session)
    await ctx.flash(
        "ok", f"Project {created['name']} was created. Now give your application a key."
    )
    return ctx.redirect("/projects")


@router.post("/projects/settings")
async def settings(
    ctx: Annotated[Ctx, Depends(admin_action)],
    description: Annotated[str, Form()] = "",
    store_bodies: Annotated[str, Form()] = "",
    mask_pii: Annotated[str, Form()] = "",
    reference_model_id: Annotated[str, Form()] = "",
) -> Response:
    pid = await ctx.project_id()
    await ctx.api.send(
        "PATCH",
        f"/projects/{pid}",
        {
            "description": optional_text(description),
            "store_bodies": checked(store_bodies),
            "mask_pii": checked(mask_pii),
        },
    )
    await ctx.api.send(
        "PUT", f"/projects/{pid}/reference-model", {"model_id": optional_text(reference_model_id)}
    )
    await ctx.flash("ok", "Settings saved.")
    return ctx.redirect("/projects")


# ---- keys ---------------------------------------------------------------------------------------
@router.post("/projects/keys")
async def create_key(
    ctx: Annotated[Ctx, Depends(admin_action)],
    name: Annotated[str, Form()],
    default_task_type: Annotated[str, Form()] = "",
    rate_limit_rpm: Annotated[str, Form()] = "",
    monthly_budget_usd: Annotated[str, Form()] = "",
) -> Response:
    pid = await ctx.project_id()
    created = await ctx.api.send(
        "POST",
        f"/projects/{pid}/keys",
        {
            "name": name.strip(),
            "default_task_type": optional_text(default_task_type),
            "rate_limit_rpm": optional_int(rate_limit_rpm),
            "monthly_budget_usd": optional_text(monthly_budget_usd) and number(monthly_budget_usd),
        },
    )
    # The full key exists only in this answer. It is kept in the session until the next page
    # view shows it once.
    ctx.session.data["reveal"] = {"name": created["name"], "key": created["key"]}
    await ctx.store.save(ctx.session)
    return ctx.redirect("/projects")


@router.post("/projects/keys/{key_id}/limits")
async def key_limits(
    ctx: Annotated[Ctx, Depends(admin_action)],
    key_id: str,
    rate_limit_rpm: Annotated[str, Form()] = "",
    monthly_budget_usd: Annotated[str, Form()] = "",
) -> Response:
    pid = await ctx.project_id()
    await ctx.api.send(
        "PATCH",
        f"/projects/{pid}/keys/{key_id}",
        {
            "rate_limit_rpm": optional_int(rate_limit_rpm),
            "monthly_budget_usd": optional_text(monthly_budget_usd) and number(monthly_budget_usd),
        },
    )
    await ctx.flash("ok", "The limits of the key were saved.")
    return ctx.redirect("/projects")


@router.post("/projects/keys/{key_id}/revoke")
async def revoke_key(ctx: Annotated[Ctx, Depends(admin_action)], key_id: str) -> Response:
    pid = await ctx.project_id()
    await ctx.api.send("POST", f"/projects/{pid}/keys/{key_id}/revoke")
    await ctx.flash("ok", "The key was revoked. Applications using it stop working within seconds.")
    return ctx.redirect("/projects")


@router.post("/projects/keys/{key_id}/recount")
async def recount_budget(ctx: Annotated[Ctx, Depends(admin_action)], key_id: str) -> Response:
    pid = await ctx.project_id()
    state = await ctx.api.send("POST", f"/projects/{pid}/keys/{key_id}/budget/recount")
    await ctx.flash(
        "ok", f"The counter was set from the traffic log: ${state['spent_usd']} spent this month."
    )
    return ctx.redirect("/projects")


# ---- members and administrators -----------------------------------------------------------------
@router.post("/projects/members")
async def add_member(
    ctx: Annotated[Ctx, Depends(admin_action)], email: Annotated[str, Form()]
) -> Response:
    pid = await ctx.project_id()
    await ctx.api.send("POST", f"/projects/{pid}/members", {"email": email.strip()})
    await ctx.flash("ok", f"{email.strip()} can now see this project.")
    return ctx.redirect("/projects")


@router.post("/projects/members/{member_id}/remove")
async def remove_member(ctx: Annotated[Ctx, Depends(admin_action)], member_id: str) -> Response:
    pid = await ctx.project_id()
    await ctx.api.send("DELETE", f"/projects/{pid}/members/{member_id}")
    await ctx.flash("ok", "The member was removed.")
    return ctx.redirect("/projects")


@router.post("/admins")
async def create_admin(
    ctx: Annotated[Ctx, Depends(admin_action)],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = "viewer",
) -> Response:
    await ctx.api.send(
        "POST", "/admins", {"email": email.strip(), "password": password, "role": role}
    )
    await ctx.flash("ok", f"{email.strip()} can sign in as {role}.")
    return ctx.redirect("/projects")
