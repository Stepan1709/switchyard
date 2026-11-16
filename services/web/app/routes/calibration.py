"""Calibration runs: start, follow, steer, read the results and propose a rule."""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form
from fastapi.responses import Response

from app.api_client import ApiError
from app.forms import checked, number, optional_int, optional_text
from app.ui import Ctx, admin_action, page

router = APIRouter()
FINISHED = {"completed", "cancelled", "budget_exceeded"}


def keeps_changing(run: dict[str, Any]) -> bool:
    """Should the page keep asking for news about this run?"""
    evaluation = (run.get("evaluation") or {}).get("status", "pending")
    if evaluation == "done":
        return False
    if run["status"] in ("running", "paused"):
        return bool(run["status"] == "running" or evaluation == "running")
    return bool(run["status"] == "completed" or evaluation == "running")


@router.get("/runs")
async def runs(ctx: Annotated[Ctx, Depends(page)]) -> Response:
    pid = await ctx.project_id()
    project = await ctx.project()
    listed, task_types, models, providers = await asyncio.gather(
        ctx.api.get(f"/projects/{pid}/calibration-runs", limit=30),
        ctx.api.get(f"/projects/{pid}/task-types"),
        ctx.api.get("/models"),
        ctx.api.get("/providers"),
    )
    provider_names = {p["id"]: p["name"] for p in providers}
    # Two providers may serve a model of the same name: show where each one lives.
    by_id = {m["id"]: f"{m['name']} ({provider_names.get(m['provider_id'], '?')})" for m in models}
    reference = (project or {}).get("reference_model_id")
    enabled = [
        {**m, "label": by_id[m["id"]]} for m in models if m["enabled"] and m["id"] != reference
    ]
    return await ctx.render(
        "runs.html",
        page="runs",
        runs=listed,
        names=by_id,
        task_types=task_types,
        task_options=[(t["name"], t["name"]) for t in task_types],
        candidates=enabled,
        reference=by_id.get(reference),
        idem=ctx.new_idempotency_key(),
    )


@router.post("/runs")
async def start_run(
    ctx: Annotated[Ctx, Depends(admin_action)],
    task_type: Annotated[str, Form()],
    sample_size: Annotated[int, Form()],
    candidate_model_ids: Annotated[list[str] | None, Form()] = None,
    strategy: Annotated[str, Form()] = "random",
    source: Annotated[str, Form()] = "traffic",
    sample_unit: Annotated[str, Form()] = "request",
    prefer_disliked: Annotated[str, Form()] = "",
    seed: Annotated[str, Form()] = "",
    max_requests: Annotated[str, Form()] = "",
    max_cost_usd: Annotated[str, Form()] = "",
    min_pass_rate_pct: Annotated[str, Form()] = "",
    max_error_rate_pct: Annotated[str, Form()] = "",
    min_quality: Annotated[str, Form()] = "",
    max_p95_ms: Annotated[str, Form()] = "",
    judge_model_id: Annotated[str, Form()] = "",
    judge_pairwise: Annotated[str, Form()] = "",
    idem: Annotated[str, Form()] = "",
) -> Response:
    if not candidate_model_ids:
        raise ApiError(422, "No candidates", "Tick at least one model to compare.")
    pid = await ctx.project_id()
    thresholds: dict[str, Any] = {}
    if optional_text(min_pass_rate_pct):
        thresholds["min_pass_rate"] = float(number(min_pass_rate_pct)) / 100
    if optional_text(max_error_rate_pct):
        thresholds["max_error_rate"] = float(number(max_error_rate_pct)) / 100
    if optional_text(min_quality):
        thresholds["min_quality"] = float(number(min_quality))
    if optional_text(max_p95_ms):
        thresholds["max_p95_ms"] = optional_int(max_p95_ms)
    body: dict[str, Any] = {
        "task_type": task_type,
        "candidate_model_ids": candidate_model_ids,
        "sample_size": sample_size,
        "strategy": strategy,
        "source": source,
        "sample_unit": sample_unit,
        "prefer_disliked": checked(prefer_disliked),
        "seed": optional_int(seed),
        "max_requests": optional_int(max_requests),
        "max_cost_usd": optional_text(max_cost_usd) and number(max_cost_usd),
        "thresholds": thresholds,
        "judge_model_id": optional_text(judge_model_id),
        "judge_pairwise": checked(judge_pairwise),
    }
    run = await ctx.api.send(
        "POST",
        f"/projects/{pid}/calibration-runs",
        body,
        idempotency_key=optional_text(idem),
    )
    await ctx.flash("ok", "The calibration started.")
    return ctx.redirect(f"/runs/{run['id']}")


async def _detail(ctx: Ctx, run_id: str) -> tuple[str, dict[str, Any]]:
    pid = await ctx.project_id()
    run = await ctx.api.get(f"/projects/{pid}/calibration-runs/{run_id}")
    return pid, run


@router.get("/runs/{run_id}")
async def run_detail(ctx: Annotated[Ctx, Depends(page)], run_id: str) -> Response:
    pid, run = await _detail(ctx, run_id)
    models = await ctx.api.get("/models")
    names = {m["id"]: m["name"] for m in models}
    aggregates: list[dict[str, Any]] = []
    recommendation: dict[str, Any] | None = None
    agreement: dict[str, Any] | None = None
    evaluation_done = (run.get("evaluation") or {}).get("status") == "done"
    if evaluation_done:
        base = f"/projects/{pid}/calibration-runs/{run_id}"
        aggregates = (await ctx.api.get(f"{base}/aggregates"))["items"]
        recommendation = await ctx.api.get(f"{base}/recommendation")
        if run.get("judge_model_id"):
            try:
                agreement = await ctx.api.get(f"{base}/judge-agreement")
            except ApiError as exc:
                if exc.status not in (404, 409):
                    raise
    return await ctx.render(
        "run_detail.html",
        page="runs",
        run=run,
        names=names,
        aggregates=aggregates,
        recommendation=recommendation,
        agreement=agreement,
        live=keeps_changing(run),
    )


@router.get("/runs/{run_id}/progress")
async def run_progress(ctx: Annotated[Ctx, Depends(page)], run_id: str) -> Response:
    """The progress block, asked for every few seconds while the run is alive."""
    _, run = await _detail(ctx, run_id)
    response = await ctx.render("partials/run_progress.html", run=run, live=keeps_changing(run))
    if (run.get("evaluation") or {}).get("status") == "done":
        response.headers["HX-Refresh"] = "true"  # numbers are ready: show them
    return response


@router.post("/runs/{run_id}/{command}")
async def steer(ctx: Annotated[Ctx, Depends(admin_action)], run_id: str, command: str) -> Response:
    if command not in ("pause", "resume", "cancel"):
        raise ApiError(404, "Not found", "Unknown command.")
    pid = await ctx.project_id()
    await ctx.api.send("POST", f"/projects/{pid}/calibration-runs/{run_id}/{command}")
    await ctx.flash(
        "ok", {"pause": "Paused.", "resume": "Resumed.", "cancel": "Cancelled."}[command]
    )
    return ctx.redirect(f"/runs/{run_id}")


@router.post("/runs/{run_id}/recommendation/propose")
async def propose(
    ctx: Annotated[Ctx, Depends(admin_action)],
    run_id: str,
    note: Annotated[str, Form()] = "",
) -> Response:
    pid = await ctx.project_id()
    rule = await ctx.api.send(
        "POST",
        f"/projects/{pid}/calibration-runs/{run_id}/recommendation/propose",
        {"note": optional_text(note)},
    )
    await ctx.flash(
        "ok", f"Rule version {rule['version']} was proposed. Nothing is live until you activate it."
    )
    return ctx.redirect(f"/rules#{rule['task_type']}")
