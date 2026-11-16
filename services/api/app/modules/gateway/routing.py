"""Which model answers a request. A pure function of the snapshot, the project, the task type
and the `model` the client asked for.

The client keeps its old model name (usually the reference's) and only changes `base_url`, so
the name cannot mean "use exactly this model" in general. The rules:

* a name that is registered and is NOT the project's reference pins that model: it answers
  alone, no rules, no fallbacks (a deliberate choice of the client);
* anything else (the reference's own name, `auto`, no name, a name the registry does not
  know) means "let the gateway decide":
    - task type in `route` mode with an active rule: the rule's chain;
    - task type in `shadow` mode: the reference answers and a share of requests is copied
      to the candidates in the background;
    - otherwise: the reference.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass

from app.core.snapshot import ProjectInfo, RouteTarget, Snapshot

AUTO = "auto"


@dataclass(frozen=True)
class Plan:
    mode: str  # record | shadow | route | pinned
    chain: tuple[RouteTarget, ...]  # tried in order, only before the first byte is sent
    shadow: tuple[RouteTarget, ...] = ()
    shadow_rate: float = 0.0
    validation_schema: dict[str, object] | None = None


def plan_for(
    snapshot: Snapshot, project: ProjectInfo, task_type: str | None, requested_model: str | None
) -> Plan | None:
    """The plan, or None when nothing can answer (no reference model and no rule)."""
    reference = project.reference
    if requested_model and requested_model != AUTO:
        pinned_id = snapshot.target_ids_by_name.get(requested_model)
        is_reference_name = reference is not None and reference.model_name == requested_model
        if pinned_id is not None and not is_reference_name:
            return Plan("pinned", (snapshot.targets[pinned_id],))

    route = snapshot.route_of(project.project_id, task_type)
    if route is not None and route.mode == "route" and route.chain:
        return Plan("route", route.chain, validation_schema=route.validation_schema)
    if reference is None:
        return None
    if route is not None and route.mode == "shadow" and route.shadow and route.shadow_rate > 0:
        return Plan("shadow", (reference,), shadow=route.shadow, shadow_rate=route.shadow_rate)
    return Plan("record", (reference,))


def shadow_targets(
    plan: Plan, roll: Callable[[], float] = random.random
) -> tuple[RouteTarget, ...]:
    """The models that get a copy of this request: all of them or none, by `shadow_rate`."""
    if plan.mode != "shadow" or not plan.shadow:
        return ()
    return plan.shadow if roll() < plan.shadow_rate else ()
