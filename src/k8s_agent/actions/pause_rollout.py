"""`pause_rollout` / `resume_rollout`: set a Deployment's `spec.paused`, the way `kubectl rollout pause` and
`kubectl rollout resume` do.

- Proposed only when the request itself asks for it (`policy.asks_pause_rollout`, `asks_resume_rollout`);
  pausing a Deployment that is paused, or resuming one that is not, is refused.
- A pause replaces no pod. While paused, a change to the pod template (image automation, a Helm upgrade,
  kubectl set image) is recorded but not rolled out, and a rollout in progress stops where it is. Scaling
  still works. Nothing reminds anyone to resume, so the proposal lists that as a risk.
- A resume rolls out whatever template change waited while paused. Whether one waits is read from the
  Deployment's status: while paused, the controller counts only pods already on the current template as
  updated, so fewer updated than desired means pods will be replaced (checked on test, 2026-11-25).
- There is nothing to keep in Git: a pause is meant to be temporary.
- Verified from the controller's own report (`rollout.wait_paused`: the Progressing condition says, or stops
  saying, DeploymentPaused, and the value holds); a resume that rolls pods by `wait_rollout`.
"""

from ..stepkit import refusal
from ..tools import policy
from .base import DeploymentAction, Operation


def pending_rollout(dep: dict) -> int:
    """How many pods are not on the current template yet (all of them when the controller has not looked at
    the latest change): what a resume will replace."""
    status = dep.get("status") or {}
    if (dep.get("observed_generation") or 0) < (dep.get("generation") or 0):
        return dep.get("replicas") or 0
    return max((dep.get("replicas") or 0) - (status.get("updated") or 0), 0)


class _SetPaused(DeploymentAction):
    paused: bool = True

    def plan(self, ctx, state, change, env):
        dep = state["deployment"]
        ref = f"Deployment {dep['namespace']}/{dep['name']}"
        if bool(dep.get("paused")) == self.paused:
            return None, refusal(f"{ref} is {'already' if self.paused else 'not'} paused; nothing to change.")
        waiting = pending_rollout(dep)
        risks: list[dict] = []
        notes: list[str] = []
        facts = {
            "before": {"paused": bool(dep.get("paused"))},
            "after": {"paused": self.paused},
            "scale_down": False,
            "high_replicas": False,
            "new_pods": 0,
            "risks": risks,
            "notes": notes,
            "changed_object": {"kind": "Deployment", "name": dep["name"], "labels": dep["labels"], "annotations": dep["annotations"]},
            "rendered_changes": [],
        }
        if self.paused:
            risks.append(
                {
                    "level": "medium",
                    "message": (
                        "While paused, no change to the pod template rolls out: image automation, Helm upgrades and "
                        "kubectl set image are recorded but replace no pod until the Deployment is resumed. Nothing "
                        "resumes it automatically."
                    ),
                }
            )
            if waiting:
                risks.append(
                    {
                        "level": "medium",
                        "message": f"A rollout is in progress ({waiting} of {dep['replicas']} pods not on the current "
                        "template yet); pausing stops it where it is, with old and new pods side by side.",
                    }
                )
            notes.append("A pause replaces no pod, and scaling (by hand or by an autoscaler) still works while paused.")
            facts["restart"] = False
            facts["nothing_to_persist"] = "Nothing to persist: a pause is meant to be temporary. Resume it when done."
            facts["action_name"] = f"Pause rollouts of {ref}"
        else:
            if waiting:
                facts["rolls_pods"] = True
                facts["roll_message"] = (
                    f"The pod template changed while the Deployment was paused: resuming replaces {waiting} of "
                    f"{dep['replicas']} pods with the current template."
                )
                facts["action_name"] = f"Resume rollouts of {ref} (rolls {waiting} pod{'s' if waiting != 1 else ''})"
            else:
                notes.append("Every pod already runs the current template, so resuming replaces no pod.")
                facts["restart"] = False
                facts["action_name"] = f"Resume rollouts of {ref}"
            facts["nothing_to_persist"] = "Nothing to persist: resuming changes no setting."
        action = {"action": self.name, "namespace": dep["namespace"], "deployment": dep["name"], "paused": self.paused}
        action["pending_pods"] = waiting
        return (action, facts), None

    def operations(self, action):
        return [
            Operation(
                verb="patch",
                kind="Deployment",
                resource="Deployment",
                name=action["deployment"],
                namespace=action.get("namespace"),
                body={"spec": {"paused": bool(action["paused"])}},
                patch_type="merge",
            )
        ]

    def summarize(self, op, raw, action):
        return {**super().summarize(op, raw, action), "paused": bool((raw.get("spec") or {}).get("paused"))}

    def patch_action(self, proposal):
        return {
            **{k: proposal.get(k) for k in ("action", "namespace", "deployment")},
            "paused": bool(proposal.get("paused")),
            "pending_pods": int(proposal.get("pending_pods") or 0),
        }

    def snapshot(self, state):
        dep = state["deployment"]
        return {"deployment_uid": dep.get("uid"), "paused": bool(dep.get("paused")), "pending_pods": pending_rollout(dep)}

    def drift(self, proposal, state):
        dep, snapshot = state["deployment"], proposal["snapshot"]
        changes = []
        if snapshot.get("deployment_uid") and dep.get("uid") != snapshot.get("deployment_uid"):
            changes.append(f"Deployment {dep['name']} was deleted and created again")
        if bool(dep.get("paused")) != bool(snapshot.get("paused")):
            changes.append(f"it is {'paused' if dep.get('paused') else 'not paused'} now")
        # A template change made while paused, after a resume was approved as "replaces no pod" (image automation
        # does this several times an hour on test), would make that resume roll every pod.
        elif not self.paused and dep.get("paused") and not int(snapshot.get("pending_pods") or 0) and pending_rollout(dep):
            changes.append(f"its pod template changed since the proposal: resuming would now replace {pending_rollout(dep)} pod(s)")
        return changes

    def at_target(self, action, state):
        return bool(state["deployment"].get("paused")) == bool(action["paused"])

    def verify_target(self, action):
        return {"kind": "paused", "paused": bool(action["paused"])}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        # A resume rolls whatever waits when it lands: read from the state the write started from, not only
        # from the proposal (a retried attempt has no `before`; the approved count stands then).
        waiting = pending_rollout(before["deployment"]) if before else int(action.get("pending_pods") or 0)
        if not action["paused"] and (waiting or action.get("pending_pods")):
            return await ctx.tool("wait_rollout")(action["namespace"], action["deployment"], self.verify_target(action), applied_at, max_wait)
        return await ctx.tool("wait_paused")(action["namespace"], action["deployment"], bool(action["paused"]), max_wait)

    def base_tier(self, facts):
        return "medium"

    def lifetime_notice(self, ownership, chain):
        return (
            "spec.paused is not usually set by a chart or manifest, so a GitOps reconcile or Helm upgrade normally "
            "leaves it as it is; if the manifest does set it, the next reconcile puts that value back."
        )

    def lifetime_risk(self, ownership):
        return "info"


class PauseRollout(_SetPaused):
    name = "pause_rollout"
    paused = True
    prompt_line = (
        "pause the Deployment's rollouts (like kubectl rollout pause): no pod is replaced, and template changes "
        "wait until it is resumed. Only when the request asks to pause it."
    )
    not_asked = (
        "The request does not itself ask to pause rollouts, so nothing was proposed. To have it proposed, ask for "
        "it directly, e.g. \"pause the rollout of deployment {name} in namespace {namespace}\"."
    )

    def accepts(self, prompt):
        return policy.asks_pause_rollout(prompt)


class ResumeRollout(_SetPaused):
    name = "resume_rollout"
    paused = False
    prompt_line = (
        "resume a paused Deployment's rollouts (like kubectl rollout resume); a template change made while paused "
        "then rolls out. Only when the request asks to resume it."
    )
    not_asked = (
        "The request does not itself ask to resume rollouts, so nothing was proposed. To have it proposed, ask for "
        "it directly, e.g. \"resume the rollout of deployment {name} in namespace {namespace}\"."
    )

    def accepts(self, prompt):
        return policy.asks_resume_rollout(prompt)
