"""The matrix of models x task types, and the examples behind a cell."""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import Response

from app.api_client import ApiError
from app.payloads import answer_text, prompt_text
from app.ui import Ctx, admin_action, page

router = APIRouter()


def build_matrix(raw: dict[str, Any]) -> dict[str, Any]:
    """The api answer reshaped for the page: one row per model, a cell per task type."""
    cells: dict[tuple[str, str], dict[str, Any]] = {}
    for cell in raw["cells"]:
        cells[(cell["model_id"], cell["task_type"])] = cell
    tasks = [
        {
            "name": c["task_type"],
            "mode": c["mode"],
            "run_id": c["run_id"],
            "primary": c["primary"],
            "version": c["active_version"],
            "criteria": c["criteria"],
            "measured": c["run_id"] is not None,
        }
        for c in raw["columns"]
    ]
    rows = []
    for model in raw["models"]:
        row_cells = {t["name"]: cells.get((model["id"], t["name"])) for t in tasks}
        rows.append({**model, "cells": row_cells, "measured": any(row_cells.values())})
    return {"tasks": tasks, "models": rows}


@router.get("/matrix")
async def matrix(ctx: Annotated[Ctx, Depends(page)]) -> Response:
    pid = await ctx.project_id()
    raw = await ctx.api.get(f"/projects/{pid}/matrix")
    return await ctx.render("matrix.html", page="matrix", matrix=build_matrix(raw))


async def _example(
    ctx: Ctx, pid: str, run_id: str, model_id: str, evaluation: dict[str, Any]
) -> dict[str, Any]:
    """One evaluated answer with the question, the reference answer and the candidate's answer."""
    request_id = evaluation["request_id"]
    results, request = await asyncio.gather(
        ctx.api.get(
            f"/projects/{pid}/calibration-runs/{run_id}/results",
            model_id=model_id,
            request_id=request_id,
            limit=1,
        ),
        ctx.api.get(f"/projects/{pid}/requests/{request_id}"),
    )
    result = results["items"][0] if results["items"] else None
    return {
        "evaluation": evaluation,
        "result_id": result["id"] if result else evaluation["result_id"],
        "prompt": prompt_text(request.get("request_payload")),
        "reference": answer_text(request.get("response_payload")),
        "candidate": answer_text(result.get("response_payload")) if result else "",
        "error": result.get("error_code") if result else None,
    }


@router.get("/matrix/cell")
async def cell(
    ctx: Annotated[Ctx, Depends(page)],
    run_id: Annotated[str, Query()],
    model_id: Annotated[str, Query()],
    task_type: Annotated[str, Query()],
) -> Response:
    pid = await ctx.project_id()
    base = f"/projects/{pid}/calibration-runs/{run_id}"
    aggregates, recommendation, failed, passed = await asyncio.gather(
        ctx.api.get(f"{base}/aggregates"),
        ctx.api.get(f"{base}/recommendation"),
        ctx.api.get(f"{base}/evaluations", model_id=model_id, passed="false", limit=4),
        ctx.api.get(f"{base}/evaluations", model_id=model_id, passed="true", limit=2),
    )
    aggregate = next((a for a in aggregates["items"] if a["model_id"] == model_id), None)
    verdict = next((v for v in recommendation["verdicts"] if v["model_id"] == model_id), None)
    if aggregate is None:
        raise ApiError(404, "No numbers", "This model has no results in the run.")
    examples = await asyncio.gather(
        *[_example(ctx, pid, run_id, model_id, e) for e in failed["items"] + passed["items"]]
    )
    return await ctx.render(
        "partials/matrix_cell.html",
        aggregate=aggregate,
        verdict=verdict,
        criteria=recommendation["criteria"],
        examples=examples,
        run_id=run_id,
        task_type=task_type,
        model_name=verdict["name"] if verdict else model_id,
    )


@router.post("/matrix/verdict")
async def save_verdict(
    ctx: Annotated[Ctx, Depends(admin_action)],
    run_id: Annotated[str, Form()],
    result_id: Annotated[str, Form()],
    verdict: Annotated[str, Form()],
) -> Response:
    """A person's verdict on one answer; it outranks the judge."""
    if verdict not in ("pass", "fail"):
        raise ApiError(422, "Invalid verdict", "The verdict must be pass or fail.")
    pid = await ctx.project_id()
    saved = await ctx.api.send(
        "PUT",
        f"/projects/{pid}/calibration-runs/{run_id}/results/{result_id}/human-verdict",
        {"verdict": verdict},
    )
    return await ctx.render("partials/verdict_saved.html", verdict=saved["verdict"])
