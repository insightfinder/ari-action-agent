"""Node capacity: the per-pod cap for resource changes, whether new pods
will fit, and what the Cluster Autoscaler says when they don't.

Every EKS cluster this agent runs in has the Cluster Autoscaler (CA), so
a pod that does not fit *today* is normally just waiting for a node. The
cases worth raising are the ones where no node is coming: the node group
is at its max size, or provisioning keeps failing (most often an EC2
quota). Those are read from CA's own status ConfigMap and from the
events it puts on pending pods, never guessed.
"""

import math

import yaml

from . import quantities

DEFAULT_HEADROOM = 0.2


# ---------------------------------------------------------------- matching


def _expression_matches(expr: dict, labels: dict) -> bool:
    key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
    present = key in labels
    value = labels.get(key)
    if op == "In":
        return present and value in values
    if op == "NotIn":
        return not present or value not in values
    if op == "Exists":
        return present
    if op == "DoesNotExist":
        return not present
    if op in ("Gt", "Lt") and present and values:
        try:
            left, right = int(value), int(values[0])
        except ValueError:
            return False
        return left > right if op == "Gt" else left < right
    return False


def _tolerates(taint: dict, tolerations: list[dict]) -> bool:
    for tol in tolerations:
        if tol.get("effect") and tol.get("effect") != taint.get("effect"):
            continue
        if tol.get("operator") == "Exists":
            if not tol.get("key") or tol.get("key") == taint.get("key"):
                return True
            continue
        if tol.get("key") == taint.get("key") and (tol.get("value") or "") == (taint.get("value") or ""):
            return True
    return False


def node_is_candidate(node: dict, template: dict) -> bool:
    """Could a pod from `template` be scheduled on `node` at all?"""
    if not node.get("ready") or node.get("unschedulable"):
        return False
    labels = node.get("labels") or {}
    for key, value in (template.get("node_selector") or {}).items():
        if labels.get(key) != value:
            return False
    terms = template.get("required_node_affinity") or []
    if terms and not any(all(_expression_matches(e, labels) for e in term) for term in terms):
        return False
    for taint in node.get("taints") or []:
        if taint.get("effect") in ("NoSchedule", "NoExecute") and not _tolerates(taint, template.get("tolerations") or []):
            return False
    return True


# ---------------------------------------------------------------- amounts


def _amount(values: dict, resource: str) -> int:
    raw = (values or {}).get(resource)
    if raw in (None, ""):
        return 0
    try:
        return quantities.to_units(resource, raw)
    except ValueError:
        return 0


def pod_requests(pod: dict) -> dict:
    total = {"cpu": 0, "memory": 0}
    for requests in pod.get("requests") or []:
        for resource in total:
            total[resource] += _amount(requests, resource)
    return total


def _active(pod: dict) -> bool:
    return pod.get("phase") not in ("Succeeded", "Failed") and not pod.get("deleting")


def _pods_on(node_name: str, pods: list[dict]) -> list[dict]:
    return [p for p in pods if p.get("node_name") == node_name and _active(p)]


def node_allocatable(node: dict) -> dict:
    return {r: _amount(node.get("allocatable"), r) for r in ("cpu", "memory")}


def daemonset_requests(node: dict, pods: list[dict]) -> dict:
    total = {"cpu": 0, "memory": 0}
    for pod in _pods_on(node["name"], pods):
        if pod.get("owner_kind") == "DaemonSet":
            req = pod_requests(pod)
            for r in total:
                total[r] += req[r]
    return total


def free_capacity(node: dict, pods: list[dict]) -> dict:
    alloc = node_allocatable(node)
    used = {"cpu": 0, "memory": 0}
    for pod in _pods_on(node["name"], pods):
        req = pod_requests(pod)
        for r in used:
            used[r] += req[r]
    return {r: max(0, alloc[r] - used[r]) for r in alloc}


# ---------------------------------------------------------------- per-pod cap


def _container_requests(container: dict) -> dict:
    requests = (container.get("resources") or {}).get("requests") or {}
    return {r: _amount(requests, r) for r in ("cpu", "memory")}


def resource_cap(
    nodes: list[dict],
    pods: list[dict],
    template: dict,
    headroom: float = DEFAULT_HEADROOM,
    container: str | None = None,
    wanted: dict | None = None,
) -> dict | None:
    """The most `container` can be given on this cluster's hardware, with headroom.

    Per candidate node: allocatable minus what that node's DaemonSet pods
    request (they run on every node regardless), times (1 - headroom), minus
    what the pod's other containers request, since the pod is scheduled as a
    whole. CPU and memory come from the same node, because the pod has to fit
    on one: the node that can give the largest share of `wanted` (units per
    resource being changed), passing over nodes that cannot hold the
    container's current request of a resource not being changed. Without
    `wanted`, the node with the most room in its tighter resource. Returns
    None when no node could run the pod at all.
    """
    candidates = [n for n in nodes if node_is_candidate(n, template)]
    if not candidates:
        return None
    factor = 1 - max(0.0, min(headroom, 0.9))
    others = {"cpu": 0, "memory": 0}
    current = {"cpu": 0, "memory": 0}
    if container is not None:
        for c in template.get("containers") or []:
            into = current if c.get("name") == container else others
            for resource, amount in _container_requests(c).items():
                into[resource] += amount
    wanted = {r: v for r, v in (wanted or {}).items() if v}

    def room(node: dict) -> dict:
        alloc, ds = node_allocatable(node), daemonset_requests(node, pods)
        return {r: max(0, math.floor(max(0, alloc[r] - ds[r]) * factor) - others[r]) for r in ("cpu", "memory")}

    rooms = [(node, room(node)) for node in candidates]
    usable = [(n, rm) for n, rm in rooms if all(rm[r] >= current[r] for r in rm if r not in wanted)] or rooms
    most = {r: max(rm[r] for _, rm in usable) or 1 for r in ("cpu", "memory")}

    def score(rm: dict) -> tuple:
        balance = min(rm[r] / most[r] for r in rm)
        if not wanted:
            return (balance,)
        return (min(min(rm[r], v) / v for r, v in wanted.items()), balance)

    best_node, best_room = max(usable, key=lambda pair: score(pair[1]))
    ds = daemonset_requests(best_node, pods)
    best: dict = {}
    for resource in ("cpu", "memory"):
        cap = best_room[resource]
        if resource == "memory":
            cap = quantities.floor_memory_to_mi(cap)
        best[resource] = {
            "cap": cap,
            "node": best_node["name"],
            "instance_type": best_node.get("instance_type"),
            "allocatable": best_node.get("allocatable", {}).get(resource),
            "daemonset_requests": quantities.format_units(resource, ds[resource]),
            "other_containers_requests": quantities.format_units(resource, others[resource]),
        }
    return {
        "headroom": round(1 - factor, 4),
        "candidate_nodes": len(candidates),
        "cpu": best["cpu"],
        "memory": best["memory"],
        "zero_node_groups_note": (
            "Node groups that currently have zero nodes are not visible here; a larger instance type "
            "in such a group would not be reflected in this cap."
        ),
    }


# ---------------------------------------------------------------- fit estimate


def first_fit(per_pod: dict, count: int, free: list[dict]) -> int:
    """How many of `count` pods, each requesting `per_pod`, fit into `free`."""
    remaining = [dict(f) for f in free]
    placed = 0
    for _ in range(count):
        for slot in remaining:
            if slot["cpu"] >= per_pod["cpu"] and slot["memory"] >= per_pod["memory"]:
                slot["cpu"] -= per_pod["cpu"]
                slot["memory"] -= per_pod["memory"]
                placed += 1
                break
        else:
            break
    return placed


def estimate_fit(nodes: list[dict], pods: list[dict], template: dict, per_pod: dict, new_pods: int) -> dict:
    """An *estimate* of whether `new_pods` more pods fit on current nodes."""
    candidates = [n for n in nodes if node_is_candidate(n, template)]
    free = [free_capacity(n, pods) for n in candidates]
    placed = first_fit(per_pod, new_pods, free)
    unplaced = new_pods - placed
    nodes_needed = 0
    if unplaced and candidates:
        per_node = [
            min(
                (node_allocatable(n)["cpu"] - daemonset_requests(n, pods)["cpu"]) // max(per_pod["cpu"], 1),
                (node_allocatable(n)["memory"] - daemonset_requests(n, pods)["memory"]) // max(per_pod["memory"], 1),
            )
            for n in candidates
        ]
        best = max(per_node) if per_node else 0
        nodes_needed = math.ceil(unplaced / best) if best > 0 else None
    return {
        "estimate": True,
        "new_pods": new_pods,
        "fit_now": placed,
        "unplaced": unplaced,
        "nodes_needed": nodes_needed,
        # One pod asks for more than an empty candidate node holds: more nodes of these shapes never help.
        "exceeds_every_node": bool(unplaced and candidates and nodes_needed is None),
        "candidate_nodes": len(candidates),
    }


# ---------------------------------------------------------------- Cluster Autoscaler


def parse_ca_status(text: str | None) -> dict:
    """`kube-system/cluster-autoscaler-status` -> node groups and scale-up state.

    Recent CA versions write YAML; older ones a free-text block. Anything
    that does not parse as the YAML shape is reported as unrecognised
    rather than half-read.
    """
    if not text:
        return {"available": False, "reason": "cluster-autoscaler-status not found"}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict) or "nodeGroups" not in data:
        return {"available": False, "reason": "cluster-autoscaler-status is not in a recognised format"}
    groups = []
    for group in data.get("nodeGroups") or []:
        health = group.get("health") or {}
        scale_up = group.get("scaleUp") or {}
        backoff = scale_up.get("backoffInfo") or {}
        error = backoff.get("errorInfo") or {}
        registered = ((health.get("nodeCounts") or {}).get("registered") or {}).get("total")
        groups.append(
            {
                "name": group.get("name"),
                "min_size": health.get("minSize"),
                "max_size": health.get("maxSize"),
                "target": health.get("cloudProviderTarget"),
                "registered": registered,
                "health": health.get("status"),
                "scale_up_status": scale_up.get("status"),
                "backoff_error_code": error.get("errorCode"),
                "backoff_error_message": error.get("errorMessage"),
            }
        )
    cluster_scale_up = ((data.get("clusterWide") or {}).get("scaleUp") or {}).get("status")
    return {"available": True, "cluster_scale_up_status": cluster_scale_up, "node_groups": groups}


def _group_at_max(group: dict) -> bool:
    target, max_size = group.get("target"), group.get("max_size")
    return target is not None and max_size is not None and target >= max_size


def _group_in_backoff(group: dict) -> bool:
    return bool(group.get("backoff_error_code") or group.get("backoff_error_message")) or (
        str(group.get("scale_up_status") or "").lower() == "backoff"
    )


def ca_risks(ca: dict, nodes_needed: int | None, exceeds_every_node: bool = False) -> list[dict]:
    """Warnings for a change that needs new nodes. Never blocking: quota is
    on the AWS side and only CA's recent behaviour hints at it."""
    oversized = (
        [
            {
                "level": "high",
                "message": (
                    "One new pod requests more CPU or memory than any eligible node can hold, so adding nodes of the "
                    "current instance types cannot run it; only a node group with a larger instance type could "
                    "(groups with zero nodes are not visible here)."
                ),
            }
        ]
        if exceeds_every_node
        else []
    )
    if not ca.get("available"):
        return oversized + [{"level": "info", "message": f"Cluster Autoscaler state unknown: {ca.get('reason')}"}]
    risks: list[dict] = oversized
    groups = ca.get("node_groups") or []
    for group in groups:
        if _group_in_backoff(group):
            detail = " ".join(x for x in (group.get("backoff_error_code"), group.get("backoff_error_message")) if x)
            # Medium, not high: which node group would serve these pods is not known here, and a
            # group for other workloads (a GPU pool, say) in backoff does not stop this change.
            risks.append(
                {
                    "level": "medium",
                    "message": (
                        f"Node group {group['name']} is in scale-up backoff ({detail or 'no detail'}); recent "
                        "node provisioning failed, most likely an EC2 quota or capacity problem. If the new pods "
                        "need a node from that group, they stay Pending."
                    ),
                }
            )
    headroom_total = sum(
        max(0, (g.get("max_size") or 0) - (g.get("target") or 0)) for g in groups if g.get("max_size") is not None
    )
    if groups and all(_group_at_max(g) for g in groups):
        risks.append({"level": "high", "message": "Every node group is already at its max size; no node can be added."})
    elif nodes_needed and headroom_total < nodes_needed:
        risks.append(
            {
                "level": "high",
                "message": f"About {nodes_needed} new node(s) are needed but the node groups can only grow by {headroom_total}.",
            }
        )
    if not risks and nodes_needed:
        risks.append(
            {
                "level": "info",
                "message": f"Cluster Autoscaler will need to add about {nodes_needed} node(s); taking effect will take a few minutes longer.",
            }
        )
    return risks


_PROVISIONING_FAILURE_EVENTS = {"FailedToScaleUpGroup", "FailedScaleUp", "ScaleUpFailed"}


def classify_pending(pending_pods: list[dict], events: list[dict], ca: dict) -> dict:
    """Why pods are Pending, from CA's events on them and its status ConfigMap.

    Precedence: a provisioning failure (CA tried and AWS said no) beats "CA
    won't scale" beats "scale-up in progress" beats a bare FailedScheduling.
    Only evidence tied to these pods counts: a node group in backoff, or a
    failed scale-up, is this workload's problem only when CA tried to add a
    node for these pods (TriggeredScaleUp) or declined to (NotTriggerScaleUp).
    Another workload's GPU group in quota backoff says nothing about a CPU
    Deployment that CA is busy scaling for.
    """
    names = {p.get("name") for p in pending_pods}
    own = [e for e in events if (e.get("involved") or {}).get("name") in names]
    reasons = {e.get("reason") for e in own}
    own_failure = any(e.get("reason") in _PROVISIONING_FAILURE_EVENTS for e in own)
    group_failure = any(
        e.get("reason") in _PROVISIONING_FAILURE_EVENTS and (e.get("involved") or {}).get("kind") != "Pod" for e in events
    )
    groups = ca.get("node_groups") or [] if ca.get("available") else []
    backoff = [g for g in groups if _group_in_backoff(g)]
    ca_acted = bool(reasons & {"TriggeredScaleUp", "NotTriggerScaleUp"})

    def _messages(wanted: set[str], pool: list[dict]) -> list[str]:
        return [e.get("message") for e in pool if e.get("reason") in wanted and e.get("message")][:5]

    if own_failure or (ca_acted and (backoff or group_failure)):
        return {
            "cause": "node_provisioning_failed",
            "stop_waiting": True,
            "detail": [
                f"{g['name']}: {g.get('backoff_error_code') or ''} {g.get('backoff_error_message') or ''}".strip()
                for g in backoff
            ]
            + _messages(_PROVISIONING_FAILURE_EVENTS, own if own_failure else events),
        }
    if "NotTriggerScaleUp" in reasons and "TriggeredScaleUp" not in reasons:
        return {"cause": "autoscaler_cannot_help", "stop_waiting": True, "detail": _messages({"NotTriggerScaleUp"}, own)}
    if "TriggeredScaleUp" in reasons:
        return {"cause": "scaling_up", "stop_waiting": False, "detail": _messages({"TriggeredScaleUp"}, own)}
    return {"cause": "failed_scheduling", "stop_waiting": False, "detail": _messages({"FailedScheduling"}, own)}


def node_group_summary(ca: dict) -> list[dict]:
    if not ca.get("available"):
        return []
    return [
        {"name": g["name"], "nodes": g.get("target"), "max_size": g.get("max_size")} for g in ca.get("node_groups") or []
    ]
