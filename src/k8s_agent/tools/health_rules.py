"""Deterministic Pod/Deployment health rules.

The model never decides whether something is unhealthy — it only
summarises what these rules found. Each finding names its evidence, so a
reader (or the summary) can tell an OOM kill from a failing probe from a
missing image without re-deriving anything.
"""

from . import quantities

_WAITING_ERRORS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
    "ContainerCannotRun",
}
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
RESTART_WARNING_THRESHOLD = 3
USAGE_WARNING_RATIO = 0.9


def _finding(severity: str, kind: str, obj: dict, reason: str, evidence: str, container: str | None = None) -> dict:
    return {
        "severity": severity,
        "kind": kind,
        "namespace": obj.get("namespace"),
        "name": obj.get("name"),
        "container": container,
        "reason": reason,
        "evidence": evidence,
    }


def _termination(state: dict | None) -> str | None:
    if not state or state.get("state") != "terminated":
        return None
    return f"{state.get('reason') or 'terminated'} (exit code {state.get('exit_code')})"


def pod_findings(pod: dict) -> list[dict]:
    findings: list[dict] = []
    if pod.get("deleting"):
        return findings

    if pod.get("phase") == "Pending":
        scheduled = next((c for c in pod.get("conditions") or [] if c.get("type") == "PodScheduled"), None)
        if scheduled and scheduled.get("status") == "False":
            findings.append(
                _finding(
                    "critical",
                    "Pod",
                    pod,
                    scheduled.get("reason") or "Unschedulable",
                    scheduled.get("message") or "pod is not scheduled",
                )
            )
    if pod.get("phase") == "Failed":
        findings.append(_finding("critical", "Pod", pod, pod.get("reason") or "Failed", "pod phase is Failed"))

    for container in pod.get("containers") or []:
        name = container.get("name")
        state = container.get("state") or {}
        last = container.get("last_state")
        restarts = container.get("restart_count") or 0

        if state.get("state") == "waiting" and state.get("reason") in _WAITING_ERRORS:
            reason = state["reason"]
            evidence = state.get("message") or reason
            last_termination = _termination(last)
            if reason == "CrashLoopBackOff" and last_termination:
                # The previous container's termination is the actual cause;
                # OOMKilled and a failing probe look identical from outside.
                reason = f"CrashLoopBackOff ({(last or {}).get('reason') or 'terminated'})"
                evidence = f"previous container {last_termination}; restarts {restarts}"
            findings.append(_finding("critical", "Pod", pod, reason, evidence, name))
            continue

        if state.get("state") == "terminated" and (state.get("exit_code") or 0) != 0:
            if restarts >= 1:
                # Between two restarts of a crash loop the kubelet shows the
                # container that just exited as `terminated` for a few
                # seconds before it reports CrashLoopBackOff (found live,
                # 2026-10-14). Same condition, so the same finding.
                findings.append(
                    _finding(
                        "critical",
                        "Pod",
                        pod,
                        f"CrashLoopBackOff ({state.get('reason') or 'terminated'})",
                        f"container {_termination(state)}, being restarted; restarts {restarts}",
                        name,
                    )
                )
            else:
                findings.append(_finding("warning", "Pod", pod, state.get("reason") or "Error", _termination(state), name))
            continue

        if restarts >= RESTART_WARNING_THRESHOLD:
            detail = _termination(last) or "unknown cause"
            # Named after the last termination, as a crash loop is: an OOM-killed container spends
            # most of its time running when its limit takes a while to fill.
            cause = (last or {}).get("reason") if (last or {}).get("state") == "terminated" else None
            findings.append(
                _finding(
                    "warning",
                    "Pod",
                    pod,
                    f"FrequentRestarts ({cause})" if cause else "FrequentRestarts",
                    f"{restarts} restarts; last: {detail}",
                    name,
                )
            )

        if state.get("state") == "running" and not container.get("ready"):
            findings.append(_finding("info", "Pod", pod, "NotReady", "container running but not ready", name))
    return findings


def deployment_findings(dep: dict) -> list[dict]:
    findings: list[dict] = []
    desired = dep.get("replicas") or 0
    available = (dep.get("status") or {}).get("available") or 0
    for cond in dep.get("conditions") or []:
        if cond.get("type") == "Progressing" and cond.get("reason") == "ProgressDeadlineExceeded":
            findings.append(_finding("critical", "Deployment", dep, "ProgressDeadlineExceeded", cond.get("message") or ""))
        if cond.get("type") == "ReplicaFailure" and cond.get("status") == "True":
            findings.append(_finding("critical", "Deployment", dep, cond.get("reason") or "ReplicaFailure", cond.get("message") or ""))
    if available < desired:
        findings.append(
            _finding("warning", "Deployment", dep, "ReplicasUnavailable", f"{available}/{desired} replicas available")
        )
    return findings


def usage_findings(pods: list[dict], usage: dict) -> list[dict]:
    """Point-in-time usage against limits — a reading, not a trend.
    `usage` is keyed "namespace/pod" (see `read.get_pod_metrics`)."""
    findings: list[dict] = []
    by_pod = {f"{p.get('namespace')}/{p.get('name')}": p for p in pods}
    for key, containers in usage.items():
        pod = by_pod.get(key)
        if not pod:
            continue
        for container, usage in containers.items():
            limits = (pod.get("limits") or {}).get(container) or {}
            for resource in ("memory", "cpu"):
                if resource not in limits or resource not in usage:
                    continue
                try:
                    used = quantities.to_units(resource, usage[resource])
                    limit = quantities.to_units(resource, limits[resource])
                except ValueError:
                    continue
                if limit and used / limit >= USAGE_WARNING_RATIO:
                    findings.append(
                        _finding(
                            "warning",
                            "Pod",
                            pod,
                            f"Near{resource.capitalize()}Limit",
                            f"{quantities.format_units(resource, used)} used of {limits[resource]} limit "
                            f"({used / limit:.0%}, point-in-time reading)",
                            container,
                        )
                    )
    return findings


def sort_and_bound(findings: list[dict], limit: int) -> tuple[list[dict], int]:
    ordered = sorted(findings, key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 9), f.get("namespace") or "", f.get("name") or ""))
    return ordered[:limit], max(0, len(ordered) - limit)
