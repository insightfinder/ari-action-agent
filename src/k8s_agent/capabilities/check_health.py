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

from ..stepkit import finish, options, resolved_target
from ..tools import health_rules

_FINDINGS_LIMIT = {"deployment": 40, "namespace": 40, "cluster": 30}
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


def _deployment_of(pod: dict) -> str | None:
    """The Deployment that owns `pod`, read from its ReplicaSet owner and pod-template-hash label."""
    owner = pod.get("owner_name")
    template_hash = (pod.get("labels") or {}).get("pod-template-hash")
    if pod.get("owner_kind") == "ReplicaSet" and owner and template_hash and owner.endswith(f"-{template_hash}"):
        return owner[: -len(template_hash) - 1]
    return None


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
    target = resolved_target(ctx)
    scope = target.get("scope")
    namespace = target.get("namespace") or options(ctx).get("namespace")
    if scope not in ("deployment", "namespace", "cluster"):
        scope = "namespace" if namespace else "cluster"
    if scope == "cluster":
        namespace = None

    gaps: list[str] = []
    if scope == "deployment":
        dep = await ctx.tool("get_deployment")(namespace, target["name"])
        deployments = [dep]
        pods = await ctx.tool("list_pods")(namespace, dep.get("selector") or None)
    else:
        deployments = await ctx.tool("list_deployment_views")(namespace)
        pods = await ctx.tool("list_pods")(namespace)

    try:
        events = await ctx.tool("list_events")(namespace)
    except Exception as exc:  # noqa: BLE001 — events are supporting evidence
        events = []
        gaps.append(f"events could not be read: {exc}")
    if scope == "deployment":
        own_pods = {p.get("name") for p in pods}
        events = [e for e in events if _belongs_to((e.get("involved") or {}).get("name"), target["name"], own_pods)]
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
                    finding["previous_log_tail"] = tail
                    tails += 1

    bounded, omitted = health_rules.sort_and_bound(findings, _FINDINGS_LIMIT[scope])
    # The configured requests/limits of the workloads that have a problem: what a follow-up change
    # request has to name a concrete value against (an OOM kill says nothing about the limit hit).
    flagged_keys = _flagged_keys(findings, pods)
    flagged = [
        d for d in deployments
        if scope == "deployment" or (d["namespace"], d["name"]) in flagged_keys
    ][:_RESOURCES_LIMIT]
    container_resources = {
        f"{d['namespace']}/{d['name']}": [
            {"container": c["name"], **(c.get("resources") or {})} for c in (d.get("template") or {}).get("containers") or []
        ]
        for d in flagged
    }
    unhealthy_pods = {(f["namespace"], f["name"]) for f in findings if f["kind"] == "Pod" and f["severity"] != "info"}
    degraded = {(f["namespace"], f["name"]) for f in findings if f["kind"] == "Deployment"}
    report = {
        "scope": {"type": scope, "namespace": namespace, "deployment": target.get("name") if scope == "deployment" else None},
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
