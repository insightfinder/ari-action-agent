"""`set_resources`: CPU/memory requests and/or limits of one container — only the fields the request states,
each clamped to what the best eligible node can give. A lowered memory value must stay at least 1.2 x the
container's current usage (`decrease_check`, shared with `resize_pod`)."""

import math

from ..stepkit import refusal
from ..tools import capacity, governance, ownership, policy, quantities
from .base import DeploymentAction, Operation
from .restart_rollout import rollout_effects

_FIELDS = (("requests", "cpu"), ("requests", "memory"), ("limits", "cpu"), ("limits", "memory"))


def _same_quantity(resource: str, have, want) -> bool:
    try:
        return have is not None and quantities.to_units(resource, have) == quantities.to_units(resource, want)
    except (ValueError, TypeError):
        return False


def surge_pods(dep: dict) -> int:
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

    async def load_state(self, ctx, target):
        state = await super().load_state(ctx, target)
        dep = state["deployment"]
        pods = await ctx.tool("list_pods")(dep["namespace"], dep.get("label_selector") or dep.get("selector") or None, True)
        own = [f"{p['namespace']}/{p['name']}" for p in pods if not p.get("deleting") and governance.deployment_of(p) == dep["name"]]
        state["usage"] = await own_pod_usage(ctx, dep["namespace"], own)
        return state

    def plan(self, ctx, state, change, env):
        return _plan_resources(ctx, state["deployment"], change, env["nodes"], env["pods"], env["headroom"], state.get("usage"))

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
        usage_changed = decrease_drift(container, then, proposal.get("resources") or {}, state.get("usage"))
        if usage_changed:
            changes.append(usage_changed)
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

    def written_fields(self, action, proposal):
        then = (proposal["snapshot"].get("container_resources") or {}).get(action["container"])
        paths = changed_resource_paths(["f:spec", "f:template", "f:spec"], action["container"], action.get("resources"), then)
        return {"kind": "Deployment", "namespace": action.get("namespace"), "name": action["deployment"], "paths": paths}

    def verify_target(self, action):
        return {"kind": "resources", "container": action["container"], "resources": action["resources"]}

    def base_tier(self, facts):
        return "medium"  # every resource change restarts pods


def changed_resource_paths(prefix: list[str], container: str, approved: dict | None, then: dict | None) -> list[list[str]]:
    """The `ownership` paths of the approved requests/limits of `container` (under `prefix`, the pod spec) that
    differ from what the snapshot held (`then`, a container's resources view)."""
    then = then or {}
    return [
        [*prefix, "f:containers", ownership.keyed(container), "f:resources", f"f:{side}", f"f:{resource}"]
        for side, values in (approved or {}).items()
        for resource, value in (values or {}).items()
        if not _same_quantity(resource, (then.get(side) or {}).get(resource), value)
    ]


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


def decreases(current: dict, new: dict) -> list[tuple[str, str, int | None, int]]:
    """`[(side, resource, old units or None, new units)]` for every field of `new` (`{(side, resource): units}`)
    that leaves the container with less than it has: a lower value, a limit where there was none (unbounded
    before), or a request below the limit it defaulted to."""
    out = []
    for (side, resource), amount in new.items():
        limit = (current.get("limits") or {}).get(resource)
        old = (current.get(side) or {}).get(resource)
        if old is None and side == "requests":
            old = limit  # a request left out defaults to the limit
        old_units = quantities.to_units(resource, old) if old is not None else None
        if old_units is None and side == "requests":
            continue  # no request and no limit: nothing was reserved, so none is taken away
        if old_units is None or amount < old_units:
            out.append((side, resource, old_units, amount))
    return out


def peak_usage(usage: dict | None, container: str) -> tuple[dict, str | None]:
    """`({"cpu": millicores or None, "memory": bytes or None, "pods": n}, gap or None)`: the highest usage
    metrics.k8s.io reports now for `container` across `usage["pods"]` (`namespace/name` keys). It is a current
    sample, not the peak over time."""
    usage = usage or {}
    if usage.get("gap"):
        return {"cpu": None, "memory": None, "pods": 0}, usage["gap"]
    by_pod = usage.get("usage") or {}
    peak: dict = {"cpu": None, "memory": None, "pods": 0}
    for key in usage.get("pods") or []:
        sample = (by_pod.get(key) or {}).get(container)
        if not sample:
            continue
        peak["pods"] += 1
        for resource in ("cpu", "memory"):
            try:
                value = quantities.to_units(resource, sample[resource]) if sample.get(resource) else None
            except (ValueError, TypeError):
                value = None
            if value is not None and (peak[resource] is None or value > peak[resource]):
                peak[resource] = value
    if not peak["pods"]:
        return peak, f"metrics.k8s.io reports no usage for container {container} in {len(usage.get('pods') or [])} pod(s)"
    return peak, None


def _usage_text(resource: str, amount: int) -> str:
    """A usage sample as people read it: memory rounded up to whole Mi (metrics-server reports Ki)."""
    if resource == "memory":
        return quantities.format_memory(math.ceil(amount / quantities.MI) * quantities.MI)
    return quantities.format_cpu(amount)


def decrease_check(container: str, current: dict, new: dict, usage: dict | None) -> tuple[list[dict], str | None]:
    """`(risks, refusal reason or None)` for lowering a container's CPU/memory (user decision 2026-11-26).

    A memory request or limit is lowered only when the container's current memory usage can be read and the new
    value is at least `policy.DECREASE_USAGE_MARGIN` times it: a memory limit below what the process uses
    OOM-kills it, a rollout of such pods stalls (or, under Recreate, leaves nothing running), and the agent does
    not undo a failed change. Any decrease is a `high` risk. Also checked again when the approval arrives."""
    lowered = decreases(current, new)
    if not lowered:
        return [], None
    peak, gap = peak_usage(usage, container)
    margin = policy.DECREASE_USAGE_MARGIN
    for side, resource, _old, amount in lowered:
        if resource != "memory":
            continue
        if peak["memory"] is None:
            return [], (
                f"This lowers the memory {side[:-1]} of container {container}, which is proposed only when the "
                f"container's current memory usage can be read, and it could not ({gap or 'no memory sample'}). "
                "Nothing was proposed."
            )
        floor = math.ceil(peak["memory"] * margin / quantities.MI) * quantities.MI
        if amount < floor:
            return [], (
                f"A memory {side[:-1]} of {quantities.format_memory(amount)} would be below {margin:g} x the current memory usage of "
                f"container {container} ({_usage_text('memory', peak['memory'])}, the highest of {peak['pods']} pod(s)): "
                f"it could be OOM-killed. The lowest value proposed is {quantities.format_memory(floor)}. Nothing was proposed."
            )
    used = ", ".join(
        f"{r} {_usage_text(r, peak[r])}" for r in ("cpu", "memory") if peak[r] is not None
    ) or "unknown"
    fields = ", ".join(
        f"{side}.{resource} {quantities.format_units(resource, old) if old is not None else 'unset (no limit)'} -> "
        f"{quantities.format_units(resource, amount)}"
        for side, resource, old, amount in lowered
    )
    message = (
        f"This lowers {fields} of container {container}. Current usage: {used} (highest of {peak['pods']} pod(s), a "
        "current sample, not the peak over time). Less memory than a load peak needs OOM-kills the container; a lower "
        "CPU limit throttles it, which can fail its probes. The agent reports a failed change but does not undo it."
    )
    cpu_limit = next((amount for side, r, _o, amount in lowered if side == "limits" and r == "cpu"), None)
    if cpu_limit is not None and peak["cpu"] is not None and cpu_limit < peak["cpu"]:
        message += f" The new CPU limit is below the current CPU usage ({_usage_text('cpu', peak['cpu'])})."
    return [{"level": "high", "message": message}], None


async def own_pod_usage(ctx, namespace: str, pod_keys: list[str]) -> dict:
    """What `decrease_check` reads: metrics.k8s.io now (or its gap) for these `namespace/name` pods."""
    return {"pods": pod_keys, **(await ctx.tool("get_pod_metrics")(namespace))}


def _units_of(resources: dict) -> dict:
    """`{(side, resource): units}` of a `{requests: {...}, limits: {...}}` map (an approved proposal's values)."""
    out = {}
    for side in ("requests", "limits"):
        for resource, value in ((resources or {}).get(side) or {}).items():
            if resource in ("cpu", "memory"):
                out[(side, resource)] = quantities.to_units(resource, value)
    return out


def decrease_drift(container: str, before: dict | None, resources: dict, usage: dict | None) -> str | None:
    """At approval: why the approved values (`resources`, against `before`, the container's values at proposal
    time) would now be refused by `decrease_check`, or None."""
    _risks, refused = decrease_check(container, before or {}, _units_of(resources), usage)
    return f"usage changed: {refused.removesuffix(' Nothing was proposed.')}" if refused else None


def _plan_resources(ctx, dep, change, nodes, pods, headroom, usage=None):
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
    risks, refused_why = decrease_check(name, current, applied, usage)
    if refused_why:
        return None, refusal(refused_why, clamped=clamped)
    # Recreate stops every pod before the new ones start: the same `high` risk as a restart of it.
    risks.extend(rollout_effects(dep)[0])

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
        "rolls_pods": True,
        "roll_message": "Changing CPU/memory triggers a rolling restart of every pod of this Deployment.",
        "risks": risks,
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
