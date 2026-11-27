"""Pod-level health, collected deterministically.

No model decides what is unhealthy: the rules in tools/health_rules.py
produce findings with their evidence, and the shared `respond` step only
summarises them. Missing data is reported as a gap (events expire after
about an hour; metrics-server may be absent; RBAC may deny a read) — an
absent signal is never presented as a healthy one.
"""

import re
from datetime import datetime, timezone

from common.capabilities_base import StepContext

from ..stepkit import finish, options, resolved_target, untrusted_block
from ..tools import capacity, governance, health_rules, policy, quantities

_FINDINGS_LIMIT = {"deployment": 40, "workload": 40, "node": 40, "namespace": 40, "cluster": 30}
_EVENTS_LIMIT = 15
_LOG_TAILS = 5
_RESOURCES_LIMIT = 20


def _event_digest(events: list[dict], limit: int) -> list[dict]:
    """Warning events grouped by (reason, involved object) — bounded."""
    grouped: dict = {}
    for e in events:
        involved = e.get("involved") or {}
        key = (e.get("reason"), involved.get("kind"), involved.get("namespace"), involved.get("name"))
        entry = grouped.setdefault(
            key,
            {
                "reason": e.get("reason"),
                "object": f"{involved.get('kind')}/{involved.get('namespace')}/{involved.get('name')}",
                "count": 0,
                "last_seen": e.get("last_seen"),
                "message": e.get("message"),
            },
        )
        entry["count"] += e.get("count") or 1
    return sorted(grouped.values(), key=lambda g: -g["count"])[:limit]


# Kubernetes' vowel-free safe-encoding alphabet: pod-template-hash values and
# generated Pod name suffixes are drawn from it, ordinary name segments
# ("gateway", "worker") almost never are.
_GENERATED = "[bcdfghjklmnpqrstvwxz2456789]"


def _belongs_to(name: str | None, deployment: str, own_pods: set[str] | frozenset = frozenset()) -> bool:
    """Whether `name` is `deployment`, one of its listed Pods (`own_pods`, selected by its selector), or an
    object it generated: a ReplicaSet `<deployment>-<pod-template-hash>` or a Pod `<deployment>-<hash>-<suffix>`
    that is gone by now.

    A plain prefix test would hand `api-gateway`'s events to `api`.
    """
    if not name:
        return False
    if name == deployment or name in own_pods:
        return True
    # A pod-template-hash is a 32-bit hash written out digit by digit, so 6 to 10 characters in
    # practice; a shorter vowel-free segment ("api-grpc", "api-cms") is a name, not a hash.
    pattern = rf"{re.escape(deployment)}-{_GENERATED}{{6,10}}(?:-{_GENERATED}{{5}})?"
    return re.fullmatch(pattern, name) is not None


_deployment_of = governance.deployment_of


def _flagged_keys(findings: list[dict], pods: list[dict]) -> set[tuple[str, str]]:
    """(namespace, Deployment) for every Deployment a finding is about, directly or through its Pods."""
    owners = {(p.get("namespace"), p.get("name")): _deployment_of(p) for p in pods}
    keys = set()
    for f in findings:
        if f["kind"] == "Deployment":
            keys.add((f["namespace"], f["name"]))
        elif f["kind"] == "Pod" and owners.get((f["namespace"], f["name"])):
            keys.add((f["namespace"], owners[(f["namespace"], f["name"])]))
    return keys


# An OOM-killed container's suggested memory limit: this many times its current limit (user decision, 2026-11-27).
# The peak it needed is not on record (metrics-server samples before the kill, and a crash loop leaves few), so
# this is a first step, capped by what the largest eligible node can give; still killed, raise it again.
OOM_LIMIT_FACTOR = 2
# Killed after running at least this long: more like a leak or growth with load than a limit set too low.
LONG_RUN_SECONDS = 3600


async def _oom_suggestions(ctx: StepContext, findings: list[dict], pods: list[dict], templates: dict) -> None:
    """Adds `suggestion` to each OOMKilled finding: a memory limit of `OOM_LIMIT_FACTOR` x the current one, capped by
    node capacity, and for a Deployment the request that would get it proposed. `templates` maps (namespace,
    Deployment) to its view."""
    oom = [f for f in findings if f.get("kind") == "Pod" and f.get("killed_by") == "oom"]
    if not oom:
        return
    by_pod = {(p.get("namespace"), p.get("name")): p for p in pods}
    nodes = all_pods = None
    made: dict = {}
    for finding in oom:
        pod = by_pod.get((finding["namespace"], finding["name"])) or {}
        container = finding.get("container")
        owner = governance.deployment_of(pod)
        key = (finding["namespace"], owner or finding["name"], container)
        if key in made:
            finding["suggestion"] = made[key]
            continue
        limit = ((pod.get("limits") or {}).get(container) or {}).get("memory")
        if not limit:
            made[key] = finding["suggestion"] = {
                "field": "limits.memory",
                "container": container,
                "note": "the container has no memory limit, so it was killed when its node ran out of memory, not at "
                "a limit of its own: set a memory request that reserves what it uses rather than a larger limit",
            }
            continue
        current = quantities.to_units("memory", limit)
        want = -(-current * OOM_LIMIT_FACTOR // quantities.MI) * quantities.MI
        suggestion = {"field": "limits.memory", "container": container, "from": limit,
                      "basis": f"{OOM_LIMIT_FACTOR} x the current limit {limit}"}
        dep = templates.get((finding["namespace"], owner)) if owner else None
        if dep is not None:
            if nodes is None:
                nodes = await ctx.tool("list_nodes")()
                all_pods = await ctx.tool("list_pods")(None, None, True)
            cap = capacity.resource_cap(nodes, all_pods, dep["template"], policy.headroom_ratio(options(ctx)), container, {"memory": want})
            room = (cap or {}).get("memory", {}).get("cap")
            if room is not None and room < want:
                if room <= current:
                    suggestion["note"] = (
                        f"no eligible node can give container {container} more than its current limit "
                        f"({quantities.format_memory(room)} at most on {cap['memory']['node']}); larger nodes, or less "
                        "memory used, are needed"
                    )
                    made[key] = finding["suggestion"] = suggestion
                    continue
                want = room
                suggestion["basis"] += f", capped by the largest eligible node ({cap['memory']['node']})"
        suggestion["to"] = quantities.format_memory(want)
        ran = finding.get("last_run_seconds")
        if ran is not None and ran >= LONG_RUN_SECONDS:
            suggestion["pattern"] = "long_run"
            suggestion["note"] = (
                f"it ran {ran // 60} min before being killed, which looks more like a memory leak or growth with load "
                "than a limit set too low: a larger limit only delays the next OOM kill; use it to stop the restarts "
                "and find what uses the memory"
            )
        else:
            suggestion["pattern"] = "early" if ran is not None else "unknown"
            suggestion["note"] = (
                "a first step, not a measured need (the peak it used before being killed is not on record): if it is "
                "still OOM-killed, raise it again or find what uses the memory"
            )
        if dep is not None:
            suggestion["request"] = (
                f"set the memory limit of container {container} in deployment {owner} in namespace "
                f"{finding['namespace']} to {suggestion['to']}"
            )
        made[key] = finding["suggestion"] = suggestion


async def run(ctx: StepContext) -> dict:
    found = ctx.results.get("find_workload")
    if found is not None and not found.get("resolution"):
        # A name that matches nothing (or several things) is not widened to the whole namespace: the
        # report would look like an answer about a workload it never looked at.
        candidates = found.get("candidates") or []
        return finish(
            {
                "report": {
                    "scope": {"type": "unresolved", "namespace": None, "deployment": None, "candidates": candidates},
                    "as_of": datetime.now(timezone.utc).isoformat(),
                    "findings": [],
                    "gaps": [
                        "Nothing was checked: the request did not match exactly one Deployment, namespace or the "
                        "whole cluster"
                        + (f"; candidates: {', '.join(candidates)}." if candidates else ".")
                        + " Name the Deployment (or the namespace) as it appears in the cluster."
                    ],
                }
            }
        )
    target = resolved_target(ctx)
    scope = target.get("scope")
    namespace = target.get("namespace") or options(ctx).get("namespace")
    if scope not in ("deployment", "workload", "node", "namespace", "cluster"):
        scope = "namespace" if namespace else "cluster"
    if scope in ("cluster", "node"):
        namespace = None

    gaps: list[str] = []
    workload = None
    node = None
    if scope == "node":
        # The pods running on one node, in any namespace, and the node itself.
        node = await ctx.tool("get_node")(target["name"])
        deployments = []
        pods = await ctx.tool("list_pods_on_node")(target["name"]) if node else []
        if node is None:
            gaps.append(f"node {target['name']} no longer exists (Cluster Autoscaler may have removed it)")
    elif scope == "deployment":
        dep = await ctx.tool("get_deployment")(namespace, target["name"])
        deployments = [dep]
        pods = await ctx.tool("list_pods")(namespace, dep.get("label_selector") or dep.get("selector") or None)
    elif scope == "workload":
        # A StatefulSet, DaemonSet or CronJob: its own pods (a CronJob's are its Jobs'), never widened to the
        # namespace.
        workload = await ctx.tool("get_workload")(target["kind"], namespace, target["name"])
        deployments = []
        selected = await ctx.tool("list_pods")(namespace, workload.get("label_selector") or workload.get("selector") or None)
        pods = [p for p in selected if governance.owned_by(p, target["kind"], target["name"])]
    else:
        deployments = await ctx.tool("list_deployment_views")(namespace)
        pods = await ctx.tool("list_pods")(namespace)

    try:
        events = await ctx.tool("list_events")(namespace)
    except Exception as exc:  # noqa: BLE001 — events are supporting evidence
        events = []
        gaps.append(f"events could not be read: {exc}")
    if scope in ("deployment", "workload", "node"):
        own_pods = {p.get("name") for p in pods}
        events = [
            e
            for e in events
            if _belongs_to((e.get("involved") or {}).get("name"), target["name"], own_pods)
            or (target.get("kind") == "CronJob" and governance.is_cronjob_job((e.get("involved") or {}).get("name"), target["name"]))
        ]
    if not events:
        gaps.append(
            "No Warning events in the last 60 minutes. Events expire (about an hour by default), so this "
            "means nothing recent was recorded — not that nothing happened."
        )

    try:
        # What restarted a container: a probe kill is a Normal event, so list_events (Warning only) misses it.
        kills = await ctx.tool("list_probe_kills")(namespace)
    except Exception as exc:  # noqa: BLE001 — supporting evidence, like the events
        kills = []
        gaps.append(f"probe kills (Killing events) could not be read: {exc}")

    metrics = await ctx.tool("get_pod_metrics")(namespace)
    usage = metrics.get("usage") or {}
    if metrics.get("gap"):
        gaps.append(metrics["gap"])
    elif pods and not any(f"{p.get('namespace')}/{p.get('name')}" in usage for p in pods):
        gaps.append("metrics.k8s.io returned no usage for these pods; usage against limits was not checked.")

    findings = []
    for dep in deployments:
        findings.extend(health_rules.deployment_findings(dep))
    if workload is not None:
        findings.extend(health_rules.workload_findings(workload))
    if node is not None:
        findings.extend(health_rules.node_findings(node))
    for pod in pods:
        findings.extend(health_rules.pod_findings(pod, kills))
    findings.extend(health_rules.usage_findings(pods, usage))

    templates = {(d["namespace"], d["name"]): d for d in deployments}
    await _oom_suggestions(ctx, findings, pods, templates)

    if options(ctx).get("include_logs", True):
        get_log = ctx.tool("get_pod_log")
        tails = 0
        for finding in findings:
            if tails >= _LOG_TAILS:
                break
            # A crash loop's previous log, and a probe-killed container's (a slow start mostly shows as running,
            # restarted now and then, not as CrashLoopBackOff): what it was doing when it was stopped.
            if finding["kind"] == "Pod" and (
                str(finding["reason"]).startswith("CrashLoopBackOff") or finding.get("killed_by") in ("startup_probe", "liveness_probe")
            ):
                tail = await get_log(finding["namespace"], finding["name"], finding.get("container"), True)
                if tail:
                    finding["previous_log_tail"] = untrusted_block(tail)
                    tails += 1

    bounded, omitted = health_rules.sort_and_bound(findings, _FINDINGS_LIMIT[scope])
    # The configured requests/limits of the workloads that have a problem: what a follow-up change
    # request has to name a concrete value against (an OOM kill says nothing about the limit hit).
    flagged_keys = _flagged_keys(findings, pods)
    flagged = [
        d for d in deployments
        if scope == "deployment" or (d["namespace"], d["name"]) in flagged_keys
    ][:_RESOURCES_LIMIT]
    if workload is not None:
        flagged = [workload]
    container_resources = {
        f"{d['namespace']}/{d['name']}": [
            {"container": c["name"], **(c.get("resources") or {})} for c in (d.get("template") or {}).get("containers") or []
        ]
        for d in flagged
    }
    unhealthy_pods = {(f["namespace"], f["name"]) for f in findings if f["kind"] == "Pod" and f["severity"] != "info"}
    degraded = {(f["namespace"], f["name"]) for f in findings if f["kind"] == "Deployment"}
    report = {
        "scope": {
            "type": scope,
            "namespace": namespace,
            "deployment": target.get("name") if scope == "deployment" else None,
            **({"kind": target["kind"], "name": target["name"]} if scope in ("workload", "node") else {}),
        },
        "as_of": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "deployments": len(deployments),
            "deployments_degraded": len(degraded),
            "pods": len(pods),
            "pods_unhealthy": len(unhealthy_pods),
            "findings": len(findings),
        },
        "findings": bounded,
        "findings_omitted": omitted,
        "warning_events": _event_digest(events, _EVENTS_LIMIT),
        "container_resources": container_resources,
        "gaps": gaps,
    }
    return finish({"report": report})
