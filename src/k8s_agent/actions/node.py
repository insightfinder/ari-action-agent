"""Node actions: `cordon_node` and `uncordon_node` set a node's `spec.unschedulable` (as `kubectl cordon` /
`uncordon` do); `drain_node` cordons a node and evicts its pods (as `kubectl drain` does, with its default rules).

Each is proposed only when the request itself asks for it (`policy.asks_cordon`, `asks_uncordon`,
`asks_drain`), and only for a node `find_workload` resolved from a name the request writes out.

- Nothing here touches a node's labels or taints or deletes a node (the "Never" list in
  docs/K8S_AGENT_INTERFACE.md). RBAC's `patch` on nodes cannot be narrowed to `spec.unschedulable`, so that limit
  is this code's.
- A node is not kept in Git: there is nothing to persist, and the setting lasts until someone changes it back or
  the node is replaced (Cluster Autoscaler or the node group may remove a node at any time).
- A drain evicts through the Eviction API, so PodDisruptionBudgets are honoured: an eviction a budget refuses
  (429) is retried until the drain's time is up, never forced. A DaemonSet's pod and a mirror pod stay, a
  finished pod is left, a pod no controller owns blocks the proposal, and an emptyDir's data is listed as lost.
  What is approved is the node, the workloads seen on it and these rules; the pods are listed again when the
  drain runs (image automation replaces pods every few minutes on test, so names from the proposal would be
  gone by approval), and the drain refuses if a workload the approver did not see is on the node by then.
  The agent's own worker pod, when it runs there, is evicted last; a retried step on its replacement carries on.
"""

import asyncio
import os
import time

from ..stepkit import refusal
from ..tools import capacity, governance, policy
from ..tools.client import api_status, safe_error
from .base import ActionSpec, Operation

_RBAC = 'patch on "nodes" (core group; the oncallari chart grants it from 0.3.17)'
EVICTION_SECONDS = 300
_EVICTION_RETRY_SECONDS = 10


def _own_pod() -> str | None:
    # A pod's hostname is its name.
    return os.environ.get("HOSTNAME") or None


class _NodeAction(ActionSpec):
    workload_kinds = ("Node",)
    target_kinds = ("Node",)
    rbac_rule = _RBAC
    needs_capacity = True

    async def resolve_target(self, ctx, workload, change):
        return {"kind": "Node", "namespace": None, "name": workload["name"]}, None

    async def load_state(self, ctx, target):
        return {"node": await ctx.tool("get_node")(target["name"])}

    def target(self, action):
        return {"kind": "Node", "namespace": None, "name": action.get("node")}

    def changed_object(self, action, state):
        # A node has no GitOps owner; its labels are the cloud's, not a Flux or Helm ownership marker.
        return {"labels": {}, "annotations": {}}

    def lifetime_notice(self, ownership, chain):
        return (
            "A node is not kept in Git: the change lasts until someone changes it back or the node is replaced "
            "(Cluster Autoscaler or the node group may remove a node at any time)."
        )

    def lifetime_risk(self, ownership):
        return "info"

    def _facts(self, node: dict, before: dict, after: dict) -> dict:
        return {
            "before": before,
            "after": after,
            "scale_down": False,
            "high_replicas": False,
            "new_pods": 0,
            "restart": False,
            "risks": [],
            "notes": [],
            "changed_object": {"kind": "Node", "name": node["name"], "labels": {}, "annotations": {}},
            "rendered_changes": [],
            "nothing_to_persist": "Nothing to persist: a node is not kept in Git.",
        }

    def _gone(self, proposal: dict, state: dict) -> list[str]:
        node = state.get("node")
        if node is None:
            return [f"node {proposal.get('node')} no longer exists"]
        if proposal["snapshot"].get("node_uid") and node.get("uid") != proposal["snapshot"].get("node_uid"):
            return [f"node {node['name']} was replaced by a new node of the same name"]
        return []


def _schedulable_elsewhere(nodes: list[dict] | None, name: str) -> list[dict]:
    return [n for n in nodes or [] if n.get("name") != name and n.get("ready") and not n.get("unschedulable")]


class _SetUnschedulable(_NodeAction):
    unschedulable: bool = True

    def plan(self, ctx, state, change, env):
        node = state["node"]
        if bool(node.get("unschedulable")) == self.unschedulable:
            return None, refusal(f"Node {node['name']} is {'already' if self.unschedulable else 'not'} cordoned; nothing to change.")
        facts = self._facts(node, {"unschedulable": bool(node.get("unschedulable"))}, {"unschedulable": self.unschedulable})
        others = _schedulable_elsewhere(env.get("nodes"), node["name"])
        if self.unschedulable:
            facts["notes"].append(
                "Pods already on the node keep running: a cordon evicts nothing. New pods, replacements included, "
                "are scheduled on other nodes. Uncordon it (uncordon_node) when done."
            )
            if not others:
                facts["risks"].append(
                    {"level": "high", "message": "No other ready node takes new pods: nothing new can be scheduled anywhere until this is undone or a node is added."}
                )
            else:
                facts["notes"].append(f"{len(others)} other ready node(s) take new pods.")
            facts["action_name"] = f"Cordon node {node['name']}"
        else:
            away = governance.going_away_taints(node)
            if away:
                facts["risks"].append(
                    {
                        "level": "high",
                        "message": f"The node carries {', '.join(away)}: something else (Cluster Autoscaler, a spot "
                        "termination handler, Karpenter) cordoned it to remove or replace it. Uncordoning lets new pods "
                        "land on a node that is going away.",
                    }
                )
            if not node.get("ready"):
                facts["risks"].append({"level": "medium", "message": "The node is not Ready: pods scheduled on it may not start."})
            facts["action_name"] = f"Uncordon node {node['name']}"
        action = {
            "action": self.name,
            "namespace": None,
            "deployment": None,
            "node": node["name"],
            "node_uid": node.get("uid"),
            "unschedulable": self.unschedulable,
        }
        return (action, facts), None

    def operations(self, action):
        return [
            Operation(
                verb="patch",
                kind="Node",
                resource="Node",
                name=action["node"],
                namespace=None,
                body={"spec": {"unschedulable": bool(action["unschedulable"])}},
                patch_type="merge",
            )
        ]

    def summarize(self, op, raw, action):
        return {"resource": "Node", "name": (raw.get("metadata") or {}).get("name"), "unschedulable": bool((raw.get("spec") or {}).get("unschedulable"))}

    def patch_action(self, proposal):
        return {**{k: proposal.get(k) for k in ("action", "node", "node_uid")}, "unschedulable": bool(proposal.get("unschedulable"))}

    def snapshot(self, state):
        node = state["node"]
        return {"node_uid": node.get("uid"), "unschedulable": bool(node.get("unschedulable"))}

    def drift(self, proposal, state):
        changes = self._gone(proposal, state)
        if not changes and bool(state["node"].get("unschedulable")) != bool(proposal["snapshot"].get("unschedulable")):
            changes.append(f"it is {'cordoned' if state['node'].get('unschedulable') else 'not cordoned'} now")
        return changes

    def at_target(self, action, state):
        node = state.get("node")
        return bool(node) and node.get("uid") == action.get("node_uid") and bool(node.get("unschedulable")) == bool(action["unschedulable"])

    def verify_target(self, action):
        return {"kind": "unschedulable", "unschedulable": bool(action["unschedulable"])}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        return await ctx.tool("wait_node_schedulable")(action["node"], action.get("node_uid"), bool(action["unschedulable"]), max_wait)

    def base_tier(self, facts):
        return "medium"


class CordonNode(_SetUnschedulable):
    name = "cordon_node"
    unschedulable = True
    prompt_line = "cordon the node (like kubectl cordon): no new pod is scheduled on it; nothing is evicted."
    not_asked = (
        "The request does not itself ask to cordon the node, so nothing was proposed. To have it proposed, ask for "
        "it directly, e.g. \"cordon node {name}\"."
    )

    def accepts(self, prompt):
        return policy.asks_cordon(prompt)


class UncordonNode(_SetUnschedulable):
    name = "uncordon_node"
    unschedulable = False
    prompt_line = "uncordon the node (like kubectl uncordon): new pods may be scheduled on it again."
    not_asked = (
        "The request does not itself ask to uncordon the node, so nothing was proposed. To have it proposed, ask "
        "for it directly, e.g. \"uncordon node {name}\"."
    )

    def accepts(self, prompt):
        return policy.asks_uncordon(prompt)


# ---------------------------------------------------------------- drain


def _ready(pod: dict) -> bool:
    containers = pod.get("containers") or []
    return pod.get("phase") == "Running" and not pod.get("deleting") and bool(containers) and all(c.get("ready") for c in containers)


def _matches(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


class DrainNode(_NodeAction):
    name = "drain_node"
    prompt_line = (
        "drain the node (like kubectl drain): cordon it, then evict its pods so their controllers start them "
        "elsewhere. Only when the request asks to drain or evacuate the node."
    )
    not_asked = (
        "The request does not itself ask to drain the node, so nothing was proposed. To have it proposed, ask for "
        "it directly, e.g. \"drain node {name}\"."
    )
    rbac_rule = 'patch on "nodes" and create on "pods/eviction" (core group; the oncallari chart grants them from 0.3.17)'
    executes_itself = True

    def accepts(self, prompt):
        return policy.asks_drain(prompt)

    async def load_state(self, ctx, target):
        node = await ctx.tool("get_node")(target["name"])
        pods = await ctx.tool("list_pods_on_node")(target["name"]) if node else []
        namespaces = sorted({p.get("namespace") for p in pods if p.get("namespace")})
        pdbs = {ns: await ctx.tool("list_pdbs")(ns) for ns in namespaces}
        return {"node": node, "pods": pods, "pdbs": pdbs}

    # ------------------------------------------------------------ plan

    def plan(self, ctx, state, change, env):
        node, pods = state["node"], state.get("pods") or []
        name = node["name"]
        drain = governance.drain_plan(pods)
        if drain["bare"]:
            names = sorted(f"{p['namespace']}/{p['name']}" for p in drain["bare"])
            return None, refusal(
                f"Node {name} runs pod(s) no controller owns, which an eviction would delete for good: "
                + ", ".join(names)
                + ". Nothing was proposed; move or delete them first (kubectl drain refuses them too without --force).",
                bare_pods=names,
            )
        evict = drain["evict"]
        workloads = sorted({governance.workload_key(p) for p in evict})
        facts = self._facts(
            node,
            {"unschedulable": bool(node.get("unschedulable")), "pods_to_evict": sorted(f"{p['namespace']}/{p['name']}" for p in evict)[:60]},
            {"unschedulable": True, "pods_on_node": "only DaemonSet and mirror pods"},
        )
        risks, notes = facts["risks"], facts["notes"]
        skipped = drain["skipped"]
        notes.append(
            f"Evicts {len(evict)} pod(s) of {len(workloads)} workload(s). Stays: {skipped['daemonset']} DaemonSet pod(s), "
            f"{skipped['mirror']} mirror pod(s); left alone: {skipped['finished']} finished, {skipped['deleting']} already deleting."
        )
        notes.append(
            "Evictions honour PodDisruptionBudgets: one a budget holds back is retried for up to "
            f"{EVICTION_SECONDS // 60} minutes, never forced. The node stays cordoned afterwards; uncordon it "
            "(uncordon_node) when done. The pods are listed again when the drain runs, and it refuses if a workload "
            "not listed here is on the node by then."
        )
        if drain["empty_dir"]:
            risks.append(
                {
                    "level": "medium",
                    "message": "Data in emptyDir volumes is lost with these pods: "
                    + ", ".join(sorted(f"{p['namespace']}/{p['name']} ({', '.join(p['empty_dir'])})" for p in drain["empty_dir"])[:20]),
                }
            )
        # Disruption budgets, per evicted pod.
        held, refused_pdb = set(), set()
        for pod in evict:
            covering = [b for b in (state.get("pdbs") or {}).get(pod.get("namespace")) or [] if _matches(b.get("selector") or {}, pod.get("labels") or {})]
            if len(covering) > 1:
                refused_pdb.add(f"{pod['namespace']}/{pod['name']} ({', '.join(b['name'] for b in covering)})")
            elif covering and covering[0].get("disruptions_allowed") == 0:
                held.add(f"{pod['namespace']}/{covering[0]['name']}")
        if held:
            risks.append(
                {
                    "level": "high",
                    "message": "These PodDisruptionBudgets allow no disruption now, so their pods are evicted only once "
                    "the budget allows it, or the drain ends incomplete: " + ", ".join(sorted(held)),
                }
            )
        if refused_pdb:
            risks.append(
                {
                    "level": "high",
                    "message": "More than one PodDisruptionBudget covers these pods, and the Eviction API refuses such a "
                    "pod outright (seen on test), so the drain will end incomplete: " + ", ".join(sorted(refused_pdb)),
                }
            )
        # Workloads with no ready pod elsewhere serve nothing until their replacement is ready.
        everywhere = env.get("pods") or []
        alone = []
        for key in workloads:
            if key.startswith(("Job/",)):
                continue
            elsewhere = [p for p in everywhere if governance.workload_key(p) == key and p.get("node_name") != name and _ready(p)]
            if not elsewhere:
                alone.append(key)
        if alone:
            risks.append(
                {
                    "level": "high",
                    "message": "No ready pod of these runs on another node, so each serves nothing until its replacement "
                    "is ready elsewhere: " + ", ".join(alone[:20]) + (f" and {len(alone) - 20} more" if len(alone) > 20 else ""),
                }
            )
        # Room elsewhere (an estimate: requests against free allocatable, first fit).
        others = _schedulable_elsewhere(env.get("nodes"), name)
        free = {n["name"]: capacity.free_capacity(n, everywhere) for n in others}
        unplaced = []
        for pod in sorted(evict, key=lambda p: -capacity.pod_requests(p)["memory"]):
            need = capacity.pod_requests(pod)
            slot = next(
                (n for n in others if capacity.node_is_candidate(n, pod) and free[n["name"]]["cpu"] >= need["cpu"] and free[n["name"]]["memory"] >= need["memory"]),
                None,
            )
            if slot is None:
                unplaced.append(f"{pod['namespace']}/{pod['name']}")
                continue
            for r in ("cpu", "memory"):
                free[slot["name"]][r] -= need[r]
        if unplaced:
            risks.append(
                {
                    "level": "medium",
                    "message": f"Estimate: {len(unplaced)} of {len(evict)} evicted pod(s) do not fit on the other nodes "
                    "now; they stay Pending until Cluster Autoscaler adds a node (if it can): " + ", ".join(unplaced[:15]),
                }
            )
        own = _own_pod()
        if own and any(p["name"] == own for p in evict):
            risks.append(
                {
                    "level": "medium",
                    "message": "This agent's own worker pod runs on the node. It is evicted last; the step then carries on "
                    "on its replacement, which can take a few minutes.",
                }
            )
        facts["action_name"] = f"Drain node {name} ({len(evict)} pod(s) of {len(workloads)} workload(s))"
        action = {
            "action": "drain_node",
            "namespace": None,
            "deployment": None,
            "node": name,
            "node_uid": node.get("uid"),
            "workloads": workloads,
            "empty_dir_workloads": sorted({governance.workload_key(p) for p in drain["empty_dir"]}),
            "eviction_seconds": EVICTION_SECONDS,
        }
        return (action, facts), None

    # ------------------------------------------------------------ write

    def _check(self, action: dict, pods: list[dict]) -> tuple[list[dict], list[str]]:
        """`(pods to evict, why it must not run)` for the pods on the node now."""
        drain = governance.drain_plan(pods)
        problems = []
        if drain["bare"]:
            problems.append("pod(s) no controller owns: " + ", ".join(sorted(f"{p['namespace']}/{p['name']}" for p in drain["bare"])))
        approved = set(action.get("workloads") or [])
        new = sorted({governance.workload_key(p) for p in drain["evict"]} - approved)
        if new:
            problems.append("workload(s) not in the approved drain: " + ", ".join(new))
        empty_ok = set(action.get("empty_dir_workloads") or [])
        lost = sorted({governance.workload_key(p) for p in drain["empty_dir"]} - empty_ok)
        if lost:
            problems.append("pods with emptyDir data the approval did not cover: " + ", ".join(lost))
        return drain["evict"], problems

    async def execute(self, action, kube, dry_run, run_operation):
        from ..tools import views

        node_name = action["node"]
        raw = await kube.get("Node", node_name)
        if action.get("node_uid") and (raw.get("metadata") or {}).get("uid") != action["node_uid"]:
            raise RuntimeError(f"node {node_name} was replaced since the proposal; nothing was changed")
        pods = [views.pod_view(p) for p in await kube.list("Pod", None, None, f"spec.nodeName={node_name}")]
        evict, problems = self._check(action, pods)
        if problems:
            raise RuntimeError(f"drain of {node_name} not started, the node changed since the proposal: " + "; ".join(problems))

        summary = {"resource": "Node", "name": node_name, "dry_run": dry_run, "evicted": [], "gone": [], "blocked": [], "failed": []}
        # Nothing below may raise: once the cordon is in, an error becomes part of the summary, never a failed
        # step that hides a half-drained node.
        if not (raw.get("spec") or {}).get("unschedulable"):
            cordon = Operation(
                verb="patch", kind="Node", resource="Node", name=node_name, namespace=None,
                body={"spec": {"unschedulable": True}}, patch_type="merge",
            )
            await run_operation(kube, cordon, dry_run)  # before any eviction; an error here leaves the node as it was
        summary["cordoned"] = True

        own = _own_pod()
        pending = sorted(evict, key=lambda p: (p["name"] == own, p.get("namespace") or "", p["name"]))
        seconds = action.get("eviction_seconds")
        deadline = time.monotonic() + float(EVICTION_SECONDS if seconds is None else seconds)
        while pending:
            held = []
            for pod in pending:
                ref = f"{pod['namespace']}/{pod['name']}"
                options = {"preconditions": {"uid": pod.get("uid")}}
                if dry_run:
                    options["dryRun"] = ["All"]
                op = Operation(
                    verb="create", kind="Pod", resource="Pod.Eviction", name=pod["name"], namespace=pod["namespace"],
                    subresource="eviction",
                    body={"apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": pod["name"], "namespace": pod["namespace"]}, "deleteOptions": options},
                )
                try:
                    await run_operation(kube, op, dry_run)
                    summary["evicted"].append(ref)
                except Exception as exc:  # noqa: BLE001 — sorted into the summary, never raised
                    status = api_status(exc)
                    if status in (404, 409):  # gone, or already replaced under the same name
                        summary["gone"].append(ref)
                    elif status == 429:  # a disruption budget holds it back for now
                        held.append(pod)
                    else:
                        summary["failed"].append({"pod": ref, "error": safe_error(exc)[:300]})
            pending = held
            if not pending or dry_run or time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(_EVICTION_RETRY_SECONDS, max(1.0, deadline - time.monotonic())))
        summary["blocked"] = sorted(f"{p['namespace']}/{p['name']}" for p in pending)
        if dry_run:
            denied = [f for f in summary["failed"] if "HTTP 403" in f["error"]]
            if denied:
                # The proposal turns this into a refusal naming the missing rule.
                raise RuntimeError(f"dry-run eviction of {denied[0]['pod']} failed: {denied[0]['error']}")
        return summary

    def operations(self, action):
        raise ValueError("a drain runs itself (execute)")

    def patch_action(self, proposal):
        return {
            **{k: proposal.get(k) for k in ("action", "node", "node_uid")},
            "workloads": list(proposal.get("workloads") or []),
            "empty_dir_workloads": list(proposal.get("empty_dir_workloads") or []),
            "eviction_seconds": int(EVICTION_SECONDS if proposal.get("eviction_seconds") is None else proposal["eviction_seconds"]),
        }

    # ------------------------------------------------------------ approval-time checks

    def snapshot(self, state):
        return {"node_uid": (state["node"] or {}).get("uid")}

    def drift(self, proposal, state):
        changes = self._gone(proposal, state)
        if changes:
            return changes
        _, problems = self._check(proposal, state.get("pods") or [])
        return problems

    def at_target(self, action, state):
        node = state.get("node")
        if not node or node.get("uid") != action.get("node_uid") or not node.get("unschedulable"):
            return False
        return not governance.drain_plan(state.get("pods") or [])["evict"]

    def verify_target(self, action):
        return {"kind": "drained"}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        return await ctx.tool("wait_drain")(action["node"], action.get("node_uid"), max_wait)

    def base_tier(self, facts):
        return "high"
