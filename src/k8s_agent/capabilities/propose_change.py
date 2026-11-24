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

import uuid
from datetime import datetime, timezone

from common.capabilities_base import StepContext
from common.llm_json import extract

from .. import actions
from ..stepkit import finish, options, refusal, resolved_target
from ..tools import capacity, persistence, policy, quantities

_SENTINEL = "CHANGE_JSON:"
_SYSTEM = (
    "You read a request to change a Kubernetes Deployment and report exactly what it asks for. You never "
    "invent or round a number. For every number you report you also give the exact words of the request "
    "it came from (a verbatim quote). If the request does not ask for a concrete change, say so."
)
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
        + "".join(f"- {spec.name}: {spec.prompt_line}\n" for spec in actions.REGISTRY.values())
        + "- none: no concrete change is requested.\n"
        f"End your response with a line reading exactly '{_SENTINEL}' followed by ONLY a JSON object:\n"
        '{"action": ' + " | ".join(f'"{n}"' for n in (*actions.REGISTRY, "none")) + ', '
        '"replicas": <int or null>, "replicas_quote": "<verbatim words or null>", '
        '"min": <int or null>, "min_quote": "<verbatim or null>", "max": <int or null>, "max_quote": "<verbatim or null>", '
        '"container": "<container name or null>", "pod": "<pod name exactly as written in the request, or null>", '
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
    spec = actions.REGISTRY.get(change.get("action"))
    if spec is not None and spec.fallback and not spec.accepts(ctx.prompt):
        spec = actions.REGISTRY[spec.fallback]
    state = {"deployment": dep, "autoscaler": autoscaler}

    nodes = pods = None
    if spec is not None:
        target_obj, refused = await spec.resolve_target(ctx, dep, change)
        if refused:
            return refused
        if target_obj["kind"] != "Deployment":
            state = await spec.load_state(ctx, target_obj)
        if spec.needs_capacity:
            nodes = await ctx.tool("list_nodes")()
            pods = await ctx.tool("list_pods")(None, None, True)
        env = {"threshold": threshold, "headroom": policy.headroom_ratio(opts), "nodes": nodes, "pods": pods}
        planned, refused = spec.plan(ctx, state, change, env)
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
    tier = spec.base_tier(facts)
    if facts.get("high_replicas"):
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
    if facts.get("new_pods", 0) > 0:
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
    drift = spec.lifetime_notice(chain.get("ownership") or {}, chain)
    if spec.target(action)["kind"] != "Deployment":
        risks.append({"level": "info", "message": drift})
    elif (chain.get("ownership") or {}).get("class") != "unowned":
        risks.append({"level": "medium", "message": drift})
    hint = persistence.build_hint(chain, facts["rendered_changes"])

    try:
        dry_run = await ctx.tool("dry_run_patch")(action)
    except RuntimeError as exc:
        # A new image can reach a cluster before the chart that grants its RBAC does.
        if spec.rbac_rule and "HTTP 403" in str(exc):
            return refusal(
                f"The agent is not allowed to make this change in this cluster yet: its ServiceAccount lacks "
                f"{spec.rbac_rule}. Nothing was proposed. "
                f"Dry-run answer: {exc}"
            )
        raise
    # Nothing restarts for an in-place resize without RestartContainer.
    startup = None if "restart" in facts and not facts["restart"] else _startup_estimate(dep)

    pending_action = {
        "id": str(uuid.uuid4()),
        **action,
        # Added in II-25740 beside the older `namespace`/`deployment` (which stay): the object the change is
        # about, for actions whose target is not a Deployment. See docs/K8S_AGENT_INTERFACE.md.
        "target": spec.target(action),
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
        "snapshot": spec.snapshot(state),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return finish({"pending_action": pending_action, "requires_approval": True})
