"""Works out exactly what would change, checks it, dry-runs it, and stops.

The model's only job here is to read the request and say which change it
asks for, quoting the words it took each number from. Everything else is
code: whether the Deployment is managed by KEDA/HPA (and so may only be
changed through its autoscaler, and only on explicit request), the
replica floor, the node-capacity cap for CPU/memory, the capacity and
Cluster Autoscaler outlook, the pre-flight risks, the risk tier, the
ownership/drift notice and the persistence hint. The resulting
`pending_action` is built here from those checked values, never taken as
JSON from the model.

This capability cannot change the cluster: its tools list in
configs/k8s/capabilities.yaml has only reads and `dry_run_patch`, which is
always a server-side dry run. The real write is reachable only from
`apply_approved_change`, and only after a person approves this proposal.
"""

import math
import uuid
from datetime import datetime, timezone

from common.capabilities_base import StepContext
from common.llm_json import extract

from ..stepkit import finish, options, refusal, resolved_target
from ..tools import capacity, governance, persistence, policy, quantities

_SENTINEL = "CHANGE_JSON:"
_SYSTEM = (
    "You read a request to change a Kubernetes Deployment and report exactly what it asks for. You never "
    "invent or round a number. For every number you report you also give the exact words of the request "
    "it came from (a verbatim quote). If the request does not ask for a concrete change, say so."
)
_FIELDS = (("requests", "cpu"), ("requests", "memory"), ("limits", "cpu"), ("limits", "memory"))


def _container_summary(dep: dict) -> list[dict]:
    return [{"name": c["name"], "resources": c["resources"]} for c in dep["template"]["containers"]]


async def _extract(ctx: StepContext, dep: dict, autoscaler: dict | None) -> dict:
    context = {
        "deployment": f"{dep['namespace']}/{dep['name']}",
        "current_replicas": dep["replicas"],
        "containers": _container_summary(dep),
        "autoscaler": (
            {"kind": autoscaler.get("kind"), "name": autoscaler.get("name"), "min": autoscaler.get("min"), "max": autoscaler.get("max")}
            if autoscaler
            else None
        ),
    }
    user_prompt = (
        f"Request:\n{ctx.prompt}\n\nCurrent state:\n{context}\n\n"
        "Which change does the request ask for?\n"
        "- scale_replicas: set the Deployment's replica count.\n"
        "- set_autoscaler_bounds: change the KEDA ScaledObject's or HPA's min and/or max replica count.\n"
        "- set_resources: change CPU and/or memory requests and/or limits of one container. Report only the "
        "fields the request names.\n"
        "- none: no concrete change is requested.\n"
        f"End your response with a line reading exactly '{_SENTINEL}' followed by ONLY a JSON object:\n"
        '{"action": "scale_replicas" | "set_autoscaler_bounds" | "set_resources" | "none", '
        '"replicas": <int or null>, "replicas_quote": "<verbatim words or null>", '
        '"min": <int or null>, "min_quote": "<verbatim or null>", "max": <int or null>, "max_quote": "<verbatim or null>", '
        '"container": "<container name or null>", '
        '"resources": {"requests": {"cpu": {"value": "<quantity>", "quote": "<verbatim>"}, "memory": {...}}, '
        '"limits": {"cpu": {...}, "memory": {...}}}, '
        '"reasoning": "<one line>"}\n'
        "Omit resource fields the request does not name. Quantities use Kubernetes units (e.g. 500m, 2, 4Gi, 512Mi)."
    )
    raw = await ctx.tool("llm_complete")(ctx.config["llm"], _SYSTEM, user_prompt)
    try:
        return extract(raw, _SENTINEL)
    except RuntimeError:
        return {"action": "none", "reasoning": "the request could not be read as a concrete change"}


def _int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) else None


def _per_pod_requests(template: dict, override: dict | None = None) -> dict:
    total = {"cpu": 0, "memory": 0}
    for container in template["containers"]:
        requests = dict(container["resources"].get("requests") or {})
        if override and container["name"] == override.get("container"):
            requests.update(override.get("requests") or {})
        for resource in total:
            if requests.get(resource):
                total[resource] += quantities.to_units(resource, requests[resource])
    return total


def _surge_pods(dep: dict) -> int:
    replicas = max(dep["replicas"] or 0, 1)
    surge = str(dep["strategy"].get("max_surge", "25%"))
    if dep["strategy"].get("type") == "Recreate":
        return replicas
    if surge.endswith("%"):
        return max(1, math.ceil(replicas * float(surge[:-1]) / 100))
    return max(1, int(surge))


def _startup_estimate(dep: dict) -> int | None:
    worst = 0
    for container in dep["template"]["containers"]:
        startup = container.get("startup_probe")
        readiness = container.get("readiness_probe")
        seconds = 0
        if startup:
            seconds += startup["initial_delay_seconds"] + startup["period_seconds"] * startup["failure_threshold"]
        if readiness:
            seconds += readiness["initial_delay_seconds"] + readiness["period_seconds"]
        worst = max(worst, seconds)
    return worst or None


def _pdb_risks(pdbs: list[dict], dep: dict) -> list[dict]:
    # The pods carry the template's labels (a superset of the selector's); a PDB can key on either.
    pod_labels = {**(dep["template"].get("labels") or {}), **(dep["selector"] or {})}
    risks = []
    for pdb in pdbs:
        selector = pdb.get("selector") or {}
        if selector and all(pod_labels.get(k) == v for k, v in selector.items()):
            risks.append(
                {
                    "level": "info",
                    "message": (
                        f"PodDisruptionBudget {pdb['name']} covers these pods (minAvailable={pdb.get('min_available')}, "
                        f"maxUnavailable={pdb.get('max_unavailable')}). It does not block this scale-down, which removes "
                        "pods through the ReplicaSet rather than by eviction; with fewer replicas it leaves less room "
                        "for voluntary disruptions such as node drains."
                    ),
                }
            )
    return risks


# ------------------------------------------------------------------ per action


def _plan_scale(ctx, dep, autoscaler, change, threshold):
    if autoscaler:
        return None, refusal(
            f"{dep['namespace']}/{dep['name']} is managed by {autoscaler.get('kind')} "
            f"{autoscaler.get('name') or ''}".strip()
            + f" (min {autoscaler.get('min')}, max {autoscaler.get('max')}). A direct replica change would be "
            "overwritten by the autoscaler within seconds, so it is not proposed. To change how far it scales, "
            "ask explicitly for the autoscaler's min and/or max, e.g. \"set the KEDA max replicas of "
            f"{dep['name']} to 10\".",
            autoscaler={"kind": autoscaler.get("kind"), "name": autoscaler.get("name"), "min": autoscaler.get("min"), "max": autoscaler.get("max")},
        )
    replicas = _int(change.get("replicas"))
    if replicas is None or not policy.quote_in_prompt(change.get("replicas_quote"), ctx.prompt, replicas):
        return None, refusal("The request does not state a replica count explicitly enough to act on.")
    if replicas < policy.REPLICA_FLOOR:
        return None, refusal(f"Scaling below {policy.REPLICA_FLOOR} replica is never done by this agent (requested {replicas}).")
    if replicas == dep["replicas"]:
        return None, refusal(f"{dep['namespace']}/{dep['name']} already runs {replicas} replica(s); nothing to change.")
    action = {"action": "scale_replicas", "namespace": dep["namespace"], "deployment": dep["name"], "replicas": replicas}
    facts = {
        "before": {"replicas": dep["replicas"]},
        "after": {"replicas": replicas},
        "scale_down": replicas < dep["replicas"],
        "high_replicas": replicas > threshold,
        "new_pods": max(0, replicas - dep["replicas"]),
        "changed_object": {"kind": "Deployment", "name": dep["name"], "labels": dep["labels"], "annotations": dep["annotations"]},
        "rendered_changes": [{"object": f"Deployment {dep['namespace']}/{dep['name']}", "field": "spec.replicas", "from": dep["replicas"], "to": replicas}],
        "action_name": f"Scale {dep['namespace']}/{dep['name']} from {dep['replicas']} to {replicas} replicas",
    }
    return (action, facts), None


def _plan_autoscaler(ctx, dep, autoscaler, change, threshold):
    if not autoscaler:
        return None, refusal(
            f"{dep['namespace']}/{dep['name']} is not managed by KEDA or an HPA, so there is no autoscaler "
            "min/max to change; ask for a replica count instead."
        )
    if not autoscaler.get("name"):
        return None, refusal(
            f"{dep['namespace']}/{dep['name']} is scaled by KEDA (seen through its generated HPA "
            f"{autoscaler.get('inferred_from_hpa')}), but its ScaledObject could not be read, so it cannot be changed safely."
        )
    if not policy.mentions_autoscaler(ctx.prompt, [dep["name"], dep["namespace"], autoscaler.get("name")]):
        return None, refusal(
            f"{dep['namespace']}/{dep['name']} is managed by {autoscaler['kind']} {autoscaler['name']}; its "
            "min/max are changed only on an explicit request that names the autoscaler (KEDA/ScaledObject/HPA) and the values."
        )
    new = {}
    for bound in ("min", "max"):
        value = _int(change.get(bound))
        if value is None:
            continue
        if not policy.quote_in_prompt(change.get(f"{bound}_quote"), ctx.prompt, value):
            return None, refusal(f"The request does not state the autoscaler {bound} explicitly enough to act on.")
        new[bound] = value
    if not new:
        return None, refusal("The request names the autoscaler but no min or max value to set.")
    for bound, value in new.items():
        if value < policy.REPLICA_FLOOR:
            return None, refusal(f"An autoscaler {bound} below {policy.REPLICA_FLOOR} is never set by this agent (requested {value}).")
    final_min = new.get("min", autoscaler.get("min"))
    final_max = new.get("max", autoscaler.get("max"))
    if final_min is not None and final_max is not None and final_max < final_min:
        return None, refusal(f"That would leave max ({final_max}) below min ({final_min}).")
    if all(autoscaler.get(b) == v for b, v in new.items()):
        return None, refusal(f"{autoscaler['kind']} {autoscaler['name']} already has those values; nothing to change.")
    fields = {"ScaledObject": ("spec.minReplicaCount", "spec.maxReplicaCount"), "HorizontalPodAutoscaler": ("spec.minReplicas", "spec.maxReplicas")}[autoscaler["kind"]]
    rendered = [
        {
            "object": f"{autoscaler['kind']} {dep['namespace']}/{autoscaler['name']}",
            "field": fields[0] if bound == "min" else fields[1],
            "from": autoscaler.get(bound),
            "to": value,
        }
        for bound, value in new.items()
    ]
    action = {
        "action": "set_autoscaler_bounds",
        "namespace": dep["namespace"],
        "deployment": dep["name"],
        "autoscaler": {"kind": autoscaler["kind"], "name": autoscaler["name"]},
        **new,
    }
    label = "KEDA ScaledObject" if autoscaler["kind"] == "ScaledObject" else "HPA"
    parts = ", ".join(f"{b} {autoscaler.get(b)} -> {v}" for b, v in new.items())
    current_replicas = dep["replicas"] or 0
    facts = {
        "before": {b: autoscaler.get(b) for b in new},
        "after": dict(new),
        "scale_down": final_max is not None and final_max < current_replicas,
        "high_replicas": any(v > threshold for v in new.values()),
        "new_pods": max(0, (new.get("min") or 0) - current_replicas),
        "changed_object": {"kind": autoscaler["kind"], "name": autoscaler["name"], "labels": autoscaler.get("labels") or {}, "annotations": autoscaler.get("annotations") or {}},
        "rendered_changes": rendered,
        "action_name": f"Set {label} {autoscaler['name']} replica bounds ({parts})",
        "notes": (
            [f"The ScaledObject's current minReplicaCount is {autoscaler.get('min')}; it is left unchanged."]
            if autoscaler["kind"] == "ScaledObject" and autoscaler.get("min") == 0 and "min" not in new
            else []
        ),
    }
    return (action, facts), None


def _plan_resources(ctx, dep, change, nodes, pods, headroom):
    containers = {c["name"]: c for c in dep["template"]["containers"]}
    name = change.get("container")
    if not name and len(containers) == 1:
        name = next(iter(containers))
    if name not in containers:
        return None, refusal(
            "Which container to change is not clear; name one of: " + ", ".join(sorted(containers)),
            containers=sorted(containers),
        )
    requested_raw = change.get("resources") or {}
    requested: dict = {}
    for side, resource in _FIELDS:
        entry = (requested_raw.get(side) or {}).get(resource)
        if not entry:
            continue
        if isinstance(entry, str):
            entry = {"value": entry, "quote": None}
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
        req = applied.get(("requests", resource))
        lim = applied.get(("limits", resource))
        cur_req = current.get("requests", {}).get(resource)
        cur_lim = current.get("limits", {}).get(resource)
        eff_req = req if req is not None else (quantities.to_units(resource, cur_req) if cur_req else None)
        eff_lim = lim if lim is not None else (quantities.to_units(resource, cur_lim) if cur_lim else None)
        if eff_req is not None and eff_lim is not None and eff_req > eff_lim:
            which = "new request" if req is not None else "current request"
            against = "new limit" if lim is not None else "current limit"
            return None, refusal(
                f"{resource}: the {which} ({quantities.format_units(resource, eff_req)}) would exceed the {against} "
                f"({quantities.format_units(resource, eff_lim)}). Request both together, or a value that fits.",
                clamped=clamped,
            )

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


# ------------------------------------------------------------------ run


async def run(ctx: StepContext) -> dict:
    target = resolved_target(ctx)
    if target.get("scope") != "deployment" or not target.get("name") or not target.get("namespace"):
        candidates = (ctx.results.get("find_workload") or {}).get("candidates") or []
        return refusal(
            "Which Deployment to change is not clear" + (f"; candidates: {', '.join(candidates)}" if candidates else "."),
            candidates=candidates,
        )

    opts = options(ctx)
    threshold = policy.high_replica_threshold(opts)
    namespace, name = target["namespace"], target["name"]
    dep = await ctx.tool("get_deployment")(namespace, name)
    autoscaler = await ctx.tool("find_autoscaler")(namespace, name)
    change = await _extract(ctx, dep, autoscaler)
    kind = change.get("action")

    nodes = pods = None
    if kind == "scale_replicas":
        planned, refused = _plan_scale(ctx, dep, autoscaler, change, threshold)
    elif kind == "set_autoscaler_bounds":
        planned, refused = _plan_autoscaler(ctx, dep, autoscaler, change, threshold)
    elif kind == "set_resources":
        nodes = await ctx.tool("list_nodes")()
        pods = await ctx.tool("list_pods")(None, None, True)
        planned, refused = _plan_resources(ctx, dep, change, nodes, pods, policy.headroom_ratio(opts))
    else:
        # Nothing is proposed without a value the requester chose; hand back what is configured now
        # so a caller (an engineer, or the on-call coordinator) can ask again with one.
        return refusal(
            "The request does not ask for a concrete scaling or CPU/memory change: "
            + str(change.get("reasoning") or "no change identified")
            + ". Nothing was proposed. Ask again with the exact new value, for example 'set the memory limit "
            f"of container {_container_summary(dep)[0]['name'] if dep['template']['containers'] else '<container>'} "
            f"in deployment {name} (namespace {namespace}) to <value>'.",
            current={"replicas": dep.get("replicas"), "containers": _container_summary(dep)},
        )
    if refused:
        return refused
    action, facts = planned

    risks: list[dict] = []
    tier = policy.base_tier(action["action"], facts)
    if facts["high_replicas"]:
        tier = "critical"
        facts["action_name"] = f"[HIGH REPLICA COUNT > {threshold}] {facts['action_name']}"
        risks.append(
            {
                "level": "critical",
                "message": f"The target replica count exceeds {threshold}. Check the number carefully before approving — this is the only confirmation.",
            }
        )
    if action["action"] == "set_resources":
        risks.append({"level": "medium", "message": "Changing CPU/memory triggers a rolling restart of every pod of this Deployment."})
        claims = [c for c in dep["template"]["pvc_claims"] if c]
        if claims and dep["strategy"]["type"] == "RollingUpdate":
            pvcs = await ctx.tool("get_pvcs")(namespace, claims)
            rwo = [p["name"] for p in pvcs if "ReadWriteOnce" in (p.get("access_modes") or []) or "ReadWriteOncePod" in (p.get("access_modes") or [])]
            if rwo:
                tier = policy.raise_tier(tier, "high")
                risks.append(
                    {
                        "level": "high",
                        "message": (
                            f"Pods mount ReadWriteOnce volume(s) {', '.join(rwo)}: a new pod may wait for the old pod to "
                            "release the volume, so the rolling update can stall instead of overlapping."
                        ),
                    }
                )
    if facts.get("scale_down"):
        risks.extend(_pdb_risks(await ctx.tool("list_pdbs")(namespace), dep))

    outlook = None
    if facts["new_pods"] > 0:
        nodes = nodes if nodes is not None else await ctx.tool("list_nodes")()
        pods = pods if pods is not None else await ctx.tool("list_pods")(None, None, True)
        per_pod = _per_pod_requests(dep["template"], facts.get("per_pod_override"))
        outlook = capacity.estimate_fit(nodes, pods, dep["template"], per_pod, facts["new_pods"])
        if outlook["unplaced"]:
            ca = await ctx.tool("get_ca_status")()
            ca_notes = capacity.ca_risks(ca, outlook["nodes_needed"], outlook["exceeds_every_node"])
            outlook["node_groups"] = capacity.node_group_summary(ca)
            tier = policy.raise_tier(tier, "medium")
            if not outlook["exceeds_every_node"]:
                risks.append(
                    {
                        "level": "medium",
                        "message": (
                            f"Estimate: {outlook['unplaced']} of {outlook['new_pods']} new pod(s) do not fit on current "
                            "nodes; they stay Pending until Cluster Autoscaler adds node(s)."
                        ),
                    }
                )
            risks.extend(ca_notes)
            if any(r["level"] == "high" for r in ca_notes):
                tier = policy.raise_tier(tier, "high")

    changed = facts["changed_object"]
    chain = await ctx.tool("get_flux_chain")(changed["labels"], changed["annotations"])
    drift = governance.drift_notice(chain.get("ownership") or {}, chain)
    if (chain.get("ownership") or {}).get("class") != "unowned":
        risks.append({"level": "medium", "message": drift})
    hint = persistence.build_hint(chain, facts["rendered_changes"])

    dry_run = await ctx.tool("dry_run_patch")(action)
    startup = _startup_estimate(dep)

    pending_action = {
        "id": str(uuid.uuid4()),
        **action,
        "action_name": facts["action_name"],
        "before": facts["before"],
        "after": facts["after"],
        "requested": facts.get("requested"),
        "clamped": facts.get("clamped") or [],
        "node_cap": facts.get("cap"),
        "risk_tier": tier,
        "risks": risks,
        "notes": facts.get("notes") or [],
        "capacity_outlook": outlook,
        "dry_run": dry_run,
        "ownership": chain.get("ownership"),
        "drift_notice": drift,
        "persistence": hint,
        "estimated_seconds_per_pod_to_ready": startup,
        "snapshot": {
            "deployment_generation": dep["generation"],
            "deployment_replicas": dep["replicas"],
            "container_resources": {c["name"]: c["resources"] for c in dep["template"]["containers"]},
            "autoscaler": governance.autoscaler_fingerprint(autoscaler),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return finish({"pending_action": pending_action, "requires_approval": True})
