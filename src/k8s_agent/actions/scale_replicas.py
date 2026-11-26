"""`scale_replicas`: set the replica count of a Deployment or a StatefulSet (never below 1).

- Refused when an autoscaler (HPA or KEDA) scales the workload, or when another object controls a StatefulSet
  (an operator's custom resource): either would put its own count back within seconds.
- A Deployment is scaled through its `scale` subresource. A StatefulSet's `spec.replicas` is patched on the
  StatefulSet itself, with the rule the oncallari chart already grants for a restart (0.3.15), so it needs no
  new RBAC.
- A StatefulSet adds and removes pods by ordinal: scaling up creates `<name>-<n>` and, from its
  `volumeClaimTemplates`, one PVC per template for each new pod (or mounts the one left from an earlier
  scale-down, with its old data); scaling down removes the highest ordinals first. Its scale-down is `high`
  risk (a clustered application may need the members removed from its own configuration first), and refused
  under `persistentVolumeClaimRetentionPolicy.whenScaled: Delete`, which would delete the removed pods' PVCs:
  this agent never deletes a PVC.
- Verified from the workload's own counts: a Deployment's rollout (`wait_rollout`), a StatefulSet's
  `spec.replicas`, `status.replicas` and ready pods all at the target (`rollout.replicas_reached`).

Proposals made before StatefulSets carry only `deployment`; they still read as a Deployment scale.
"""

from ..stepkit import refusal
from ..tools import governance, policy
from .base import DeploymentAction, Operation, artifact_int, extracted_int as _int

_KINDS = ("Deployment", "StatefulSet")
# PVC and pod names listed in one risk or note, before "and N more".
_LISTED = 6


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:_LISTED])
    return shown + (f" and {len(names) - _LISTED} more" if len(names) > _LISTED else "")


def _kind(action: dict) -> str:
    return action.get("workload_kind") or "Deployment"


def _name(action: dict) -> str | None:
    return action.get("workload") or action.get("deployment")


class ScaleReplicas(DeploymentAction):
    name = "scale_replicas"
    workload_kinds = _KINDS
    target_kinds = _KINDS
    prompt_line = "set the replica count of the Deployment or StatefulSet."
    # Deployments are scaled with the rule every chart has; a StatefulSet is patched with the newer one.
    rbac_rule = 'patch on "statefulsets" (apps group; the oncallari chart grants it from 0.3.15)'
    rbac_rule_kinds = ("StatefulSet",)

    async def load_state(self, ctx, target):
        if target["kind"] != "StatefulSet":
            return await super().load_state(ctx, target)
        view = await ctx.tool("get_workload")("StatefulSet", target["namespace"], target["name"])
        autoscaler = await ctx.tool("find_autoscaler")(target["namespace"], target["name"], "StatefulSet")
        return {"workload": view, "autoscaler": autoscaler}

    async def plan_inputs(self, ctx, state):
        w = state["workload"]
        if w.get("kind") != "StatefulSet" or not w.get("volume_claim_templates"):
            return {}
        # Whether a new pod's PVC is already there (left by an earlier scale-down) decides what the proposal says.
        try:
            return {"pvcs": {p["name"]: p for p in await ctx.tool("list_views")("PersistentVolumeClaim", w["namespace"])}}
        except Exception as exc:  # noqa: BLE001 — said in the proposal, not a reason to refuse
            return {"pvcs": None, "pvcs_unreadable": f"{type(exc).__name__}: {exc}"}

    def target(self, action):
        return {"kind": _kind(action), "namespace": action.get("namespace"), "name": _name(action)}

    # ------------------------------------------------------------ plan

    def plan(self, ctx, state, change, env):
        w = state["workload"]
        kind = w.get("kind") or "Deployment"
        ref = f"{w['namespace']}/{w['name']}"
        autoscaler = state.get("autoscaler")
        if autoscaler:
            return None, _autoscaled(kind, w, autoscaler)
        if kind == "StatefulSet" and w.get("controller"):
            owner = w["controller"]
            return None, refusal(
                f"StatefulSet {ref} is managed by {owner.get('kind')} {owner.get('name')}, which sets its replica "
                f"count: a direct change would be put back by that controller, so it is not proposed. Change the "
                f"replica count on the {owner.get('kind')} instead.",
                controller=owner,
            )
        replicas = _int(change.get("replicas"))
        if replicas is None or not policy.quote_in_prompt(change.get("replicas_quote"), ctx.prompt, replicas):
            return None, refusal("The request does not state a replica count explicitly enough to act on.")
        if replicas < policy.REPLICA_FLOOR:
            return None, refusal(f"Scaling below {policy.REPLICA_FLOOR} replica is never done by this agent (requested {replicas}).")
        current = w["replicas"]
        if replicas == current:
            return None, refusal(f"{ref} already runs {replicas} replica(s); nothing to change.")
        if kind == "StatefulSet" and replicas < current and (w.get("pvc_retention") or {}).get("when_scaled") == "Delete":
            return None, refusal(
                f"StatefulSet {ref} has persistentVolumeClaimRetentionPolicy.whenScaled: Delete, so scaling it down "
                "would delete the PersistentVolumeClaims of the removed pods, and their data. This agent never "
                "deletes a PVC, so it is not proposed. Scale it with kubectl, or set whenScaled to Retain first."
            )
        action = {
            "action": "scale_replicas",
            "namespace": w["namespace"],
            "deployment": w["name"] if kind == "Deployment" else None,
            "workload_kind": kind,
            "workload": w["name"],
            "replicas": replicas,
        }
        facts = {
            "kind": kind,
            "before": {"replicas": current},
            "after": {"replicas": replicas},
            "scale_down": replicas < current,
            "high_replicas": replicas > env["threshold"],
            "new_pods": max(0, replicas - current),
            "changed_object": {"kind": kind, "name": w["name"], "labels": w.get("labels") or {}, "annotations": w.get("annotations") or {}},
            "rendered_changes": [{"object": f"{kind} {ref}", "field": "spec.replicas", "from": current, "to": replicas}],
            "action_name": f"Scale {'StatefulSet ' if kind == 'StatefulSet' else ''}{ref} from {current} to {replicas} replicas",
        }
        if kind == "StatefulSet":
            facts["risks"], facts["notes"] = _statefulset_effects(w, current, replicas, env)
        return (action, facts), None

    # ------------------------------------------------------------ write

    def operations(self, action):
        body = {"spec": {"replicas": int(action["replicas"])}}
        if _kind(action) == "StatefulSet":
            return [
                Operation(
                    verb="patch",
                    kind="StatefulSet",
                    resource="StatefulSet",
                    name=_name(action),
                    namespace=action.get("namespace"),
                    body=body,
                    patch_type="merge",
                )
            ]
        return [
            Operation(
                verb="patch",
                kind="Deployment",
                subresource="scale",
                resource="Deployment.Scale",
                name=_name(action),
                namespace=action.get("namespace"),
                body=body,
                patch_type="merge",
            )
        ]

    def summarize(self, op, raw, action):
        return {**super().summarize(op, raw, action), "replicas": (raw.get("spec") or {}).get("replicas")}

    def patch_action(self, proposal):
        return {
            **{k: proposal.get(k) for k in ("action", "namespace", "deployment")},
            "workload_kind": _kind(proposal),
            "workload": _name(proposal),
            "replicas": artifact_int(proposal["replicas"]),
        }

    # ------------------------------------------------------------ approval-time checks

    def snapshot(self, state):
        w = state["workload"]
        if w.get("kind") != "StatefulSet":
            return super().snapshot(state)
        return {
            "uid": w.get("uid"),
            "replicas": w.get("replicas"),
            "controller": w.get("controller"),
            "pvc_retention": w.get("pvc_retention"),
            "autoscaler": governance.autoscaler_fingerprint(state.get("autoscaler")),
        }

    def drift(self, proposal, state):
        # The autoscaler governing the workload: its appearance, disappearance or change invalidates the approval.
        changes = super().drift(proposal, state)
        w, snapshot = state["workload"], proposal["snapshot"]
        # Only the fields the approved change depends on. The Deployment's `generation` is deliberately not
        # compared: image automation upgrades Helm releases several times an hour (observed on test,
        # 2026-10-13), each bumping it, so an approval hours later would almost always be refused for a
        # change that has nothing to do with it.
        then = artifact_int(snapshot["deployment_replicas"] if "deployment_replicas" in snapshot else snapshot.get("replicas"))
        if then != w.get("replicas"):
            changes.append(f"replicas changed from {then} to {w.get('replicas')}")
        if w.get("kind") == "StatefulSet":
            if snapshot.get("uid") and w.get("uid") != snapshot.get("uid"):
                changes.append(f"StatefulSet {w['name']} was deleted and created again")
            if w.get("controller") != snapshot.get("controller"):
                changes.append(f"its controlling object is now {w.get('controller')}")
            if w.get("pvc_retention") != snapshot.get("pvc_retention"):
                changes.append(f"its PVC retention policy is now {w.get('pvc_retention')}")
        return changes

    def at_target(self, action, state):
        return state["workload"].get("replicas") == action["replicas"]

    def verify_target(self, action):
        return {"kind": "replicas", "replicas": action["replicas"]}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        if _kind(action) != "StatefulSet":
            return await super().verify(ctx, action, proposal, before, applied_at, max_wait)
        return await ctx.tool("wait_workload_rollout")(
            "StatefulSet", action["namespace"], _name(action), applied_at, max_wait, target=self.verify_target(action)
        )

    def base_tier(self, facts):
        if facts.get("kind") == "StatefulSet":
            return "high" if facts.get("scale_down") else "medium"
        return "medium" if facts.get("scale_down") else "low"

    def changed_object(self, action, state):
        w = state["workload"]
        return {"labels": w.get("labels") or {}, "annotations": w.get("annotations") or {}}


def _autoscaled(kind: str, w: dict, autoscaler: dict) -> dict:
    how = (
        "To change how far it scales, ask explicitly for the autoscaler's min and/or max, e.g. \"set the KEDA max "
        f"replicas of {w['name']} to 10\"."
        if kind == "Deployment"
        # set_autoscaler_bounds is offered for Deployments only.
        else "To change how far it scales, change the autoscaler's min and/or max where it is defined (usually Git)."
    )
    return refusal(
        f"{w['namespace']}/{w['name']} is managed by {autoscaler.get('kind')} "
        f"{autoscaler.get('name') or ''}".strip()
        + f" (min {autoscaler.get('min')}, max {autoscaler.get('max')}). A direct replica change would be "
        "overwritten by the autoscaler within seconds, so it is not proposed. " + how,
        autoscaler={"kind": autoscaler.get("kind"), "name": autoscaler.get("name"), "min": autoscaler.get("min"), "max": autoscaler.get("max")},
    )


def _statefulset_effects(w: dict, current: int, replicas: int, env: dict) -> tuple[list[dict], list[str]]:
    """`(risks, notes)` of scaling a StatefulSet from `current` to `replicas`: which pods and PVCs come and go."""
    risks: list[dict] = []
    notes: list[str] = []
    start = w.get("ordinals_start") or 0
    name = w["name"]
    templates = [t["name"] for t in w.get("volume_claim_templates") or [] if t.get("name")]
    status = w.get("status") or {}
    if replicas < current:
        ordinals = list(range(start + current - 1, start + replicas - 1, -1))
        pods = [f"{name}-{i}" for i in ordinals]
        risks.append(
            {
                "level": "high",
                "message": f"Removes pod(s) {_listed(pods)}, highest ordinal first. A clustered application (a database, "
                "a message broker, a quorum service) may need these members drained or removed from its own "
                "configuration first; that cannot be checked from here.",
            }
        )
        if templates:
            claims = [f"{t}-{name}-{i}" for i in ordinals for t in templates]
            notes.append(
                f"Their PersistentVolumeClaims ({_listed(claims)}) are kept with their data (whenScaled: Retain): the "
                "same pods mount them again if the StatefulSet is scaled back up, and they keep costing storage "
                "until someone deletes them."
            )
    else:
        ordinals = list(range(start + current, start + replicas))
        pods = [f"{name}-{i}" for i in ordinals]
        notes.append(
            f"Adds pod(s) {_listed(pods)}. A clustered application may also need its own configuration changed "
            "before it uses new members."
        )
        if templates:
            claims = [f"{t}-{name}-{i}" for i in ordinals for t in templates]
            pvcs = env.get("pvcs")
            if pvcs is None:
                notes.append(
                    f"Whether PersistentVolumeClaims {_listed(claims)} already exist could not be read "
                    f"({env.get('pvcs_unreadable')}); an existing one is mounted with its old data."
                )
            else:
                existing = [c for c in claims if c in pvcs]
                new = [c for c in claims if c not in pvcs]
                if existing:
                    risks.append(
                        {
                            "level": "medium",
                            "message": f"PersistentVolumeClaim(s) {_listed(existing)} already exist (left by an earlier "
                            "scale-down, the oldest created "
                            f"{min(str(pvcs[c].get('created_at') or '') for c in existing) or 'at an unknown time'}): the new "
                            "pods mount them with the data they hold, and a pod whose volume is bound to one zone can "
                            "only run in that zone.",
                        }
                    )
                if new:
                    sizes = ", ".join(
                        f"{t['name']} {t.get('storage') or '?'} (storage class {t.get('storage_class') or 'default'})"
                        for t in w.get("volume_claim_templates") or []
                    )
                    notes.append(
                        f"Creates PersistentVolumeClaim(s) {_listed(new)} from the volume claim templates ({sizes}). "
                        "They are kept after a later scale-down and cost storage until someone deletes them."
                    )
    if w.get("pod_management_policy") == "Parallel":
        notes.append("Pod management Parallel: the controller adds or removes the pods all at once.")
    else:
        not_ready = max(0, (status.get("replicas") or 0) - (status.get("ready") or 0))
        if not_ready:
            risks.append(
                {
                    "level": "medium",
                    "message": f"{not_ready} of {status.get('replicas')} pod(s) are not ready. Under pod management "
                    "OrderedReady the controller waits while any pod is not Running and Ready, so the scaling may "
                    "not proceed until they recover.",
                }
            )
        notes.append(
            "Pod management OrderedReady: the controller adds pods one at a time in ordinal order, each after the "
            "previous one is ready, and removes them highest ordinal first."
        )
    if status.get("current_revision") and status.get("current_revision") != status.get("update_revision"):
        notes.append(
            f"A template update of this StatefulSet is not finished (revision {status.get('current_revision')} to "
            f"{status.get('update_revision')}), so new pods may start from either template."
        )
    return risks, notes
