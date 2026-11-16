"""Chart.js configurations built on the server (the browser only draws them)."""

from decimal import Decimal
from typing import Any

PALETTE = ["#2f6fed", "#0ea5a4", "#f59e0b", "#e11d48", "#7c3aed", "#16a34a", "#64748b", "#ea580c"]

Series = list[tuple[str, list[float], str]]


def num(value: Any) -> float:
    return float(Decimal(str(value))) if value not in (None, "") else 0.0


def line(labels: list[str], series: Series, *, money: bool = False) -> dict[str, Any]:
    """series: (label, values, colour)."""
    return {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": label,
                    "data": values,
                    "borderColor": colour,
                    "backgroundColor": colour,
                    "borderWidth": 2,
                    "pointRadius": 2,
                    "tension": 0.25,
                }
                for label, values, colour in series
            ],
        },
        "options": {
            "interaction": {"mode": "index", "intersect": False},
            "scales": {"y": {"beginAtZero": True, "title": {"display": money, "text": "USD"}}},
        },
    }


def bars(labels: list[str], series: Series, *, stacked: bool = False) -> dict[str, Any]:
    return {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {"label": label, "data": values, "backgroundColor": colour}
                for label, values, colour in series
            ],
        },
        "options": {
            "scales": {"x": {"stacked": stacked}, "y": {"stacked": stacked, "beginAtZero": True}}
        },
    }


def doughnut(labels: list[str], values: list[float]) -> dict[str, Any]:
    colours = [PALETTE[i % len(PALETTE)] for i in range(len(values))]
    return {
        "type": "doughnut",
        "data": {"labels": labels, "datasets": [{"data": values, "backgroundColor": colours}]},
        "options": {"plugins": {"legend": {"position": "right"}}},
    }
