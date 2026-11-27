"""`set_autoscaler_bounds`: a KEDA ScaledObject's or HPA's min/max — only on a request that names the
autoscaler and the values; never below 1; KEDA's generated HPAs are never patched."""

from ..stepkit import refusal
from ..tools import governance, policy
from .base import DeploymentAction, Operation, artifact_int, extracted_int as _int

_FIELDS = {
    "ScaledObject": {"min": "minReplicaCount", "max": "maxReplicaCount"},
    "HorizontalPodAutoscaler": {"min": "minReplicas", "max": "maxReplicas"},
}


class SetAutoscalerBounds(DeploymentAction):
    name = "set_autoscaler_bounds"
    prompt_line = "change the KEDA ScaledObject's or HPA's min and/or max replica count."

    def plan(self, ctx, state, change, env):
        return _plan_autoscaler(ctx, state["deployment"], state.get("autoscaler"), change, env["threshold"])

    def operations(self, action):
        target = action["autoscaler"]
        fields = _FIELDS.get(target.get("kind"))
        if fields is None:
            raise ValueError(f"unsupported autoscaler kind {target.get('kind')!r}")
        spec = {fields[k]: int(action[k]) for k in ("min", "max") if action.get(k) is not None}
        if not spec:
            raise ValueError("set_autoscaler_bounds needs at least one of min/max")
        return [
            Operation(
                verb="patch",
                kind=target["kind"],
                resource=target["kind"],
                name=target["name"],
                namespace=action.get("namespace"),
                body={"spec": spec},
                patch_type="merge",
            )
        ]

    def summarize(self, op, raw, action):
        spec = raw.get("spec") or {}
        fields = _FIELDS[op.kind]
        return {**super().summarize(op, raw, action), "min": spec.get(fields["min"]), "max": spec.get(fields["max"])}

    def patch_action(self, proposal):
        action = {k: proposal.get(k) for k in ("action", "namespace", "deployment")}
        action["autoscaler"] = proposal["autoscaler"]
        for bound in ("min", "max"):
            if proposal.get(bound) is not None:
                action[bound] = artifact_int(proposal[bound])
        return action

    def at_target(self, action, state):
        fingerprint = governance.autoscaler_fingerprint(state.get("autoscaler")) or {}
        if fingerprint.get("name") != (action.get("autoscaler") or {}).get("name"):
            return False
        return all(artifact_int(fingerprint.get(b)) == action[b] for b in ("min", "max") if action.get(b) is not None)

    def written_fields(self, action, proposal):
        target = action["autoscaler"]
        fields = _FIELDS.get(target.get("kind"))
        if fields is None:
            return None
        then = (proposal.get("snapshot") or {}).get("autoscaler") or {}
        paths = [
            ["f:spec", f"f:{fields[bound]}"]
            for bound in ("min", "max")
            if action.get(bound) is not None and artifact_int(then.get(bound)) != action[bound]
        ]
        return {"kind": target["kind"], "namespace": action.get("namespace"), "name": target["name"], "paths": paths}

    def verify_target(self, action):
        return {"kind": "autoscaler", "min": action.get("min"), "max": action.get("max")}

    def base_tier(self, facts):
        return "medium"

    def changed_object(self, action, state):
        autoscaler = state.get("autoscaler") or {}
        return {"labels": autoscaler.get("labels") or {}, "annotations": autoscaler.get("annotations") or {}}


def _plan_autoscaler(ctx, dep, autoscaler, change, threshold):
    if not autoscaler:
        return None, refusal(
            f"{dep['namespace']}/{dep['name']} is not managed by KEDA or an HPA, so there is no autoscaler "
            "min/max to change; ask for a replica count instead."
        )
    if not autoscaler.get("name"):
        return None, refusal(
            f"{dep['namespace']}/{dep['name']} is scaled by KEDA (seen through its generated HPA "
            f"{autoscaler.get('inferred_from_hpa')}), but its ScaledObject could not be read, so it cannot be changed safely."
        )
    if not policy.mentions_autoscaler(ctx.prompt, [dep["name"], dep["namespace"], autoscaler.get("name")]):
        return None, refusal(
            f"{dep['namespace']}/{dep['name']} is managed by {autoscaler['kind']} {autoscaler['name']}; its "
            "min/max are changed only on an explicit request that names the autoscaler (KEDA/ScaledObject/HPA) and the values."
        )
    new = {}
    for bound in ("min", "max"):
        value = _int(change.get(bound))
        if value is None:
            continue
        if not policy.quote_in_prompt(change.get(f"{bound}_quote"), ctx.prompt, value):
            return None, refusal(f"The request does not state the autoscaler {bound} explicitly enough to act on.")
        new[bound] = value
    if not new:
        return None, refusal("The request names the autoscaler but no min or max value to set.")
    for bound, value in new.items():
        if value < policy.REPLICA_FLOOR:
            return None, refusal(f"An autoscaler {bound} below {policy.REPLICA_FLOOR} is never set by this agent (requested {value}).")
    final_min = new.get("min", autoscaler.get("min"))
    final_max = new.get("max", autoscaler.get("max"))
    if final_min is not None and final_max is not None and final_max < final_min:
        return None, refusal(f"That would leave max ({final_max}) below min ({final_min}).")
    if all(autoscaler.get(b) == v for b, v in new.items()):
        return None, refusal(f"{autoscaler['kind']} {autoscaler['name']} already has those values; nothing to change.")
    fields = {"ScaledObject": ("spec.minReplicaCount", "spec.maxReplicaCount"), "HorizontalPodAutoscaler": ("spec.minReplicas", "spec.maxReplicas")}[autoscaler["kind"]]
    rendered = [
        {
            "object": f"{autoscaler['kind']} {dep['namespace']}/{autoscaler['name']}",
            "field": fields[0] if bound == "min" else fields[1],
            "from": autoscaler.get(bound),
            "to": value,
        }
        for bound, value in new.items()
    ]
    action = {
        "action": "set_autoscaler_bounds",
        "namespace": dep["namespace"],
        "deployment": dep["name"],
        "autoscaler": {"kind": autoscaler["kind"], "name": autoscaler["name"]},
        **new,
    }
    label = "KEDA ScaledObject" if autoscaler["kind"] == "ScaledObject" else "HPA"
    parts = ", ".join(f"{b} {autoscaler.get(b)} -> {v}" for b, v in new.items())
    current_replicas = dep["replicas"] or 0
    facts = {
        "before": {b: autoscaler.get(b) for b in new},
        "after": dict(new),
        "scale_down": final_max is not None and final_max < current_replicas,
        "high_replicas": any(v > threshold for v in new.values()),
        "new_pods": max(0, (new.get("min") or 0) - current_replicas),
        "changed_object": {"kind": autoscaler["kind"], "name": autoscaler["name"], "labels": autoscaler.get("labels") or {}, "annotations": autoscaler.get("annotations") or {}},
        "rendered_changes": rendered,
        "action_name": f"Set {label} {autoscaler['name']} replica bounds ({parts})",
        "notes": (
            [f"The ScaledObject's current minReplicaCount is {autoscaler.get('min')}; it is left unchanged."]
            if autoscaler["kind"] == "ScaledObject" and autoscaler.get("min") == 0 and "min" not in new
            else []
        ),
    }
    return (action, facts), None
