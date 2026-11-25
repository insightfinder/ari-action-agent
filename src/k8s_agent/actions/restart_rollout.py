"""`restart_rollout`: a rolling restart of every pod of a Deployment, StatefulSet or DaemonSet, the way
`kubectl rollout restart` does it. The pod template gets a `kubectl.kubernetes.io/restartedAt` annotation and
the workload's own controller replaces its pods under its own update strategy. No setting changes.

- Proposed only when the request itself asks for a restart (`policy.asks_restart`): an on-call prompt that
  quotes "restarted 5 times" while asking about memory is not one.
- The annotation's value is fixed when the proposal is made and is part of what is approved, so a retried
  apply finds it in place and verifies instead of restarting a second time.
- Refused where the controller would not act on it: a paused Deployment, an `OnDelete` update strategy, or a
  workload with no pods.
- There is nothing to keep in Git: the hint says so.
"""

from datetime import datetime, timezone

from ..stepkit import refusal
from ..tools import governance, policy
from ..tools.views import RESTARTED_AT
from .base import ActionSpec, Operation


def workload_pods(view: dict) -> int:
    if view.get("kind") == "DaemonSet":
        return (view.get("status") or {}).get("desired") or 0
    return view.get("replicas") or 0


class RestartRollout(ActionSpec):
    name = "restart_rollout"
    workload_kinds = governance.WORKLOAD_KINDS
    target_kinds = governance.WORKLOAD_KINDS
    prompt_line = (
        "restart every pod of the workload with a rolling restart (like kubectl rollout restart). Changes no "
        "setting. Only when the request asks for a restart of the workload; a request to restart one named pod is "
        "delete_pod."
    )
    not_asked = (
        "The request does not itself ask for a restart, so none was proposed. To have one proposed, ask for it "
        "directly, e.g. \"restart the {kind} {name} in namespace {namespace}\"."
    )
    # Deployments are patched with the rule every chart has; StatefulSets and DaemonSets need the newer one.
    rbac_rule = 'patch on "statefulsets" and "daemonsets" (apps group; the oncallari chart grants it from 0.3.15)'

    def accepts(self, prompt):
        return policy.asks_restart(prompt)

    async def load_state(self, ctx, target):
        view = await ctx.tool("get_workload")(target["kind"], target["namespace"], target["name"])
        return {"workload": view, **({"deployment": view} if target["kind"] == "Deployment" else {})}

    def target(self, action):
        return {"kind": action.get("workload_kind") or "Deployment", "namespace": action.get("namespace"), "name": action.get("workload")}

    # ------------------------------------------------------------ plan

    def plan(self, ctx, state, change, env):
        w = state["workload"]
        kind, ref = w["kind"], f"{w['kind']} {w['namespace']}/{w['name']}"
        strategy = (w.get("strategy") or {}).get("type")
        if kind == "Deployment" and w.get("paused"):
            return None, refusal(
                f"{ref} is paused (spec.paused), so its controller would not roll out a restart. Resume it first."
            )
        if strategy == "OnDelete":
            return None, refusal(
                f"{ref} uses the OnDelete update strategy: changing its template restarts nothing until each pod "
                "is deleted by hand. Ask for a specific pod to be deleted instead."
            )
        pods = workload_pods(w)
        if pods < 1:
            return None, refusal(f"{ref} has no pods to restart.")

        restarted_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        risks: list[dict] = []
        notes: list[str] = []
        if kind == "Deployment":
            if strategy == "Recreate":
                risks.append(
                    {
                        "level": "high",
                        "message": "Strategy Recreate: every pod is stopped before the new ones start, so the "
                        "Deployment serves nothing until the new pods are ready.",
                    }
                )
            else:
                s = w.get("strategy") or {}
                notes.append(
                    f"Rolling update: up to {s.get('max_surge')} extra and {s.get('max_unavailable')} unavailable pods "
                    "at a time, as the Deployment's own strategy sets."
                )
        elif kind == "StatefulSet":
            partition = (w.get("strategy") or {}).get("partition") or 0
            notes.append(
                "A StatefulSet replaces its pods one at a time, highest ordinal first, each after the previous one "
                "is ready; each keeps its name and its PersistentVolumeClaims."
            )
            if partition:
                notes.append(f"Update partition {partition}: only pods with an ordinal of {partition} or more restart.")
            if pods == 1:
                risks.append(
                    {
                        "level": "high",
                        "message": "The StatefulSet has one pod: it is stopped before its replacement starts, so the "
                        "StatefulSet serves nothing until the new pod is ready.",
                    }
                )
        else:
            notes.append(
                "A DaemonSet replaces its pods node by node; while a node's pod is replaced that node runs without "
                "it (maxUnavailable {}).".format((w.get("strategy") or {}).get("max_unavailable"))
            )
        action = {
            "action": "restart_rollout",
            "namespace": w["namespace"],
            "deployment": w["name"] if kind == "Deployment" else None,
            "workload_kind": kind,
            "workload": w["name"],
            "restarted_at": restarted_at,
        }
        facts = {
            "before": {"restarted_at": (w.get("template") or {}).get("restarted_at")},
            "after": {"restarted_at": restarted_at},
            "scale_down": False,
            "high_replicas": False,
            "new_pods": 0,
            "rolls_pods": True,
            "roll_message": f"A restart replaces every pod of this {kind} ({pods}).",
            "risks": risks,
            "notes": notes,
            "changed_object": {"kind": kind, "name": w["name"], "labels": w.get("labels") or {}, "annotations": w.get("annotations") or {}},
            "rendered_changes": [],
            "nothing_to_persist": "Nothing to persist: a restart changes no setting.",
            "action_name": f"Restart {ref} ({pods} pod{'s' if pods != 1 else ''}, rolling)",
        }
        return (action, facts), None

    # ------------------------------------------------------------ write

    def operations(self, action):
        return [
            Operation(
                verb="patch",
                kind=action.get("workload_kind") or "Deployment",
                resource=action.get("workload_kind") or "Deployment",
                name=action["workload"],
                namespace=action.get("namespace"),
                body={"spec": {"template": {"metadata": {"annotations": {RESTARTED_AT: action["restarted_at"]}}}}},
                patch_type="merge",
            )
        ]

    def summarize(self, op, raw, action):
        template_meta = (((raw.get("spec") or {}).get("template") or {}).get("metadata") or {})
        return {**super().summarize(op, raw, action), "restarted_at": (template_meta.get("annotations") or {}).get(RESTARTED_AT)}

    def patch_action(self, proposal):
        return {
            **{k: proposal.get(k) for k in ("action", "namespace", "deployment", "workload", "restarted_at")},
            "workload_kind": proposal.get("workload_kind") or "Deployment",
        }

    # ------------------------------------------------------------ approval-time checks

    def snapshot(self, state):
        w = state["workload"]
        return {
            "uid": w.get("uid"),
            "restarted_at": (w.get("template") or {}).get("restarted_at"),
            "strategy": (w.get("strategy") or {}).get("type"),
        }

    def drift(self, proposal, state):
        w, snapshot = state["workload"], proposal["snapshot"]
        changes = []
        if snapshot.get("uid") and w.get("uid") != snapshot.get("uid"):
            changes.append(f"{w['kind']} {w['name']} was deleted and created again")
        now = (w.get("template") or {}).get("restarted_at")
        if now != snapshot.get("restarted_at"):
            changes.append(f"it was restarted since the proposal (restartedAt is now {now})")
        if w.get("paused"):
            changes.append("it is paused now")
        if (w.get("strategy") or {}).get("type") != snapshot.get("strategy"):
            changes.append(f"its update strategy is now {(w.get('strategy') or {}).get('type')}")
        return changes

    def at_target(self, action, state):
        return (state["workload"].get("template") or {}).get("restarted_at") == action["restarted_at"]

    def verify_target(self, action):
        return {"kind": "restart", "restarted_at": action["restarted_at"]}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        kind = action.get("workload_kind") or "Deployment"
        if kind == "Deployment":
            return await ctx.tool("wait_rollout")(action["namespace"], action["workload"], self.verify_target(action), applied_at, max_wait)
        return await ctx.tool("wait_workload_rollout")(kind, action["namespace"], action["workload"], applied_at, max_wait)

    def base_tier(self, facts):
        return "medium"

    def changed_object(self, action, state):
        w = state["workload"]
        return {"labels": w.get("labels") or {}, "annotations": w.get("annotations") or {}}

    def lifetime_notice(self, ownership, chain):
        return (
            "A restart changes no setting, so there is nothing to keep in Git. The pod template now carries the "
            f"{RESTARTED_AT} annotation, as after kubectl rollout restart."
        )

    def lifetime_risk(self, ownership):
        return "info"
