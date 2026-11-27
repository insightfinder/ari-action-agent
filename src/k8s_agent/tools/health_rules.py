"""Deterministic Pod/Deployment health rules.

The model never decides whether something is unhealthy — it only
summarises what these rules found. Each finding names its evidence, so a
reader (or the summary) can tell an OOM kill from a failing probe from a
missing image without re-deriving anything.
"""

from datetime import datetime

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


# Exit codes of a process ended by a signal: 128 + SIGKILL (9) or SIGTERM (15). A probe restart is SIGTERM, then
# SIGKILL after the grace period, so it ends as 143, 137, or 0 ("Completed") when the process stops cleanly on
# SIGTERM (all three seen on test). Only the runtime's OOMKilled reason and the kubelet's probe-kill event tell
# the causes apart; the exit code alone does not.
_SIGNALLED = {137, 143}
_LABEL = {"startup_probe": "killed by startup probe", "liveness_probe": "killed by liveness probe"}


def _seconds_between(start, end) -> int | None:
    try:
        a = datetime.fromisoformat(str(start))
        b = datetime.fromisoformat(str(end))
    except ValueError:
        return None
    return max(0, round((b - a).total_seconds()))


def probe_allowance(probe: dict) -> int:
    """About how long a probe lets a container run, from its start, before the kubelet restarts it when the
    probe never passes: its first check after initialDelaySeconds, then failureThreshold failures periodSeconds
    apart."""
    return int(probe.get("initial_delay_seconds") or 0) + int(probe.get("failure_threshold") or 3) * int(probe.get("period_seconds") or 10)


def _timing(kind: str, probe: dict) -> str:
    return (
        f"{kind}Probe allows about {probe_allowance(probe)}s (initialDelaySeconds {probe.get('initial_delay_seconds')} + "
        f"failureThreshold {probe.get('failure_threshold')} x periodSeconds {probe.get('period_seconds')}; "
        f"timeoutSeconds {probe.get('timeout_seconds')})"
    )


def kill_cause(pod: dict, container: dict, kills: list[dict] | None) -> dict | None:
    """What ended the container's last run, when the cluster recorded it: `killed_by` `oom` (the runtime's
    OOMKilled), `startup_probe` / `liveness_probe` (a kubelet probe-kill event for this container), or
    `not_recorded` (ended by a signal with neither on record: the events may have expired). None when the last
    run ended on its own with an error code (an ordinary crash) or there is no last run."""
    last = container.get("last_state") or {}
    if last.get("state") != "terminated":
        last = container.get("state") or {}
    if last.get("state") != "terminated":
        return None
    name = container.get("name")
    ran = _seconds_between(last.get("started_at"), last.get("finished_at"))
    if last.get("reason") == "OOMKilled":
        return {"killed_by": "oom", "last_run_seconds": ran, "diagnosis": "killed for exceeding its memory limit (OOMKilled), not by a probe"}
    kill = next(
        (k for k in kills or [] if (k.get("namespace"), k.get("pod"), k.get("container")) == (pod.get("namespace"), pod.get("name"), name)),
        None,
    )
    if kill is None:
        if last.get("exit_code") not in _SIGNALLED:
            return None
        return {
            "killed_by": "not_recorded",
            "last_run_seconds": ran,
            "diagnosis": (
                f"ended by a signal (exit code {last.get('exit_code')}) and not recorded as OOMKilled; no probe kill "
                "of this container is in the last 60 minutes of events (they expire), so what sent it is not on "
                "record: a probe restart whose events expired, a stop that outlasted its grace period, or a kill from "
                "outside the container"
            ),
        }
    kind = kill["probe"]
    probe = ((pod.get("probes") or {}).get(name) or {}).get(kind) or {}
    startup = ((pod.get("probes") or {}).get(name) or {}).get("startup")
    cause = {
        "killed_by": f"{kind}_probe",
        "probe_kills": kill.get("count") or 1,
        "last_run_seconds": ran,
        "probe": {"kind": kind, "allows_seconds": probe_allowance(probe) if probe else None,
                  **{k: v for k, v in probe.items() if k != "check_digest"}},
    }
    ran_text = f"after running {ran}s" if ran is not None else "(run time not recorded)"
    # A Deployment's pod (owned by a ReplicaSet): this agent can propose the fix itself (set_startup_probe).
    ask = (
        "; for a Deployment this agent can propose it when asked with a time, e.g. \"give deployment <name> "
        "10 minutes to start\" (it lasts until the Deployment is next rendered from Git or Helm)"
        if pod.get("owner_kind") == "ReplicaSet"
        else ""
    )
    if kind == "startup":
        cause["diagnosis"] = (
            f"restarted by its startup probe {ran_text}: it had not passed the startup check in time; "
            f"{_timing('startup', probe) if probe else 'startupProbe timing unknown'}. If the container needs "
            "longer to start (slow initialisation, recovery, an index rebuild: see its previous log), give it "
            "more: raise startupProbe.failureThreshold (or periodSeconds) where the workload is defined (its chart "
            "values when Helm or Flux manages it)" + ask
        )
        return cause
    grace = int(pod.get("termination_grace_seconds") or 30)
    slack = int(probe.get("period_seconds") or 10) + int(probe.get("timeout_seconds") or 1) + grace
    during_start = bool(probe) and startup is None and ran is not None and ran <= probe_allowance(probe) + slack
    cause["during_startup"] = during_start
    if during_start:
        cause["diagnosis"] = (
            f"restarted by its liveness probe {ran_text}, within the time the liveness probe allows from start "
            f"({_timing('liveness', probe)}), and it has no startup probe: it is likely killed while still "
            "starting. If it needs longer to start, add a startupProbe with the same check as the liveness probe "
            "and enough time to start (it holds the liveness probe off until it passes) where the workload is "
            "defined, rather than loosening the liveness probe" + ask
        )
    else:
        cause["diagnosis"] = (
            f"restarted by its liveness probe {ran_text}: it stopped passing its liveness check while running "
            f"(hung, overloaded, or a check slower than timeoutSeconds); "
            f"{_timing('liveness', probe) if probe else 'livenessProbe timing unknown'}"
        )
    return cause


def _label(cause: dict | None, last: dict | None) -> str:
    if cause and cause["killed_by"] in _LABEL:
        return _LABEL[cause["killed_by"]]
    return (last or {}).get("reason") or "terminated"


def pod_findings(pod: dict, kills: list[dict] | None = None) -> list[dict]:
    """`kills`: the probe kills on record (`read.list_probe_kills`), to name what restarted a container."""
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

        cause = kill_cause(pod, container, kills) if restarts or state.get("state") == "terminated" else None

        if state.get("state") == "waiting" and state.get("reason") in _WAITING_ERRORS:
            reason = state["reason"]
            evidence = state.get("message") or reason
            last_termination = _termination(last)
            if reason == "CrashLoopBackOff" and last_termination:
                # The previous container's termination is the actual cause: OOMKilled, or a probe kill the
                # kubelet recorded; the exit code alone does not tell them apart.
                reason = f"CrashLoopBackOff ({_label(cause, last)})"
                evidence = f"previous container {last_termination}; restarts {restarts}"
            findings.append(_with_cause(_finding("critical", "Pod", pod, reason, evidence, name), cause))
            continue

        if state.get("state") == "terminated" and ((state.get("exit_code") or 0) != 0 or (cause and cause["killed_by"] in _LABEL)):
            if restarts >= 1:
                # Between two restarts of a crash loop the kubelet shows the
                # container that just exited as `terminated` for a few
                # seconds before it reports CrashLoopBackOff (found live,
                # 2026-10-14). Same condition, so the same finding.
                findings.append(
                    _with_cause(
                        _finding(
                            "critical",
                            "Pod",
                            pod,
                            f"CrashLoopBackOff ({_label(cause, state)})",
                            f"container {_termination(state)}, being restarted; restarts {restarts}",
                            name,
                        ),
                        cause,
                    )
                )
            else:
                findings.append(_with_cause(_finding("warning", "Pod", pod, state.get("reason") or "Error", _termination(state), name), cause))
            continue

        if restarts >= RESTART_WARNING_THRESHOLD or (restarts and cause and cause["killed_by"] in _LABEL):
            detail = _termination(last) or "unknown cause"
            # Named after the last termination, as a crash loop is: an OOM-killed container spends
            # most of its time running when its limit takes a while to fill (and a slow start killed by
            # its probe mostly runs too, until the probe gives up on it).
            terminated = (last or {}).get("state") == "terminated"
            label = _label(cause, last) if terminated or cause else None
            if restarts >= RESTART_WARNING_THRESHOLD:
                reason = f"FrequentRestarts ({label})" if label else "FrequentRestarts"
            else:
                reason = f"Restarted ({label})"
            findings.append(_with_cause(_finding("warning", "Pod", pod, reason, f"{restarts} restarts; last: {detail}", name), cause))

        if state.get("state") == "running" and not container.get("ready"):
            findings.append(_finding("info", "Pod", pod, "NotReady", "container running but not ready", name))
    return findings


def _with_cause(finding: dict, cause: dict | None) -> dict:
    if cause:
        finding.update(cause)
    return finding


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


def workload_findings(view: dict) -> list[dict]:
    """A StatefulSet or DaemonSet with fewer ready pods than it wants; a suspended CronJob (info: it may be on
    purpose, but no scheduled run starts)."""
    kind = view.get("kind")
    status = view.get("status") or {}
    if kind == "CronJob":
        if view.get("suspend"):
            return [_finding("info", kind, view, "Suspended", f"no scheduled run starts (schedule {view.get('schedule')!r})")]
        return []
    desired = (status.get("desired") if kind == "DaemonSet" else view.get("replicas")) or 0
    ready = status.get("ready") or 0
    if ready < desired:
        return [_finding("warning", kind, view, "ReplicasUnavailable", f"{ready}/{desired} pods ready")]
    return []


def node_findings(node: dict) -> list[dict]:
    """A node that is not Ready (warning) or does not take new pods (info: often on purpose)."""
    findings = []
    if not node.get("ready"):
        findings.append(_finding("warning", "Node", node, "NotReady", "the node's Ready condition is not True"))
    if node.get("unschedulable"):
        findings.append(_finding("info", "Node", node, "Cordoned", "spec.unschedulable: new pods are not scheduled here"))
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
