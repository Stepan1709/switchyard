"""The dashboards and alert rules may only use metrics that the code really exports."""

import json
import re
from pathlib import Path

from prometheus_client import REGISTRY

from app.core import metrics
from app.modules.gateway import budget, telemetry  # noqa: F401
from app.worker import metrics_job  # noqa: F401

DEPLOY = Path(__file__).resolve().parents[4] / "deploy"
NAME = re.compile(r"\bswitchyard_[a-z0-9_]+")
SUFFIXES = ("", "_total", "_bucket", "_sum", "_count", "_created")


def exported() -> set[str]:
    metrics.register_extra_collectors()
    names: set[str] = set()
    for family in REGISTRY.collect():
        for suffix in SUFFIXES:
            names.add(family.name + suffix)
    return names


def dashboard_expressions() -> list[tuple[str, str]]:
    found = []
    for path in sorted((DEPLOY / "grafana" / "dashboards").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for panel in data["panels"]:
            for target in panel["targets"]:
                found.append((f"{path.name}: {panel['title']}", target["expr"]))
    return found


def test_every_dashboard_panel_uses_a_metric_the_code_exports():
    known = exported()
    expressions = dashboard_expressions()
    assert len(expressions) >= 20  # the two dashboards really were read
    unknown = [
        (where, name)
        for where, expr in expressions
        for name in NAME.findall(expr)
        if name not in known
    ]
    assert unknown == []


def test_every_alert_rule_uses_a_metric_the_code_exports():
    known = exported()
    text = (DEPLOY / "prometheus" / "alerts.yml").read_text(encoding="utf-8")
    used = set(NAME.findall(text))
    assert used, "no metric found in the alert rules"
    assert used <= known, used - known


def test_the_two_required_alerts_exist():
    text = (DEPLOY / "prometheus" / "alerts.yml").read_text(encoding="utf-8")
    assert "alert: DeadLetterQueueGrowing" in text
    assert "alert: ProviderErrorRateHigh" in text


def test_prometheus_scrapes_the_three_processes():
    text = (DEPLOY / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
    for target in ("api:8001", "gateway:8000", "worker:8004"):
        assert target in text
