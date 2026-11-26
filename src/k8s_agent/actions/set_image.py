"""`set_image`: change the image of one container of a Deployment, StatefulSet or DaemonSet, the way `kubectl
set image` does (strategic merge patch on the container, by name). The workload's own controller rolls its
pods under its own update strategy.

- Proposed only when the request itself asks for an image change (`policy.asks_set_image`): an on-call prompt
  that quotes an image from an ImagePullBackOff finding is not one.
- The image is the request's own words: a full reference the request writes verbatim (not as part of a longer
  one), or a tag the request writes as a tag ("tag 1.4", "version 1.4", "checkout:1.4"), put on the
  container's current repository. A different repository is allowed and listed as a `high` risk.
- The container must exist in the live template (a strategic merge with an unknown name would add one); with
  one container the request need not name it.
- Refused where the controller would not roll it out: a paused Deployment, an `OnDelete` update strategy.
- Whether the image exists in its registry cannot be checked from here: a wrong one shows as ImagePullBackOff,
  which the verification reports (`new_pods_failing`); a rolling update keeps the old pods serving meanwhile.
- Image automation and Helm upgrades set their own image again; the drift notice says how soon, the verifier
  reports `reverted` when it happens while watched, and the persistence hint names the field to change in Git.
"""

from ..stepkit import refusal
from ..tools import governance, policy
from .base import ActionSpec, Operation
from .restart_rollout import rollout_blocked, rollout_effects, workload_pods


def _images(template: dict | None) -> dict:
    return {c["name"]: c.get("image") for c in (template or {}).get("containers") or []}


class SetImage(ActionSpec):
    name = "set_image"
    workload_kinds = governance.WORKLOAD_KINDS
    target_kinds = governance.WORKLOAD_KINDS
    prompt_line = (
        "change the image of one container (like kubectl set image). Report `image` when the request writes a "
        "full image reference, or `tag` when it gives only a tag or version; and `container` when it names one."
    )
    not_asked = (
        "The request does not itself ask for an image change, so none was proposed. To have one proposed, ask for "
        "it directly, e.g. \"set the image of container <name> in {kind} {name} (namespace {namespace}) to "
        "<registry/repository:tag>\"."
    )
    rbac_rule = 'patch on "statefulsets" and "daemonsets" (apps group; the oncallari chart grants it from 0.3.15)'
    rbac_rule_kinds = ("StatefulSet", "DaemonSet")

    def accepts(self, prompt):
        return policy.asks_set_image(prompt)

    async def load_state(self, ctx, target):
        view = await ctx.tool("get_workload")(target["kind"], target["namespace"], target["name"])
        return {"workload": view, **({"deployment": view} if target["kind"] == "Deployment" else {})}

    def target(self, action):
        return {"kind": action.get("workload_kind") or "Deployment", "namespace": action.get("namespace"), "name": action.get("workload")}

    # ------------------------------------------------------------ plan

    def _new_image(self, ctx, current: str | None, change: dict) -> tuple[str | None, str | None]:
        """`(image, None)` or `(None, why not)`, from the request's own words only."""
        image, tag = change.get("image"), change.get("tag")
        if isinstance(image, str) and image.strip():
            image = image.strip()
            if not policy.valid_image(image):
                return None, f"{image!r} is not an image reference."
            if not policy.states_image(ctx.prompt, image):
                return None, "The request does not write the new image reference out in full; nothing was proposed."
            return image, None
        if isinstance(tag, str) and tag.strip():
            tag = tag.strip()
            if not policy.states_tag(ctx.prompt, tag):
                return None, (
                    "The request does not state the new tag clearly enough to act on (write it as \"tag <tag>\", "
                    "\"version <tag>\" or the full image reference)."
                )
            if not current:
                return None, "The container has no current image to put the tag on; give the full image reference."
            repository, _ = policy.split_image(current)
            return f"{repository}:{tag}", None
        return None, "The request does not state the new image; nothing was proposed."

    def plan(self, ctx, state, change, env):
        w = state["workload"]
        kind, ref = w["kind"], f"{w['kind']} {w['namespace']}/{w['name']}"
        blocked = rollout_blocked(w, "an image change")
        if blocked:
            return None, refusal(blocked)
        images = _images(w.get("template"))
        named = str(change.get("container") or "").strip()
        if named:
            if named not in images:
                return None, refusal(
                    f"{ref} has no container {named!r}; its containers are: {', '.join(sorted(images)) or 'none'}.",
                    containers=sorted(images),
                )
            container = named
        elif len(images) == 1:
            container = next(iter(images))
        else:
            return None, refusal(
                f"{ref} has more than one container; say which one: {', '.join(sorted(images))}.", containers=sorted(images)
            )
        current = images[container]
        image, why_not = self._new_image(ctx, current, change)
        if image is None:
            return None, refusal(why_not or "The request does not state the new image; nothing was proposed.")
        if image == current:
            return None, refusal(f"Container {container} of {ref} already runs {image}; nothing to change.")
        pods = workload_pods(w)
        risks, notes = rollout_effects(w)
        old_repo, new_repo = policy.split_image(current or "")[0], policy.split_image(image)[0]
        if current and old_repo != new_repo:
            risks.append(
                {
                    "level": "high",
                    "message": f"The image repository changes, from {old_repo} to {new_repo}: not only a new version. "
                    "Check it is the image meant.",
                }
            )
        notes.append(
            "Whether the new image exists, and can be pulled by the nodes, cannot be checked before it rolls out. "
            "If it cannot, the new pods fail with ImagePullBackOff and the verification says so; a rolling update "
            "keeps the old pods serving meanwhile."
        )
        obj = f"{ref} container {container}"
        action = {
            "action": "set_image",
            "namespace": w["namespace"],
            "deployment": w["name"] if kind == "Deployment" else None,
            "workload_kind": kind,
            "workload": w["name"],
            "container": container,
            "image": image,
        }
        facts = {
            "before": {"container": container, "image": current},
            "after": {"container": container, "image": image},
            "scale_down": False,
            "high_replicas": False,
            "new_pods": 0,
            "rolls_pods": True,
            "roll_message": f"An image change replaces every pod of this {kind} ({pods}).",
            "risks": risks,
            "notes": notes,
            "changed_object": {"kind": kind, "name": w["name"], "labels": w.get("labels") or {}, "annotations": w.get("annotations") or {}},
            "rendered_changes": [{"object": obj, "field": "image", "from": current, "to": image}],
            "action_name": f"Set image of {ref} container {container} to {image}",
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
                body={"spec": {"template": {"spec": {"containers": [{"name": action["container"], "image": action["image"]}]}}}},
                patch_type="strategic",
            )
        ]

    def summarize(self, op, raw, action):
        containers = (((raw.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
        return {**super().summarize(op, raw, action), "images": {c.get("name"): c.get("image") for c in containers}}

    def patch_action(self, proposal):
        return {
            **{k: proposal.get(k) for k in ("action", "namespace", "deployment", "workload", "container", "image")},
            "workload_kind": proposal.get("workload_kind") or "Deployment",
        }

    # ------------------------------------------------------------ approval-time checks

    def snapshot(self, state):
        w = state["workload"]
        return {"uid": w.get("uid"), "images": _images(w.get("template")), "strategy": (w.get("strategy") or {}).get("type")}

    def drift(self, proposal, state):
        w, snapshot = state["workload"], proposal["snapshot"]
        changes = []
        if snapshot.get("uid") and w.get("uid") != snapshot.get("uid"):
            changes.append(f"{w['kind']} {w['name']} was deleted and created again")
        container = proposal.get("container")
        now = _images(w.get("template"))
        if container not in now:
            changes.append(f"container {container} is no longer in the pod template")
        elif now[container] != (snapshot.get("images") or {}).get(container):
            changes.append(f"container {container} now runs {now[container]} (was {(snapshot.get('images') or {}).get(container)})")
        if w.get("paused"):
            changes.append("it is paused now")
        if (w.get("strategy") or {}).get("type") != snapshot.get("strategy"):
            changes.append(f"its update strategy is now {(w.get('strategy') or {}).get('type')}")
        return changes

    def at_target(self, action, state):
        return _images(state["workload"].get("template")).get(action["container"]) == action["image"]

    def verify_target(self, action):
        return {"kind": "images", "images": {action["container"]: action["image"]}}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        kind = action.get("workload_kind") or "Deployment"
        if kind == "Deployment":
            return await ctx.tool("wait_rollout")(action["namespace"], action["workload"], self.verify_target(action), applied_at, max_wait)
        return await ctx.tool("wait_workload_rollout")(
            kind, action["namespace"], action["workload"], applied_at, max_wait, target=self.verify_target(action)
        )

    def base_tier(self, facts):
        return "medium"

    def changed_object(self, action, state):
        w = state["workload"]
        return {"labels": w.get("labels") or {}, "annotations": w.get("annotations") or {}}
