"""`rollback`: put a Deployment's pod template back to an earlier revision, the way `kubectl rollout undo`
does: the template of that revision's ReplicaSet replaces the Deployment's (JSON patch, `replace
/spec/template`), and the controller rolls the pods to it.

- Proposed only when the request itself asks for a rollback (`policy.asks_rollback`). The revision is the
  previous one, or the number the request states (quoted, like every other value).
- The proposal carries identifiers only (revisions, the ReplicaSet's name and uid) and the images, never the
  template: a template holds env values, and a proposal is published as an artifact. The patch is built from
  the ReplicaSet inside the write tools (`build_operations`), for the dry run and the write alike, and starts
  with a JSON patch `test` of the Deployment's revision, so a rollout that lands between approval and write
  makes the API refuse the patch instead of rolling back from a revision nobody approved.
- Image automation and Helm upgrades roll forward again; the drift notice says how soon, the verifier reports
  `reverted` when it happens while watched, and the persistence hint names the images to pin in Git.
"""

import copy

from ..stepkit import refusal
from ..tools import policy
from .base import DeploymentAction, Operation, artifact_int, extracted_int

REVISION = "deployment.kubernetes.io/revision"
# JSON pointer for the revision annotation ("/" in a key is written "~1").
_REVISION_PATH = "/metadata/annotations/deployment.kubernetes.io~1revision"


def revision_of(obj: dict) -> int | None:
    value = obj.get("revision") if "revision" in obj else (obj.get("annotations") or {}).get(REVISION)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _images(template: dict | None) -> dict:
    return {c["name"]: c.get("image") for c in (template or {}).get("containers") or []}


def _resources(template: dict | None) -> dict:
    return {c["name"]: c.get("resources") or {} for c in (template or {}).get("containers") or []}


class Rollback(DeploymentAction):
    name = "rollback"
    prompt_line = (
        "roll the Deployment back to an earlier revision (like kubectl rollout undo): the previous revision, or "
        "the revision number the request states. Only when the request asks for a rollback."
    )
    not_asked = (
        "The request does not itself ask for a rollback, so none was proposed. To have one proposed, ask for it "
        "directly, e.g. \"roll back deployment {name} in namespace {namespace} to the previous revision\"."
    )

    def accepts(self, prompt):
        return policy.asks_rollback(prompt)

    async def load_state(self, ctx, target):
        dep = await ctx.tool("get_deployment")(target["namespace"], target["name"])
        sets = await ctx.tool("list_replicasets")(target["namespace"], target["name"], dep.get("label_selector") or dep.get("selector") or None)
        return {"deployment": dep, "workload": dep, "replicasets": sets}

    # ------------------------------------------------------------ plan

    def plan(self, ctx, state, change, env):
        dep = state["deployment"]
        ref = f"Deployment {dep['namespace']}/{dep['name']}"
        if dep.get("paused"):
            return None, refusal(f"{ref} is paused (spec.paused); a paused Deployment is not rolled back. Resume it first.")
        by_revision = {revision_of(rs): rs for rs in state.get("replicasets") or [] if revision_of(rs) is not None}
        current_revision = revision_of(dep)
        if current_revision is None or current_revision not in by_revision:
            return None, refusal(f"The current revision of {ref} could not be determined from its ReplicaSets; nothing was proposed.")
        available = sorted(r for r in by_revision if r != current_revision)

        wanted = extracted_int(change.get("revision"))
        if wanted is not None:
            if not policy.quote_in_prompt(change.get("revision_quote"), ctx.prompt, wanted):
                return None, refusal("The request does not state the revision to roll back to explicitly enough to act on.")
            if wanted == current_revision:
                return None, refusal(f"{ref} already runs revision {wanted}; nothing to roll back.")
            if wanted not in by_revision:
                return None, refusal(
                    f"Revision {wanted} of {ref} is no longer kept (revisionHistoryLimit); revisions available: "
                    + (", ".join(map(str, available)) or "none"),
                    revisions=available,
                )
            to_revision = wanted
        else:
            older = [r for r in available if r < current_revision]
            if not older:
                return None, refusal(f"{ref} has no earlier revision to roll back to (current revision {current_revision}).")
            to_revision = older[-1]

        current, target = by_revision[current_revision], by_revision[to_revision]
        now_images, then_images = _images(dep.get("template")), _images(target.get("template"))
        now_res, then_res = _resources(dep.get("template")), _resources(target.get("template"))
        rendered = []
        for container in sorted(set(now_images) | set(then_images)):
            obj = f"Deployment {dep['namespace']}/{dep['name']} container {container}"
            if now_images.get(container) != then_images.get(container):
                rendered.append({"object": obj, "field": "image", "from": now_images.get(container), "to": then_images.get(container)})
            for side in ("requests", "limits"):
                for resource in ("cpu", "memory"):
                    a = (now_res.get(container, {}).get(side) or {}).get(resource)
                    b = (then_res.get(container, {}).get(side) or {}).get(resource)
                    if a != b:
                        rendered.append({"object": obj, "field": f"resources.{side}.{resource}", "from": a, "to": b})
        notes = [
            (
                "Only the pod template goes back: ConfigMaps, Secrets, the replica count and everything else outside "
                f"the template keep their current values, and revision {to_revision} must still work with them and "
                "with the current data."
            )
        ]
        if not rendered:
            notes.append(
                "The two revisions differ only in fields this agent does not read (env, args, volumes and similar), "
                "so the difference cannot be shown here."
            )
            rendered = [
                {
                    "object": f"Deployment {dep['namespace']}/{dep['name']}",
                    "field": "spec.template",
                    "from": f"revision {current_revision}",
                    "to": f"revision {to_revision}",
                }
            ]
        image_text = ", ".join(f"{c} {then_images.get(c)}" for c in sorted(then_images) if then_images.get(c) != now_images.get(c))
        action = {
            "action": "rollback",
            "namespace": dep["namespace"],
            "deployment": dep["name"],
            "from_revision": current_revision,
            "to_revision": to_revision,
            "replicaset": target["name"],
            "replicaset_uid": target.get("uid"),
            "images": then_images,
        }
        facts = {
            "before": {"revision": current_revision, "replicaset": current["name"], "images": now_images},
            "after": {"revision": to_revision, "replicaset": target["name"], "images": then_images},
            "scale_down": False,
            "high_replicas": False,
            "new_pods": 0,
            "rolls_pods": True,
            "roll_message": f"A rollback replaces every pod of this Deployment ({dep['replicas']}) with revision {to_revision}'s template.",
            "notes": notes,
            "changed_object": {"kind": "Deployment", "name": dep["name"], "labels": dep["labels"], "annotations": dep["annotations"]},
            "rendered_changes": rendered,
            "action_name": (
                f"Roll back {ref} from revision {current_revision} to {to_revision}" + (f" ({image_text})" if image_text else "")
            ),
        }
        return (action, facts), None

    # ------------------------------------------------------------ write

    def operations(self, action):
        raise ValueError("a rollback's patch is built from the ReplicaSet at write time (build_operations)")

    async def build_operations(self, action, kube):
        raw = await kube.get("ReplicaSet", action["replicaset"], action["namespace"])
        meta = raw.get("metadata") or {}
        if action.get("replicaset_uid") and meta.get("uid") != action["replicaset_uid"]:
            raise ValueError(f"ReplicaSet {action['replicaset']} was replaced since the proposal")
        template = copy.deepcopy((raw.get("spec") or {}).get("template") or {})
        if not template:
            raise ValueError(f"ReplicaSet {action['replicaset']} has no pod template")
        # The hash label is the ReplicaSet's own; the Deployment controller adds it back, as kubectl does.
        ((template.get("metadata") or {}).get("labels") or {}).pop("pod-template-hash", None)
        return [
            Operation(
                verb="patch",
                kind="Deployment",
                resource="Deployment",
                name=action["deployment"],
                namespace=action.get("namespace"),
                body=[
                    {"op": "test", "path": _REVISION_PATH, "value": str(artifact_int(action["from_revision"]))},
                    {"op": "replace", "path": "/spec/template", "value": template},
                ],
                patch_type="json",
            )
        ]

    def summarize(self, op, raw, action):
        containers = (((raw.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
        return {**super().summarize(op, raw, action), "images": {c.get("name"): c.get("image") for c in containers}}

    def patch_action(self, proposal):
        return {
            **{k: proposal.get(k) for k in ("action", "namespace", "deployment", "replicaset", "replicaset_uid")},
            "from_revision": artifact_int(proposal["from_revision"]),
            "to_revision": artifact_int(proposal["to_revision"]),
            "images": dict(proposal.get("images") or {}),
        }

    # ------------------------------------------------------------ approval-time checks

    def snapshot(self, state):
        dep = state["deployment"]
        return {"deployment_uid": dep.get("uid"), "revision": revision_of(dep)}

    def drift(self, proposal, state):
        dep, snapshot = state["deployment"], proposal["snapshot"]
        changes = []
        if snapshot.get("deployment_uid") and dep.get("uid") != snapshot.get("deployment_uid"):
            changes.append(f"Deployment {dep['name']} was deleted and created again")
        then, now = artifact_int(snapshot.get("revision")), revision_of(dep)
        if now != then:
            changes.append(f"it rolled out again since the proposal (revision {then} -> {now})")
        target = next((rs for rs in state.get("replicasets") or [] if rs.get("name") == proposal.get("replicaset")), None)
        if target is None or (proposal.get("replicaset_uid") and target.get("uid") != proposal.get("replicaset_uid")):
            changes.append(f"revision {artifact_int(proposal.get('to_revision'))}'s ReplicaSet {proposal.get('replicaset')} is gone")
        if dep.get("paused"):
            changes.append("it is paused now")
        return changes

    def at_target(self, action, state):
        """Rolled back: the approved ReplicaSet is the Deployment's current revision (the controller gives it
        the next revision number when the template goes back to it)."""
        target = next((rs for rs in state.get("replicasets") or [] if rs.get("name") == action.get("replicaset")), None)
        return bool(target) and target.get("uid") == action.get("replicaset_uid") and revision_of(target) == revision_of(state["deployment"])

    def verify_target(self, action):
        return {"kind": "images", "images": action.get("images") or {}}

    def base_tier(self, facts):
        return "medium"
