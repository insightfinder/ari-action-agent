"""Works out exactly what would change, checks it, dry-runs it, and stops.

The model's only job here is to read the request and say which change it
asks for, quoting the words it took each number from. Which changes are
offered depends on the workload `find_workload` resolved (a Deployment, a
StatefulSet or a DaemonSet; each action lists its `workload_kinds`), and a
change that takes no value (a restart, a rollback, a pod delete) is
proposed only when the request's own words ask for it (`spec.accepts`).
Everything else is code: whether the Deployment is managed by KEDA/HPA (and so may only be
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
    "You read a request to change a Kubernetes workload (a Deployment, StatefulSet, DaemonSet or CronJob) or one "
    "of its pods and report exactly what it asks for. You never invent or round a number. For every number you report "
    "you also give the exact words of the request it came from (a verbatim quote). If the request does not ask "
    "for a concrete change, say so."
)
def _container_summary(workload: dict) -> list[dict]:
    return [{"name": c["name"], "resources": c["resources"]} for c in workload["template"]["containers"]]


def _images(workload: dict) -> list[str | None]:
    return [c.get("image") for c in workload["template"]["containers"]]


def _offered(kind: str) -> list:
    return [spec for spec in actions.REGISTRY.values() if kind in spec.workload_kinds]


async def _extract(ctx: StepContext, workload: dict, autoscaler: dict | None) -> dict:
    offered = _offered(workload.get("kind") or "Deployment")
    context = {
        "workload": f"{workload.get('kind') or 'Deployment'} {workload['namespace']}/{workload['name']}",
        "current_replicas": workload.get("replicas"),
        "containers": [{**c, "image": i} for c, i in zip(_container_summary(workload), _images(workload))],
        **({"paused": bool(workload.get("paused"))} if "paused" in workload else {}),
        **({"suspended": bool(workload.get("suspend")), "schedule": workload.get("schedule")} if "suspend" in workload else {}),
        "autoscaler": (
            {"kind": autoscaler.get("kind"), "name": autoscaler.get("name"), "min": autoscaler.get("min"), "max": autoscaler.get("max")}
            if autoscaler
            else None
        ),
    }
    user_prompt = (
        f"Request:\n{ctx.prompt}\n\nCurrent state:\n{context}\n\n"
        "Which change does the request ask for?\n"
        + "".join(f"- {spec.name}: {spec.prompt_line}\n" for spec in offered)
        + "- none: no concrete change is requested.\n"
        f"End your response with a line reading exactly '{_SENTINEL}' followed by ONLY a JSON object:\n"
        '{"action": ' + " | ".join(f'"{n}"' for n in (*(spec.name for spec in offered), "none")) + ', '
        '"replicas": <int or null>, "replicas_quote": "<verbatim words or null>", '
        '"min": <int or null>, "min_quote": "<verbatim or null>", "max": <int or null>, "max_quote": "<verbatim or null>", '
        '"container": "<container name or null>", "pod": "<pod name exactly as written in the request, or null>", '
        '"revision": <revision number to roll back to, or null for the previous one>, "revision_quote": "<verbatim or null>", '
        '"image": "<full image reference exactly as written in the request, or null>", '
        '"tag": "<image tag or version exactly as written, when the request gives only that, or null>", '
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


_SCALE_DOWN_PDB = (
    "It does not block this scale-down, which removes pods through the ReplicaSet rather than by eviction; with "
    "fewer replicas it leaves less room for voluntary disruptions such as node drains."
)


def _pdb_risks(pdbs: list[dict], pod_labels: dict, message: str) -> list[dict]:
    risks = []
    for pdb in pdbs:
        selector = pdb.get("selector") or {}
        if selector and all(pod_labels.get(k) == v for k, v in selector.items()):
            risks.append(
                {
                    "level": "info",
                    "message": (
                        f"PodDisruptionBudget {pdb['name']} covers these pods (minAvailable={pdb.get('min_available')}, "
                        f"maxUnavailable={pdb.get('max_unavailable')}). {message}"
                    ),
                }
            )
    return risks


def _template_pod_labels(workload: dict) -> dict:
    # The pods carry the template's labels (a superset of the selector's); a PDB can key on either.
    return {**(workload["template"].get("labels") or {}), **(workload.get("selector") or {})}


def _example(kind: str, name: str, namespace: str) -> str:
    if kind == "CronJob":
        return f"'suspend cronjob {name} in namespace {namespace}'"
    return f"'restart {kind.lower()} {name} in namespace {namespace}'"


# ------------------------------------------------------------------ run


async def run(ctx: StepContext) -> dict:
    target = resolved_target(ctx)
    kind = target.get("kind") or "Deployment"
    if target.get("scope") == "node":
        # Cordon, uncordon and drain were built and then declined (user, 2026-11-25): nodes are only read.
        return refusal(
            f"This agent does not change nodes (cordon, uncordon, drain, labels, taints), so nothing was proposed for "
            f"node {target.get('name')}. Use kubectl, or ask for a health check of the node."
        )
    if target.get("scope") not in ("deployment", "workload") or not target.get("name") or not target.get("namespace"):
        candidates = (ctx.results.get("find_workload") or {}).get("candidates") or []
        return refusal(
            "Which workload to change is not clear" + (f"; candidates: {', '.join(candidates)}" if candidates else "."),
            candidates=candidates,
        )

    opts = options(ctx)
    threshold = policy.high_replica_threshold(opts)
    namespace, name = target["namespace"], target["name"]
    autoscaler = None
    if kind == "Deployment":
        workload = await ctx.tool("get_deployment")(namespace, name)
        autoscaler = await ctx.tool("find_autoscaler")(namespace, name)
    else:
        workload = await ctx.tool("get_workload")(kind, namespace, name)
    change = await _extract(ctx, workload, autoscaler)
    spec = actions.REGISTRY.get(change.get("action"))
    if spec is not None and kind not in spec.workload_kinds:
        spec = None
    if spec is not None and not spec.accepts(ctx.prompt):
        if not spec.fallback:
            # The model's choice alone never proposes a change the request's own words do not ask for.
            return refusal(spec.not_asked.format(kind=kind, name=name, namespace=namespace), asked_for=spec.name)
        spec = actions.REGISTRY[spec.fallback]
    if spec is None:
        # Nothing is proposed without a value the requester chose; hand back what is configured now
        # so a caller (an engineer, or the on-call coordinator) can ask again with one.
        return refusal(
            "The request does not ask for a concrete change this agent can make to "
            f"{kind} {namespace}/{name}: "
            + str(change.get("reasoning") or "no change identified")
            + ". Nothing was proposed. Ask again with the exact change, for example "
            + (
                "'set the memory limit "
                f"of container {_container_summary(workload)[0]['name'] if workload['template']['containers'] else '<container>'} "
                f"in deployment {name} (namespace {namespace}) to <value>' or "
                if kind == "Deployment"
                else ""
            )
            + _example(kind, name, namespace)
            + ".",
            current={"kind": kind, "replicas": workload.get("replicas"), "containers": _container_summary(workload)},
        )

    target_obj, refused = await spec.resolve_target(ctx, workload, change)
    if refused:
        return refused
    state = await spec.load_state(ctx, target_obj)
    nodes = pods = None
    if spec.needs_capacity:
        nodes = await ctx.tool("list_nodes")()
        pods = await ctx.tool("list_pods")(None, None, True)
    env = {"threshold": threshold, "headroom": policy.headroom_ratio(opts), "nodes": nodes, "pods": pods}
    planned, refused = spec.plan(ctx, state, change, env)
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
    for risk in facts.get("risks") or []:
        risks.append(risk)
        if risk.get("level") in ("medium", "high", "critical"):
            tier = policy.raise_tier(tier, risk["level"])
    if facts.get("rolls_pods"):
        risks.append({"level": "medium", "message": facts.get("roll_message") or "Every pod of this workload is replaced."})
        claims = [c for c in workload["template"]["pvc_claims"] if c]
        if kind == "Deployment" and claims and workload["strategy"]["type"] == "RollingUpdate":
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
        risks.extend(_pdb_risks(await ctx.tool("list_pdbs")(namespace), _template_pod_labels(workload), _SCALE_DOWN_PDB))
    if facts.get("pdb_message"):
        risks.extend(_pdb_risks(await ctx.tool("list_pdbs")(namespace), facts.get("pod_labels") or {}, facts["pdb_message"]))

    outlook = None
    if facts.get("new_pods", 0) > 0:
        nodes = nodes if nodes is not None else await ctx.tool("list_nodes")()
        pods = pods if pods is not None else await ctx.tool("list_pods")(None, None, True)
        per_pod = _per_pod_requests(workload["template"], facts.get("per_pod_override"))
        outlook = capacity.estimate_fit(nodes, pods, workload["template"], per_pod, facts["new_pods"])
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
    ownership = chain.get("ownership") or {}
    drift = spec.lifetime_notice(ownership, chain)
    level = spec.lifetime_risk(ownership)
    if level:
        risks.append({"level": level, "message": drift})
    hint = persistence.build_hint(chain, facts["rendered_changes"], nothing_to_persist=facts.get("nothing_to_persist"))

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
    startup = None if "restart" in facts and not facts["restart"] else _startup_estimate(workload)

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
