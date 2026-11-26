"""`restart_rollout`: a rolling restart of every pod of a Deployment, the way `kubectl rollout restart` does it.
The pod template gets a `kubectl.kubernetes.io/restartedAt` annotation and the Deployment's controller replaces
its pods under its own update strategy. No setting changes. StatefulSets and DaemonSets are not restarted
(decided 2026-11-26: stateful systems and node-level agents can break in ways a restart does not undo).

- Proposed only when the request itself asks for a restart (`policy.asks_restart`): an on-call prompt that
  quotes "restarted 5 times" while asking about memory is not one.
- The annotation's value is fixed when the proposal is made and is part of what is approved, so a retried
  apply finds it in place and verifies instead of restarting a second time.
- Refused where the controller would not act on it: a paused Deployment, or one with no pods.
- Refused at approval when a rollout has happened since the proposal (the Deployment's revision moved): its pods
  are already new.
- There is nothing to keep in Git: the hint says so.
"""

from datetime import datetime, timezone

from ..stepkit import refusal
from ..tools import policy
from ..tools.views import RESTARTED_AT
from .base import ActionSpec, Operation


def rollout_blocked(w: dict, what: str) -> str | None:
    """Why the Deployment's controller would not roll out a template change (`what`, e.g. "a restart"), or None."""
    if w.get("paused"):
        return f"Deployment {w['namespace']}/{w['name']} is paused (spec.paused), so its controller would not roll out {what}."
    return None


def rollout_effects(w: dict) -> tuple[list[dict], list[str]]:
    """`(risks, notes)` of rolling every pod of the Deployment under its own update strategy."""
    s = w.get("strategy") or {}
    if s.get("type") == "Recreate":
        return [
            {
                "level": "high",
                "message": "Strategy Recreate: every pod is stopped before the new ones start, so the "
                "Deployment serves nothing until the new pods are ready.",
            }
        ], []
    return [], [
        (
            f"Rolling update: up to {s.get('max_surge')} extra and {s.get('max_unavailable')} unavailable pods "
            "at a time, as the Deployment's own strategy sets."
        )
    ]


class RestartRollout(ActionSpec):
    name = "restart_rollout"
    workload_kinds = ("Deployment",)
    target_kinds = ("Deployment",)
    prompt_line = (
        "restart every pod of the workload with a rolling restart (like kubectl rollout restart). Changes no "
        "setting. Only when the request asks for a restart of the workload; a request to restart one named pod is "
        "delete_pod."
    )
    not_asked = (
        "The request does not itself ask for a restart, so none was proposed. To have one proposed, ask for it "
        "directly, e.g. \"restart the {kind} {name} in namespace {namespace}\"."
    )

    def accepts(self, prompt):
        return policy.asks_restart(prompt)

    async def load_state(self, ctx, target):
        view = await ctx.tool("get_workload")(target["kind"], target["namespace"], target["name"])
        return {"workload": view, "deployment": view}

    def target(self, action):
        return {"kind": action.get("workload_kind") or "Deployment", "namespace": action.get("namespace"), "name": action.get("workload")}

    # ------------------------------------------------------------ plan

    def plan(self, ctx, state, change, env):
        w = state["workload"]
        kind, ref = w["kind"], f"{w['kind']} {w['namespace']}/{w['name']}"
        blocked = rollout_blocked(w, "a restart")
        if blocked:
            return None, refusal(blocked)
        pods = w.get("replicas") or 0
        if pods < 1:
            return None, refusal(f"{ref} has no pods to restart.")

        restarted_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        risks, notes = rollout_effects(w)
        action = {
            "action": "restart_rollout",
            "namespace": w["namespace"],
            "deployment": w["name"],
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
            "revision": w.get("revision"),
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
        # A rollout since the proposal (image automation, a Helm upgrade, someone's change) has already
        # replaced the pods the restart was asked for. Proposals made before the revision was recorded carry none.
        if snapshot.get("revision") and w.get("revision") != snapshot["revision"]:
            changes.append(
                f"a rollout since the proposal already replaced its pods (revision {snapshot['revision']} -> "
                f"{w.get('revision')}); ask again if a restart is still needed"
            )
        return changes

    def at_target(self, action, state):
        return (state["workload"].get("template") or {}).get("restarted_at") == action["restarted_at"]

    def verify_target(self, action):
        return {"kind": "restart", "restarted_at": action["restarted_at"]}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        return await ctx.tool("wait_rollout")(action["namespace"], action["workload"], self.verify_target(action), applied_at, max_wait)

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
