"""`scale_replicas`: set a Deployment's replica count (never below 1; refused when an autoscaler owns it)."""

from ..stepkit import refusal
from ..tools import policy
from .base import DeploymentAction, Operation, artifact_int, extracted_int as _int


class ScaleReplicas(DeploymentAction):
    name = "scale_replicas"
    prompt_line = "set the Deployment's replica count."

    def plan(self, ctx, state, change, env):
        return _plan_scale(ctx, state["deployment"], state.get("autoscaler"), change, env["threshold"])

    def operations(self, action):
        return [
            Operation(
                verb="patch",
                kind="Deployment",
                subresource="scale",
                resource="Deployment.Scale",
                name=action["deployment"],
                namespace=action.get("namespace"),
                body={"spec": {"replicas": int(action["replicas"])}},
                patch_type="merge",
            )
        ]

    def summarize(self, op, raw, action):
        return {**super().summarize(op, raw, action), "replicas": (raw.get("spec") or {}).get("replicas")}

    def patch_action(self, proposal):
        return {
            **{k: proposal.get(k) for k in ("action", "namespace", "deployment")},
            "replicas": artifact_int(proposal["replicas"]),
        }

    def drift(self, proposal, state):
        changes = super().drift(proposal, state)
        # Only the fields the approved change depends on. The Deployment's `generation` is deliberately not
        # compared: image automation upgrades Helm releases several times an hour (observed on test,
        # 2026-10-13), each bumping it, so an approval hours later would almost always be refused for a
        # change that has nothing to do with it.
        then = artifact_int(proposal["snapshot"].get("deployment_replicas"))
        if then != state["deployment"].get("replicas"):
            changes.append(f"replicas changed from {then} to {state['deployment'].get('replicas')}")
        # A scale-up's capacity estimate was made from each pod's requests then; changed requests (someone's
        # CPU/memory change while the approval waited) make it say nothing about the new pods.
        proposed = proposal["snapshot"].get("container_resources")
        if then is not None and artifact_int(proposal.get("replicas") or 0) > then and proposed is not None:
            now = {c["name"]: c["resources"] for c in state["deployment"]["template"]["containers"]}
            if now != proposed:
                changes.append("its containers' CPU/memory changed since the proposal, so its estimate of where the new pods fit no longer holds")
        return changes

    def at_target(self, action, state):
        return state["deployment"].get("replicas") == action["replicas"]

    def written_fields(self, action, proposal):
        # Written through the scale subresource; the API server records it on the Deployment's own field.
        return {"kind": "Deployment", "namespace": action.get("namespace"), "name": action["deployment"], "paths": [["f:spec", "f:replicas"]]}

    def verify_target(self, action):
        return {"kind": "replicas", "replicas": action["replicas"]}

    def base_tier(self, facts):
        return "medium" if facts.get("scale_down") else "low"


def _plan_scale(ctx, dep, autoscaler, change, threshold):
    if autoscaler:
        return None, refusal(
            f"{dep['namespace']}/{dep['name']} is managed by {autoscaler.get('kind')} "
            f"{autoscaler.get('name') or ''}".strip()
            + f" (min {autoscaler.get('min')}, max {autoscaler.get('max')}). A direct replica change would be "
            "overwritten by the autoscaler within seconds, so it is not proposed. To change how far it scales, "
            "ask explicitly for the autoscaler's min and/or max, e.g. \"set the KEDA max replicas of "
            f"{dep['name']} to 10\".",
            autoscaler={"kind": autoscaler.get("kind"), "name": autoscaler.get("name"), "min": autoscaler.get("min"), "max": autoscaler.get("max")},
        )
    replicas = _int(change.get("replicas"))
    if replicas is None or not policy.quote_in_prompt(change.get("replicas_quote"), ctx.prompt, replicas):
        return None, refusal("The request does not state a replica count explicitly enough to act on.")
    if replicas < policy.REPLICA_FLOOR:
        return None, refusal(f"Scaling below {policy.REPLICA_FLOOR} replica is never done by this agent (requested {replicas}).")
    if replicas == dep["replicas"]:
        return None, refusal(f"{dep['namespace']}/{dep['name']} already runs {replicas} replica(s); nothing to change.")
    action = {"action": "scale_replicas", "namespace": dep["namespace"], "deployment": dep["name"], "replicas": replicas}
    facts = {
        "before": {"replicas": dep["replicas"]},
        "after": {"replicas": replicas},
        "scale_down": replicas < dep["replicas"],
        "high_replicas": replicas > threshold,
        "new_pods": max(0, replicas - dep["replicas"]),
        "changed_object": {"kind": "Deployment", "name": dep["name"], "labels": dep["labels"], "annotations": dep["annotations"]},
        "rendered_changes": [{"object": f"Deployment {dep['namespace']}/{dep['name']}", "field": "spec.replicas", "from": dep["replicas"], "to": replicas}],
        "action_name": f"Scale {dep['namespace']}/{dep['name']} from {dep['replicas']} to {replicas} replicas",
    }
    return (action, facts), None
