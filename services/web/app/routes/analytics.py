"""Overview, usage and idle servers, feedback."""

import calendar
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query
from fastapi.responses import Response

from app import charts
from app.api_client import ApiError
from app.ui import Ctx, admin_action, page

router = APIRouter()
MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_bounds(month: str | None) -> tuple[str, date, date]:
    """The month as 'YYYY-MM' and its first and last day (up to today for the current one)."""
    today = datetime.now(UTC).date()
    label = month or today.strftime("%Y-%m")
    year, mon = int(label[:4]), int(label[5:7])
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return label, first, min(last, today) if first <= today else last


def shift_month(label: str, delta: int) -> str:
    year, mon = int(label[:4]), int(label[5:7])
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12}-{index % 12 + 1:02d}"


@router.get("/overview")
async def overview(
    ctx: Annotated[Ctx, Depends(page)], month: Annotated[str | None, Query(pattern=MONTH)] = None
) -> Response:
    pid = await ctx.project_id()
    label, first, last = month_bounds(month)
    data = await ctx.api.get(f"/projects/{pid}/analytics/overview", month=label)
    daily = await ctx.api.get(
        f"/projects/{pid}/analytics/usage",
        group_by="day",
        since=first.isoformat(),
        until=last.isoformat(),
    )
    rows = daily["rows"]
    cost_chart = charts.line(
        [r["day"] for r in rows],
        [
            ("Cost", [charts.num(r["cost_usd"]) for r in rows], "#2f6fed"),
            (
                "Same tokens on the reference",
                [charts.num(r["reference_cost_usd"]) for r in rows],
                "#94a3b8",
            ),
        ],
        money=True,
    )
    share_chart = charts.doughnut(
        [m["name"] for m in data["models"]], [m["requests"] for m in data["models"]]
    )
    today = datetime.now(UTC).strftime("%Y-%m")
    return await ctx.render(
        "overview.html",
        page="overview",
        data=data,
        month=label,
        prev_month=shift_month(label, -1),
        next_month=shift_month(label, 1) if label < today else None,
        cost_chart=cost_chart if rows else None,
        share_chart=share_chart if data["models"] else None,
    )


@router.get("/usage")
async def usage(
    ctx: Annotated[Ctx, Depends(page)],
    month: Annotated[str | None, Query(pattern=MONTH)] = None,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
) -> Response:
    pid = await ctx.project_id()
    label, _, _ = month_bounds(month)
    until = datetime.now(UTC).date()
    since = until - timedelta(days=days - 1)
    base = f"/projects/{pid}/analytics/usage"
    window = {"since": since.isoformat(), "until": until.isoformat()}
    daily = (await ctx.api.get(base, group_by="day", **window))["rows"]
    by_model = (await ctx.api.get(base, group_by="model", **window))["rows"]
    by_task = (await ctx.api.get(base, group_by="task_type", **window))["rows"]
    servers = (await ctx.api.get("/analytics/servers", month=label))["servers"]
    labels = [r["day"] for r in daily]
    cost_chart = charts.line(
        labels,
        [
            ("Cost", [charts.num(r["cost_usd"]) for r in daily], "#2f6fed"),
            ("Reference", [charts.num(r["reference_cost_usd"]) for r in daily], "#94a3b8"),
        ],
        money=True,
    )
    requests_chart = charts.bars(
        labels,
        [
            ("Successful", [r["ok"] for r in daily], "#16a34a"),
            ("Errors", [r["errors"] for r in daily], "#e11d48"),
        ],
        stacked=True,
    )
    model_chart = charts.doughnut(
        [r.get("model") or "?" for r in by_model], [r["requests"] for r in by_model]
    )
    return await ctx.render(
        "usage.html",
        page="usage",
        days=days,
        month=label,
        daily=daily,
        by_model=by_model,
        by_task=by_task,
        servers=servers,
        cost_chart=cost_chart if daily else None,
        requests_chart=requests_chart if daily else None,
        model_chart=model_chart if by_model else None,
    )


@router.post("/usage/recompute")
async def recompute(
    ctx: Annotated[Ctx, Depends(admin_action)], days: Annotated[int, Form()] = 7
) -> Response:
    pid = await ctx.project_id()
    days = max(1, min(days, 90))
    until = datetime.now(UTC).date()
    since = until - timedelta(days=days - 1)
    await ctx.api.send(
        "POST",
        f"/projects/{pid}/analytics/recompute",
        {"since": since.isoformat(), "until": until.isoformat()},
    )
    await ctx.flash("ok", f"Summaries for the last {days} days were recalculated.")
    return ctx.redirect("/usage")


async def _health(ctx: Ctx, pid: str, task_types: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rule-health signal of every task type that has an active rule."""
    found: list[dict[str, Any]] = []
    for task in task_types:
        try:
            found.append(
                await ctx.api.get(f"/projects/{pid}/task-types/{task['name']}/rule-health")
            )
        except ApiError as exc:
            if exc.status != 409:  # 409 = this task type has no active rule
                raise
    return found


@router.get("/feedback")
async def feedback(
    ctx: Annotated[Ctx, Depends(page)],
    rating: Annotated[int | None, Query(ge=-1, le=1)] = None,
) -> Response:
    pid = await ctx.project_id()
    report = await ctx.api.get(f"/projects/{pid}/analytics/feedback")
    items = await ctx.api.get(f"/projects/{pid}/feedback", rating=rating or "", limit=50)
    task_types = await ctx.api.get(f"/projects/{pid}/task-types")
    return await ctx.render(
        "feedback.html",
        page="feedback",
        report=report,
        items=items["items"],
        rating=rating,
        health=await _health(ctx, pid, task_types),
    )
