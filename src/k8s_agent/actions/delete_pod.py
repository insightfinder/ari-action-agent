"""`delete_pod`: delete ONE pod of a Deployment, so its controller replaces it from the unchanged template (a
stuck or misbehaving pod). The only delete this agent makes.

- Proposed only when the request itself asks for a pod to be deleted (`policy.asks_delete_pod`) and names the
  pod exactly; there is no "the only pod" shortcut, as there is for an in-place resize.
- Only a Deployment's pod (through its ReplicaSet). A StatefulSet's or DaemonSet's pod, a bare pod, a Job's pod
  or a static (mirror) pod is refused: nothing that could break a product environment (2026-11-26).
- Refused when no other pod of the Deployment is ready, at proposal and again at approval: the Deployment would
  serve nothing until the replacement is ready.
- The delete carries a uid precondition, so it removes exactly the approved pod, not a later one.
- A pod already gone or terminating when the approval arrives was deleted by someone else (or evicted): refused,
  never reported as this agent's delete (apply_approved_change, first attempt). A failure before the DELETE is
  sent ends the step (not retried, `retry_before_write`): a gone pod records nobody, so only a lost worker, which
  may have sent it, reaches the retry that counts a gone pod as its own.
- A plain delete, not an eviction: it does not wait for a PodDisruptionBudget, which the proposal says when
  one covers the pod.
- Verified by positive evidence (`rollout.wait_pod_replaced`): the old uid gone and a new pod ready and stable.
"""

import re

from ..stepkit import refusal
from ..tools import governance, policy
from .base import ActionSpec, Operation


class DeletePod(ActionSpec):
    name = "delete_pod"
    workload_kinds = ("Deployment",)
    target_kinds = ("Pod",)
    # A gone pod records nobody: a retry could not tell this agent's delete from someone else's.
    retry_before_write = False
    prompt_line = (
        "delete ONE pod so its controller replaces it — also what restarting one named pod means. Only when the "
        "request asks for a pod to be deleted or restarted; report the pod name exactly as written in the request."
    )
    not_asked = (
        "The request does not itself ask for a pod to be deleted, so nothing was proposed. To have one proposed, "
        "ask for it directly with the pod's name, e.g. \"delete pod <name> in namespace {namespace}\"."
    )
    rbac_rule = 'delete on "pods" (core API group; the oncallari chart grants it from 0.3.15)'

    def accepts(self, prompt):
        return policy.asks_delete_pod(prompt)

    # ------------------------------------------------------------ target and state

    async def resolve_target(self, ctx, workload, change):
        kind, name, namespace = workload["kind"], workload["name"], workload["namespace"]
        pods = await ctx.tool("list_pods")(namespace, workload.get("label_selector") or workload.get("selector") or None, True)
        own = sorted(p["name"] for p in pods if governance.owned_by(p, kind, name))
        named = str(change.get("pod") or "").strip()
        # Bounded, so a prompt naming db-10 does not back a model answer of db-1.
        written = bool(named) and re.search(rf"(?<![\w-]){re.escape(named)}(?![\w-])", ctx.prompt or "") is not None
        if not named or named not in own or not written:
            return None, refusal(
                f"Which pod of {kind} {namespace}/{name} to delete is not clear: name it exactly as it appears in the "
                "cluster. Its pods are: " + (", ".join(own) or "none"),
                pods=own,
            )
        return {"kind": "Pod", "namespace": namespace, "name": named}, None

    async def load_state(self, ctx, target):
        pod = await ctx.tool("get_pod")(target["namespace"], target["name"])
        owner = governance.workload_of(pod) if pod else None
        workload = await ctx.tool("get_workload")(owner[0], target["namespace"], owner[1]) if owner else None
        siblings = []
        if workload:
            pods = await ctx.tool("list_pods")(target["namespace"], workload.get("label_selector") or workload.get("selector") or None, True)
            siblings = [p for p in pods if governance.owned_by(p, owner[0], owner[1])]
        return {
            "pod": pod,
            "workload": workload,
            "siblings": siblings,
            **({"deployment": workload} if owner and owner[0] == "Deployment" else {}),
        }

    def target(self, action):
        return {"kind": "Pod", "namespace": action.get("namespace"), "name": action.get("pod")}

    # ------------------------------------------------------------ plan

    def plan(self, ctx, state, change, env):
        pod, w = state["pod"], state.get("workload")
        if pod is None:
            return None, refusal("The pod no longer exists; nothing to delete.")
        ref = f"pod {pod['namespace']}/{pod['name']}"
        if pod.get("deleting"):
            return None, refusal(f"{ref} is already being deleted.")
        if w is None or w.get("kind") != "Deployment":
            return None, refusal(
                f"{ref} is not a Deployment's pod (owner: {pod.get('owner_kind') or 'none'}); this agent deletes only "
                "pods a Deployment recreates."
            )
        kind, wref = w["kind"], f"{w['kind']} {w['namespace']}/{w['name']}"
        if not _ready_others(pod["name"], state.get("siblings") or []):
            return None, refusal(
                f"No other pod of {wref} is ready, so deleting {ref} would leave it serving nothing until the "
                "replacement is ready. Nothing was proposed."
            )
        risks: list[dict] = []
        notes = [f"{wref} recreates the pod from its unchanged template."]
        action = {
            "action": "delete_pod",
            "namespace": pod["namespace"],
            "deployment": w["name"],
            "workload_kind": kind,
            "workload": w["name"],
            "pod": pod["name"],
            "pod_uid": pod.get("uid"),
        }
        facts = {
            "before": {"pod": pod["name"], "uid": pod.get("uid"), "phase": pod.get("phase"), "node": pod.get("node_name")},
            "after": {"pod": f"deleted; {wref} creates a replacement"},
            "scale_down": False,
            "high_replicas": False,
            "new_pods": 0,
            "pdb_message": "A delete does not wait for this budget (an eviction would); the pod goes at once.",
            "pod_labels": pod.get("labels") or {},
            "risks": risks,
            "notes": notes,
            "changed_object": {"kind": kind, "name": w["name"], "labels": w.get("labels") or {}, "annotations": w.get("annotations") or {}},
            "rendered_changes": [],
            "nothing_to_persist": "Nothing to persist: deleting a pod changes no setting.",
            "action_name": f"Delete {ref} ({wref} replaces it)",
        }
        return (action, facts), None

    # ------------------------------------------------------------ write

    def operations(self, action):
        return [
            Operation(
                verb="delete",
                kind="Pod",
                resource="Pod",
                name=action["pod"],
                namespace=action.get("namespace"),
                preconditions={"uid": action["pod_uid"]},
            )
        ]

    def summarize(self, op, raw, action):
        return {"resource": op.resource, "name": action.get("pod"), "uid": action.get("pod_uid"), "deletion_requested": True}

    def patch_action(self, proposal):
        return {k: proposal.get(k) for k in ("action", "namespace", "deployment", "workload_kind", "workload", "pod", "pod_uid")}

    # ------------------------------------------------------------ approval-time checks

    def snapshot(self, state):
        pod = state.get("pod") or {}
        return {"pod_uid": pod.get("uid"), "node": pod.get("node_name")}

    def drift(self, proposal, state):
        pod = state.get("pod")
        name = proposal.get("pod")
        if pod is None:
            return [f"pod {name} no longer exists"]
        if pod.get("uid") != proposal["snapshot"].get("pod_uid"):
            return [f"pod {name} was already replaced (new uid)"]
        if pod.get("deleting"):
            return [f"pod {name} is already being deleted"]
        # A proposal made before only Deployment pods were deleted (2026-11-26) may name another kind's pod.
        owner = (state.get("workload") or {}).get("kind")
        if owner != "Deployment" or (proposal.get("workload_kind") or "Deployment") != "Deployment":
            return [f"pod {name} is a {owner or 'bare'} pod; this agent deletes only a Deployment's pods"]
        if not _ready_others(name, state.get("siblings") or []):
            return [f"no other pod of {proposal.get('workload')} is ready now"]
        return []

    def at_target(self, action, state):
        pod = state.get("pod")
        return pod is None or pod.get("uid") != action.get("pod_uid") or bool(pod.get("deleting"))

    def verify_target(self, action):
        return {"kind": "pod_replaced", "pod": action["pod"], "uid": action["pod_uid"]}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        return await ctx.tool("wait_pod_replaced")(
            action["namespace"], action["pod"], action["pod_uid"], action["workload_kind"], action["workload"], applied_at, max_wait
        )

    def base_tier(self, facts):
        return "medium"

    def changed_object(self, action, state):
        w = state.get("workload") or {}
        return {"labels": w.get("labels") or {}, "annotations": w.get("annotations") or {}}

    def lifetime_notice(self, ownership, chain):
        return "Nothing to keep in Git: deleting a pod changes no setting, and its controller recreates it from the unchanged template."

    def lifetime_risk(self, ownership):
        return "info"


def _ready_others(name: str, siblings: list[dict]) -> list[dict]:
    return [p for p in siblings if p["name"] != name and not p.get("deleting") and _ready(p)]


def _ready(pod: dict) -> bool:
    containers = pod.get("containers") or []
    return pod.get("phase") == "Running" and bool(containers) and all(c.get("ready") for c in containers)
