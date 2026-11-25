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
from ..tools import governance, health_rules

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
        findings.extend(health_rules.pod_findings(pod))
    findings.extend(health_rules.usage_findings(pods, usage))

    if options(ctx).get("include_logs", True):
        get_log = ctx.tool("get_pod_log")
        tails = 0
        for finding in findings:
            if tails >= _LOG_TAILS:
                break
            if finding["kind"] == "Pod" and str(finding["reason"]).startswith("CrashLoopBackOff"):
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
