"""`set_resources`: CPU/memory requests and/or limits of one container — only the fields the request states,
each clamped to what the best eligible node can give."""

from ..stepkit import refusal
from ..tools import capacity, policy, quantities
from .base import DeploymentAction, Operation

_FIELDS = (("requests", "cpu"), ("requests", "memory"), ("limits", "cpu"), ("limits", "memory"))


def _same_quantity(resource: str, have, want) -> bool:
    try:
        return have is not None and quantities.to_units(resource, have) == quantities.to_units(resource, want)
    except (ValueError, TypeError):
        return False


def surge_pods(dep: dict) -> int:
    import math

    replicas = max(dep["replicas"] or 0, 1)
    surge = str(dep["strategy"].get("max_surge", "25%"))
    if dep["strategy"].get("type") == "Recreate":
        return replicas
    if surge.endswith("%"):
        return max(1, math.ceil(replicas * float(surge[:-1]) / 100))
    return max(1, int(surge))


_surge_pods = surge_pods


class SetResources(DeploymentAction):
    name = "set_resources"
    prompt_line = (
        "change CPU and/or memory requests and/or limits of one container. Report only the fields the request names."
    )
    needs_capacity = True

    def plan(self, ctx, state, change, env):
        return _plan_resources(ctx, state["deployment"], change, env["nodes"], env["pods"], env["headroom"])

    def operations(self, action):
        resources = {
            side: dict(values)
            for side, values in (action.get("resources") or {}).items()
            if side in ("requests", "limits") and values
        }
        if not resources:
            raise ValueError("set_resources needs at least one requests/limits value")
        # Strategic merge: containers merge by name and `resources` maps merge key by key, so only the named
        # fields of the named container change.
        return [
            Operation(
                verb="patch",
                kind="Deployment",
                resource="Deployment",
                name=action["deployment"],
                namespace=action.get("namespace"),
                body={"spec": {"template": {"spec": {"containers": [{"name": action["container"], "resources": resources}]}}}},
                patch_type="strategic",
            )
        ]

    def summarize(self, op, raw, action):
        containers = (((raw.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
        target = next((c for c in containers if c.get("name") == action.get("container")), {})
        res = target.get("resources") or {}
        return {
            **super().summarize(op, raw, action),
            "container": action.get("container"),
            "resources": {
                side: {k: str(v) for k, v in (res.get(side) or {}).items() if k in ("cpu", "memory")}
                for side in ("requests", "limits")
            },
        }

    def patch_action(self, proposal):
        return {
            **{k: proposal.get(k) for k in ("action", "namespace", "deployment")},
            "container": proposal["container"],
            "resources": proposal["resources"],
        }

    def drift(self, proposal, state):
        changes = super().drift(proposal, state)
        container = proposal.get("container")
        now = next((c["resources"] for c in state["deployment"]["template"]["containers"] if c["name"] == container), None)
        then = (proposal["snapshot"].get("container_resources") or {}).get(container)
        if now != then:
            changes.append(f"container {container} resources changed since the proposal")
        return changes

    def at_target(self, action, state):
        current = next((c["resources"] for c in state["deployment"]["template"]["containers"] if c["name"] == action["container"]), None)
        if current is None:
            return False
        return all(
            _same_quantity(resource, (current.get(side) or {}).get(resource), value)
            for side, values in (action.get("resources") or {}).items()
            for resource, value in (values or {}).items()
        )

    def verify_target(self, action):
        return {"kind": "resources", "container": action["container"], "resources": action["resources"]}

    def base_tier(self, facts):
        return "medium"  # every resource change restarts pods


def pick_container(change: dict, names) -> tuple[str | None, dict | None]:
    """The container the request is about: the one it names, or the only one."""
    names = sorted(names)
    name = change.get("container")
    if not name and len(names) == 1:
        name = names[0]
    if name not in names:
        return None, refusal("Which container to change is not clear; name one of: " + ", ".join(names), containers=names)
    return name, None


def requested_values(ctx, change: dict) -> tuple[dict | None, dict | None]:
    """`{(side, resource): units}` for every CPU/memory field the request states with a quoted value, or a
    refusal. Shared by `set_resources` and `resize_pod`, so both accept exactly the same requests."""
    requested_raw = change.get("resources") or {}
    requested: dict = {}
    for side, resource in _FIELDS:
        entry = (requested_raw.get(side) or {}).get(resource)
        if not entry:
            continue
        if isinstance(entry, str):
            entry = {"value": entry, "quote": None}
        problem = quantities.memory_request_problem(entry.get("value")) if resource == "memory" else None
        if problem:
            return None, refusal(f"{resource} {side}: {problem}")
        if not policy.quote_in_prompt(entry.get("quote"), ctx.prompt, entry.get("value"), resource):
            return None, refusal(
                f"{resource} {side} was not explicitly requested with a value; CPU and memory are changed only "
                "when the request states the value for each field."
            )
        try:
            amount = quantities.to_units(resource, entry.get("value"))
        except (ValueError, TypeError):
            return None, refusal(f"{resource} {side} value {entry.get('value')!r} is not a valid Kubernetes quantity.")
        if amount <= 0:
            return None, refusal(f"{resource} {side} must be greater than zero.")
        requested[(side, resource)] = amount
    if not requested:
        return None, refusal("No CPU or memory value was requested.")
    return requested, None


def request_over_limit(resource: str, current: dict, applied: dict) -> str | None:
    """Why the result would have a request above its limit (new or current values), or None."""
    req = applied.get(("requests", resource))
    lim = applied.get(("limits", resource))
    cur_req = (current.get("requests") or {}).get(resource)
    cur_lim = (current.get("limits") or {}).get(resource)
    eff_req = req if req is not None else (quantities.to_units(resource, cur_req) if cur_req else None)
    eff_lim = lim if lim is not None else (quantities.to_units(resource, cur_lim) if cur_lim else None)
    if eff_req is not None and eff_lim is not None and eff_req > eff_lim:
        which = "new request" if req is not None else "current request"
        against = "new limit" if lim is not None else "current limit"
        return (
            f"{resource}: the {which} ({quantities.format_units(resource, eff_req)}) would exceed the {against} "
            f"({quantities.format_units(resource, eff_lim)}). Request both together, or a value that fits."
        )
    return None


def _plan_resources(ctx, dep, change, nodes, pods, headroom):
    containers = {c["name"]: c for c in dep["template"]["containers"]}
    name, refused = pick_container(change, containers)
    if refused:
        return None, refused
    requested, refused = requested_values(ctx, change)
    if refused:
        return None, refused

    wanted: dict = {}
    for (_side, resource), amount in requested.items():
        wanted[resource] = max(wanted.get(resource, 0), amount)
    cap = capacity.resource_cap(nodes, pods, dep["template"], headroom, container=name, wanted=wanted)
    if cap is None:
        return None, refusal("No Ready, schedulable node matches this pod's nodeSelector, affinity and tolerations.")

    applied: dict = {}
    clamped = []
    for (side, resource), amount in requested.items():
        limit = cap[resource]["cap"]
        if limit <= 0:
            return None, refusal(
                f"No eligible node can give container {name} any {resource}: on the best node "
                f"({cap[resource]['node']}), what is left after DaemonSets, the headroom and the pod's other "
                f"containers' requests ({cap[resource]['other_containers_requests']}) is nothing. Nothing was proposed."
            )
        final = min(amount, limit)
        if resource == "cpu":
            final = int(final)
        else:
            final = quantities.floor_memory_to_mi(final) if final != amount else final
        if final < amount:
            clamped.append(
                {
                    "field": f"{side}.{resource}",
                    "requested": quantities.format_units(resource, amount),
                    "applied": quantities.format_units(resource, final),
                    "cap": quantities.format_units(resource, limit),
                    "basis": (
                        f"largest eligible node {cap[resource]['node']} ({cap[resource]['instance_type']}): allocatable "
                        f"{cap[resource]['allocatable']} minus DaemonSet requests {cap[resource]['daemonset_requests']}, "
                        f"times (1 - headroom {cap['headroom']:.0%}), minus the pod's other containers' requests "
                        f"{cap[resource]['other_containers_requests']}"
                    ),
                }
            )
        applied[(side, resource)] = final

    current = containers[name]["resources"]
    for resource in ("cpu", "memory"):
        problem = request_over_limit(resource, current, applied)
        if problem:
            return None, refusal(problem, clamped=clamped)

    patch_resources: dict = {}
    for (side, resource), amount in applied.items():
        patch_resources.setdefault(side, {})[resource] = quantities.format_units(resource, amount)
    requested_fmt = {f"{s}.{r}": quantities.format_units(r, a) for (s, r), a in requested.items()}
    applied_fmt = {f"{s}.{r}": quantities.format_units(r, a) for (s, r), a in applied.items()}
    before = {f"{s}.{r}": current.get(s, {}).get(r) for (s, r) in applied}
    if all(before.get(k) and quantities.to_units(k.split(".")[1], before[k]) == applied[tuple(k.split("."))] for k in before):
        return None, refusal("The container already has those values; nothing to change.")

    action = {
        "action": "set_resources",
        "namespace": dep["namespace"],
        "deployment": dep["name"],
        "container": name,
        "resources": patch_resources,
    }
    new_requests = {r: patch_resources.get("requests", {}).get(r) for r in ("cpu", "memory") if patch_resources.get("requests", {}).get(r)}
    raised = any(
        applied[(s, r)] > quantities.to_units(r, current.get(s, {}).get(r)) if current.get(s, {}).get(r) else True
        for (s, r) in applied
        if s == "requests"
    )
    facts = {
        "before": before,
        "after": applied_fmt,
        "requested": requested_fmt,
        "clamped": clamped,
        "cap": {
            "cpu": quantities.format_cpu(cap["cpu"]["cap"]),
            "memory": quantities.format_memory(cap["memory"]["cap"]),
            "headroom": cap["headroom"],
            "candidate_nodes": cap["candidate_nodes"],
            "note": cap["zero_node_groups_note"],
        },
        "scale_down": False,
        "high_replicas": False,
        "new_pods": _surge_pods(dep) if raised else 0,
        "per_pod_override": {"container": name, "requests": new_requests},
        "changed_object": {"kind": "Deployment", "name": dep["name"], "labels": dep["labels"], "annotations": dep["annotations"]},
        "rendered_changes": [
            {
                "object": f"Deployment {dep['namespace']}/{dep['name']} container {name}",
                "field": f"resources.{k}",
                "from": before.get(k),
                "to": v,
            }
            for k, v in applied_fmt.items()
        ],
        "action_name": f"Set {', '.join(f'{k} {v}' for k, v in applied_fmt.items())} on {dep['namespace']}/{dep['name']} ({name})",
    }
    return (action, facts), None
