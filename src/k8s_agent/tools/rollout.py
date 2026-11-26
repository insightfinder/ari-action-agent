"""Verifying a change by the state it produced, not by the API accepting it.

An accepted patch and a live change are different statements. Success
follows Kubernetes' own rollout semantics, and the wait is bounded by the
Deployment's own `progressDeadlineSeconds` — InsightFinder's Java services
take minutes to start (startup probes allow ~160 s per pod), so any fixed
short timeout would report healthy rollouts as failures.

Pods that cannot be scheduled are classified from Cluster Autoscaler's
events and status (see `capacity.classify_pending`): a scale-up in
progress is waited for; a node group that cannot grow, or provisioning
that keeps failing (an EC2 quota, or no spot capacity), ends the wait early with the
reason. Nothing here ever rolls a change back — that is a change of its
own and needs its own request and approval.

A rollout that looks finished is watched a little longer before it is
called a success: a container that is killed for its memory limit is
"ready" for a few seconds after every start, so the first ready look
proves nothing (found live, 2026-10-14: a limit raised from 32Mi to 256Mi
was reported "succeeded" after 10 s while the new pod kept OOM-crashing).
"""

import asyncio
import time
from datetime import datetime

from . import capacity, governance, quantities, read

_FAILING_WAIT_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
}
POLL_SECONDS = 10
# How long new pods must stay up, without a restart, after the rollout looks done.
STABILITY_SECONDS = 45


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _new_pods(pods: list[dict], applied_at: datetime | None) -> list[dict]:
    if applied_at is None:
        return pods
    out = []
    for pod in pods:
        created = _parse(pod.get("created_at"))
        if created is None or created >= applied_at:
            out.append(pod)
    return out


def _pending(pods: list[dict]) -> list[dict]:
    out = []
    for pod in pods:
        if pod.get("phase") != "Pending" or pod.get("deleting"):
            continue
        scheduled = next((c for c in pod.get("conditions") or [] if c.get("type") == "PodScheduled"), None)
        if scheduled and scheduled.get("status") == "False":
            out.append(pod)
    return out


def _failing(pods: list[dict]) -> list[dict]:
    failing = []
    for pod in pods:
        for c in pod.get("containers") or []:
            state = c.get("state") or {}
            if state.get("state") == "waiting" and state.get("reason") in _FAILING_WAIT_REASONS:
                last = c.get("last_state") or {}
                failing.append(
                    {
                        "pod": pod.get("name"),
                        "container": c.get("name"),
                        "reason": state.get("reason"),
                        "last_termination": (
                            f"{last.get('reason')} (exit code {last.get('exit_code')})"
                            if last.get("state") == "terminated"
                            else None
                        ),
                    }
                )
    return failing


def _restart_counts(pods: list[dict]) -> dict:
    return {
        (pod.get("name"), c.get("name")): c.get("restart_count") or 0
        for pod in pods
        for c in pod.get("containers") or []
    }


def _restarted_since(baseline: dict, pods: list[dict]) -> list[dict]:
    """New pods whose containers restarted, or are down, since the rollout first looked done."""
    out = []
    for pod in pods:
        for c in pod.get("containers") or []:
            key = (pod.get("name"), c.get("name"))
            state = c.get("state") or {}
            down = state.get("state") == "terminated" and (state.get("exit_code") or 0) != 0
            if (c.get("restart_count") or 0) > baseline.get(key, 0) or down or (
                state.get("state") == "waiting" and state.get("reason") in _FAILING_WAIT_REASONS
            ):
                last = c.get("last_state") or state
                out.append(
                    {
                        "pod": pod.get("name"),
                        "container": c.get("name"),
                        "reason": state.get("reason") or "Restarted",
                        "last_termination": (
                            f"{last.get('reason')} (exit code {last.get('exit_code')})"
                            if last.get("state") == "terminated"
                            else None
                        ),
                    }
                )
    return out


def _resources_match(pods: list[dict], container: str, applied: dict) -> bool:
    """At least one ready pod runs `container` with every applied value."""
    for pod in pods:
        if not any(c.get("ready") for c in pod.get("containers") or []):
            continue
        actual = (pod.get("resources") or {}).get(container) or {}
        ok = True
        for side, values in (applied or {}).items():
            for resource, value in (values or {}).items():
                have = (actual.get(side) or {}).get(resource)
                try:
                    if have is None or quantities.to_units(resource, have) != quantities.to_units(resource, value):
                        ok = False
                except ValueError:
                    ok = False
        if ok:
            return True
    return False


def moved_away(dep: dict, target: dict) -> str | None:
    """What no longer holds the approved value on the Deployment, or None. Something that manages the
    Deployment (a GitOps reconcile, a Helm upgrade, a person) can put its own value back while the
    rollout is watched; the rollout then never completes, and that is not "still in progress"."""
    kind = target.get("kind")
    if kind == "replicas":
        if dep.get("replicas") != target.get("replicas"):
            return f"spec.replicas is {dep.get('replicas')} again, not the approved {target.get('replicas')}"
        return None
    if kind != "resources":
        return None
    container = target.get("container")
    current = next((c.get("resources") or {} for c in (dep.get("template") or {}).get("containers") or [] if c.get("name") == container), None)
    if current is None:
        return f"container {container} is no longer in the pod template"
    for side, values in (target.get("resources") or {}).items():
        for resource, value in (values or {}).items():
            have = (current.get(side) or {}).get(resource)
            try:
                same = have is not None and quantities.to_units(resource, have) == quantities.to_units(resource, value)
            except ValueError:
                same = False
            if not same:
                return f"{side}.{resource} of container {container} is {have} again, not the approved {value}"
    return None


def _about_this_change(cond: dict, generation_ok: bool, applied_at: datetime | None) -> bool:
    """Whether a Deployment condition describes the rollout this change started.

    A Deployment already stuck before the change (the case a resource fix is
    usually approved for) still carries the old ProgressDeadlineExceeded until
    the controller acts on the patch. Until it has observed the new generation,
    and for a condition last updated before the change was applied, the
    condition is the previous rollout's verdict, not this one's.
    """
    if not generation_ok:
        return False
    updated = _parse(cond.get("last_update"))
    return applied_at is None or updated is None or updated >= applied_at


def assess(dep: dict, target: dict, pods: list[dict], sched_events: list[dict], ca: dict, applied_at: datetime | None) -> dict:
    """One look at the cluster: `done` + `status`, or still in progress."""
    status = dep.get("status") or {}
    counts = {
        "desired": dep.get("replicas"),
        "updated": status.get("updated"),
        "ready": status.get("ready"),
        "available": status.get("available"),
    }
    generation_ok = (dep.get("observed_generation") or 0) >= (dep.get("generation") or 0)
    for cond in dep.get("conditions") or []:
        if (
            cond.get("type") == "Progressing"
            and cond.get("reason") == "ProgressDeadlineExceeded"
            and _about_this_change(cond, generation_ok, applied_at)
        ):
            return {"done": True, "status": "rollout_failed", "detail": cond.get("message"), "replicas": counts}

    new_pods = _new_pods(pods, applied_at)
    failing = _failing(new_pods)
    if failing:
        return {"done": True, "status": "new_pods_failing", "failing": failing[:10], "replicas": counts}

    pending = _pending(new_pods)
    scheduling = None
    if pending:
        scheduling = capacity.classify_pending(pending, sched_events, ca)
        scheduling["pending_pods"] = len(pending)
        scheduling["node_groups"] = capacity.node_group_summary(ca)
        if scheduling["stop_waiting"]:
            return {"done": True, "status": "unschedulable", "scheduling": scheduling, "replicas": counts}

    kind = target.get("kind")
    if kind == "autoscaler":
        # The autoscaler moves the Deployment into the new bounds: at least `min` available, and no
        # more than `max` replicas (a lowered max scales down).
        minimum, maximum = target.get("min"), target.get("max")
        within_min = minimum is None or (status.get("available") or 0) >= minimum
        within_max = maximum is None or (dep.get("replicas") or 0) <= maximum
        if within_min and within_max:
            return {"done": True, "status": "succeeded", "replicas": counts}
    else:
        desired = target.get("replicas", dep.get("replicas"))
        progressing = next((c for c in dep.get("conditions") or [] if c.get("type") == "Progressing"), None)
        complete = (
            generation_ok
            and dep.get("replicas") == desired
            and all((status.get(k) or 0) == desired for k in ("updated", "ready", "available"))
            and (progressing is None or progressing.get("reason") == "NewReplicaSetAvailable")
        )
        if complete and kind == "resources":
            complete = _resources_match(pods, target.get("container"), target.get("resources"))
        if complete:
            return {"done": True, "status": "succeeded", "replicas": counts}

    return {"done": False, "status": "in_progress", "scheduling": scheduling, "replicas": counts}


async def wait_rollout(
    namespace: str,
    deployment: str,
    target: dict,
    applied_at: str,
    max_wait_seconds: float,
    poll_seconds: float = POLL_SECONDS,
) -> dict:
    """Poll until `assess` is done or `max_wait_seconds` elapse."""
    applied = _parse(applied_at)
    started = time.monotonic()
    stable_since: float | None = None
    baseline: dict = {}
    while True:
        dep = await read.get_deployment(namespace, deployment)
        elapsed = round(time.monotonic() - started)
        moved = moved_away(dep, target)
        if moved:
            return {
                "done": True,
                "status": "reverted",
                "detail": (
                    f"The change was applied, then undone: {moved}. Something that manages this Deployment, "
                    "most likely a GitOps reconcile or a Helm upgrade, restored its own value."
                ),
                "waited_seconds": elapsed,
            }
        pods = await read.list_pods(namespace, selector=dep.get("label_selector") or dep.get("selector") or None)
        sched_events: list[dict] = []
        ca: dict = {"available": False, "reason": "not needed"}
        if _pending(_new_pods(pods, applied)):
            sched_events = await read.list_scheduling_events(namespace)
            ca = await read.get_ca_status()
        verdict = assess(dep, target, pods, sched_events, ca, applied)
        elapsed = round(time.monotonic() - started)
        new_pods = _new_pods(pods, applied)
        if stable_since is not None:
            restarted = _restarted_since(baseline, new_pods)
            if restarted:
                return {
                    "done": True,
                    "status": "new_pods_failing",
                    "failing": restarted[:10],
                    "detail": f"new pods restarted within {round(time.monotonic() - stable_since)}s of becoming ready",
                    "replicas": verdict.get("replicas"),
                    "waited_seconds": elapsed,
                }
        if verdict["done"] and verdict["status"] == "succeeded":
            if stable_since is None:
                stable_since, baseline = time.monotonic(), _restart_counts(new_pods)
            observed = time.monotonic() - stable_since
            if observed >= STABILITY_SECONDS or elapsed >= max_wait_seconds:
                verdict["waited_seconds"] = elapsed
                verdict["stable_for_seconds"] = round(observed)
                if observed < STABILITY_SECONDS:
                    verdict["note"] = (
                        f"Pods were watched for only {round(observed)}s after becoming ready (the wait window ended); "
                        "check the Deployment again later."
                    )
                return verdict
            await asyncio.sleep(min(poll_seconds, max(1.0, STABILITY_SECONDS - observed)))
            continue
        stable_since = None
        if verdict["done"]:
            verdict["waited_seconds"] = elapsed
            return verdict
        if elapsed >= max_wait_seconds:
            scheduling = verdict.get("scheduling")
            if scheduling and scheduling.get("cause") == "failed_scheduling":
                verdict["status"] = "unschedulable"
            else:
                verdict["status"] = "in_progress"
                verdict["note"] = (
                    "The change is applied but the rollout had not finished when the wait window "
                    f"({int(max_wait_seconds)}s) ended"
                    + ("; Cluster Autoscaler is still adding nodes" if scheduling and scheduling.get("cause") == "scaling_up" else "")
                    + ". This is not a failure; check the Deployment again later."
                )
            verdict["done"] = True
            verdict["waited_seconds"] = elapsed
            return verdict
        await asyncio.sleep(min(poll_seconds, max(1.0, max_wait_seconds - elapsed)))


# ---------------------------------------------------------------- in-place pod resize


def _container_status(pod: dict, container: str) -> dict:
    return next((c for c in pod.get("containers") or [] if c.get("name") == container), {})


def _resize_condition(pod: dict, kind: str) -> dict | None:
    return next((c for c in pod.get("conditions") or [] if c.get("type") == kind and c.get("status") == "True"), None)


def _status_matches(pod: dict, container: str, approved: dict) -> bool:
    actual = (pod.get("status_resources") or {}).get(container)
    if actual is None:
        return False
    for side, values in (approved or {}).items():
        for resource, value in (values or {}).items():
            have = (actual.get(side) or {}).get(resource)
            try:
                if have is None or quantities.to_units(resource, have) != quantities.to_units(resource, value):
                    return False
            except ValueError:
                return False
    return True


async def wait_resize(
    namespace: str,
    pod: str,
    container: str,
    resources: dict,
    uid: str | None,
    max_wait_seconds: float,
    restarts_before: int | None = None,
    poll_seconds: float = POLL_SECONDS,
) -> dict:
    """Poll one pod after an in-place resize until the kubelet reports the approved values as applied.

    Succeeded: the same pod (uid) shows the approved values in `status.containerStatuses[].resources`, no
    PodResizePending/PodResizeInProgress condition is left, and the container is ready and stays up without a
    further restart for STABILITY_SECONDS (a restart is expected once under `RestartContainer`; a lowered
    memory limit can OOM-kill the restarted process right after). With `restarts_before` set (a restart is
    expected), nothing counts as applied until the container has restarted past it, so the old container
    still reporting ready is never taken as the result. Infeasible ends the wait at once; Deferred
    (the node has the room only once other pods shrink or leave) is waited for until the window ends.
    """
    started = time.monotonic()
    stable_since: float | None = None
    baseline = 0
    while True:
        view = await read.get_pod(namespace, pod)
        elapsed = round(time.monotonic() - started)
        base = {"pod": pod, "container": container, "waited_seconds": elapsed}
        if view is None or (uid and view.get("uid") != uid):
            return {
                **base,
                "done": True,
                "status": "pod_replaced",
                "detail": (
                    "The pod was replaced while the resize was being watched (it no longer exists, or has a new uid); "
                    "its replacement runs with the Deployment template's values."
                ),
            }
        pending = _resize_condition(view, "PodResizePending")
        in_progress = _resize_condition(view, "PodResizeInProgress")
        status = _container_status(view, container)
        state = status.get("state") or {}
        failing = state.get("state") == "waiting" and state.get("reason") in _FAILING_WAIT_REASONS
        base["restart_count"] = status.get("restart_count")
        if pending and pending.get("reason") == "Infeasible":
            return {
                **base,
                "done": True,
                "status": "resize_infeasible",
                "detail": f"The kubelet cannot apply this resize on the pod's node: {pending.get('message') or 'Infeasible'}",
            }
        applied = _status_matches(view, container, resources)
        if applied and failing:
            last = status.get("last_state") or {}
            return {
                **base,
                "done": True,
                "status": "container_failing",
                "detail": (
                    f"The resize was applied but container {container} is failing: {state.get('reason')}"
                    + (f" (last termination {last.get('reason')}, exit code {last.get('exit_code')})" if last.get("state") == "terminated" else "")
                ),
            }
        restarted = restarts_before is None or (status.get("restart_count") or 0) > restarts_before
        if applied and restarted and not pending and not in_progress and status.get("ready"):
            if stable_since is None:
                stable_since, baseline = time.monotonic(), status.get("restart_count") or 0
            elif (status.get("restart_count") or 0) > baseline:
                last = status.get("last_state") or {}
                return {
                    **base,
                    "done": True,
                    "status": "container_failing",
                    "detail": (
                        f"Container {container} restarted again within {round(time.monotonic() - stable_since)}s of the resize "
                        + (f"({last.get('reason')}, exit code {last.get('exit_code')})" if last.get("state") == "terminated" else "")
                    ).strip(),
                }
            observed = time.monotonic() - stable_since
            if observed >= STABILITY_SECONDS:
                return {**base, "done": True, "status": "succeeded", "stable_for_seconds": round(observed), "node": view.get("node_name")}
        else:
            stable_since = None
        if elapsed >= max_wait_seconds:
            if pending and pending.get("reason") == "Deferred":
                return {
                    **base,
                    "done": True,
                    "status": "resize_deferred",
                    "detail": (
                        "The resize is accepted but deferred: the node does not have the room right now; the kubelet "
                        f"applies it once it does. {pending.get('message') or ''}"
                    ).strip(),
                }
            note = (in_progress or {}).get("message")
            return {
                **base,
                "done": True,
                "status": "in_progress",
                "note": (
                    "The resize was accepted but had not been reported as applied when the wait window "
                    f"({int(max_wait_seconds)}s) ended" + (f" ({note})" if note else "") + "; check the pod again later."
                ),
            }
        await asyncio.sleep(min(poll_seconds, max(1.0, max_wait_seconds - elapsed)))


# ---------------------------------------------------------------- a deleted pod


def _pod_ready(pod: dict) -> bool:
    containers = pod.get("containers") or []
    return pod.get("phase") == "Running" and not pod.get("deleting") and bool(containers) and all(c.get("ready") for c in containers)


def desired_pods(view: dict) -> int:
    return view.get("replicas") or 0


async def _scheduling(namespace: str, pending: list[dict]) -> dict | None:
    if not pending:
        return None
    events = await read.list_scheduling_events(namespace)
    ca = await read.get_ca_status()
    scheduling = capacity.classify_pending(pending, events, ca)
    scheduling["pending_pods"] = len(pending)
    scheduling["node_groups"] = capacity.node_group_summary(ca)
    return scheduling


async def wait_pod_replaced(
    namespace: str,
    pod: str,
    uid: str,
    workload_kind: str,
    workload: str,
    applied_at: str | None,
    max_wait_seconds: float,
    poll_seconds: float = POLL_SECONDS,
) -> dict:
    """Poll after a pod was deleted until its controller has replaced it.

    Succeeded needs positive evidence, because a delete bumps no generation and the workload's counts can
    look complete before anything happened: the pod with `uid` is gone (a replacement could reuse
    the name, so the uid decides), a pod of the workload created after the delete is ready, and the new pods
    stay up without a restart for STABILITY_SECONDS. The workload's other pods are reported (`ready` of
    `desired`) but not waited for: one deleted pod does not mend a sibling that is itself failing.
    """
    applied = _parse(applied_at)
    started = time.monotonic()
    stable_since: float | None = None
    baseline: dict = {}
    while True:
        current = await read.get_pod(namespace, pod)
        old_gone = current is None or current.get("uid") != uid
        view = await read.get_workload(workload_kind, namespace, workload)
        pods = await read.list_pods(namespace, selector=view.get("label_selector") or view.get("selector") or None, active_only=True)
        own = [p for p in pods if governance.owned_by(p, workload_kind, workload)]
        new = [p for p in _new_pods(own, applied) if p.get("uid") != uid]
        elapsed = round(time.monotonic() - started)
        base = {"pod": pod, "replacement": sorted(p.get("name") for p in new), "waited_seconds": elapsed}
        failing = _failing(new)
        if failing:
            return {**base, "done": True, "status": "new_pods_failing", "failing": failing[:10]}
        scheduling = await _scheduling(namespace, _pending(new))
        if scheduling and scheduling.get("stop_waiting"):
            return {**base, "done": True, "status": "unschedulable", "scheduling": scheduling}
        desired = desired_pods(view)
        ready = sum(1 for p in own if _pod_ready(p))
        if stable_since is not None:
            restarted = _restarted_since(baseline, new)
            if restarted:
                return {
                    **base,
                    "done": True,
                    "status": "new_pods_failing",
                    "failing": restarted[:10],
                    "detail": f"the replacement restarted within {round(time.monotonic() - stable_since)}s of becoming ready",
                }
        if old_gone and any(_pod_ready(p) for p in new):
            if stable_since is None:
                stable_since, baseline = time.monotonic(), _restart_counts(new)
            observed = time.monotonic() - stable_since
            if observed >= STABILITY_SECONDS:
                return {**base, "done": True, "status": "succeeded", "ready": ready, "desired": desired, "stable_for_seconds": round(observed)}
        else:
            stable_since = None
        if elapsed >= max_wait_seconds:
            if scheduling and scheduling.get("cause") == "failed_scheduling":
                return {**base, "done": True, "status": "unschedulable", "scheduling": scheduling}
            return {
                **base,
                "done": True,
                "status": "in_progress",
                "ready": ready,
                "desired": desired,
                "note": (
                    f"The pod was deleted, but when the wait window ({int(max_wait_seconds)}s) ended "
                    + ("it was still terminating (graceful shutdown or a finalizer)" if not old_gone else "its replacement was not ready yet")
                    + ". This is not a failure; check the pod's workload again later."
                ),
            }
        await asyncio.sleep(min(poll_seconds, max(1.0, max_wait_seconds - elapsed)))


async def wait_pvc_expanded(
    namespace: str,
    pvc: str,
    uid: str,
    storage: str,
    max_wait_seconds: float,
    poll_seconds: float = POLL_SECONDS,
) -> dict:
    """Poll after a PVC's size was raised until the volume reports it.

    `succeeded` needs the PVC's own status: its capacity at least the approved size (the storage driver grew the
    volume and, for a mounted filesystem, the kubelet grew the filesystem). A resize the driver or the kubelet
    reports as infeasible is `resize_failed`. Still short when the wait ends is `resize_pending`, not a failure:
    the size is asked for and the volume grows on its own (a volume no pod mounts grows its filesystem when next
    mounted). The same PVC (uid) throughout, or `pvc_replaced`.
    """
    target = quantities.memory_bytes(storage)
    started = time.monotonic()
    while True:
        current = await read.get_pvc(namespace, pvc)
        if current is None or current.get("uid") != uid:
            return {
                "status": "pvc_replaced",
                "detail": f"PVC {namespace}/{pvc} " + ("no longer exists" if current is None else "was replaced (new uid)") + " after the change.",
            }
        observed = {
            "requested": current.get("requested"),
            "capacity": current.get("capacity"),
            "allocated": current.get("allocated") or None,
            "resize_status": current.get("resize_status"),
            "conditions": current.get("conditions") or [],
        }
        capacity = quantities.memory_bytes(current["capacity"]) if current.get("capacity") else 0
        if capacity >= target:
            return {"status": "succeeded", **observed, "detail": f"The volume now has {current.get('capacity')} (asked {storage})."}
        if str(current.get("resize_status") or "").endswith("Infeasible"):
            return {
                "status": "resize_failed",
                **observed,
                "detail": f"The resize to {storage} was rejected as infeasible ({current.get('resize_status')}); the volume keeps {current.get('capacity')}.",
            }
        if time.monotonic() - started >= max_wait_seconds:
            return {
                "status": "resize_pending",
                **observed,
                "detail": (
                    f"{storage} is asked for and the volume still has {current.get('capacity')} after "
                    f"{int(max_wait_seconds)} s (state: {current.get('resize_status') or ', '.join(observed['conditions']) or 'none reported'}). "
                    "It grows on its own; a volume no pod mounts grows its filesystem when it is next mounted."
                ),
            }
        await asyncio.sleep(poll_seconds)
