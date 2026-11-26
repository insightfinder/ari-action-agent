"""`expand_pvc`: grow ONE PersistentVolumeClaim to a size the request names. Never shrinks, never deletes.

Which PVCs (decided 2026-11-26, checked on test the same day; docs/MEMORY.md):

- Only a PVC that a StatefulSet's `volumeClaimTemplates` created: its name is `<template>-<statefulset>-<ordinal>`
  of a StatefulSet in its namespace, it carries no mark of a tool that renders and re-applies it (Helm, Flux,
  Argo CD, kubectl apply: labels, annotations, field managers), its only owners (if any) are that StatefulSet
  or its pod, and the StatefulSet itself has no owner (an operator may reconcile its PVCs).
  Nothing re-applies such a PVC's size, so growing it in the cluster lasts (a Helm upgrade of its StatefulSet
  succeeded afterwards, on test).
- Everything else is refused with "change the size in Git": a PVC a chart or Kustomization renders gets its
  smaller size re-applied on the next upgrade, which the API server rejects, and the release stalls
  (`field can not be less than status.capacity`, seen on test) until Git catches up.
- Its StatefulSet must not be protected (`tools/protection.py`); its namespace neither.
- The StorageClass must allow expansion; the PVC must be Bound and not already resizing.
- The size must be written in the request (a size to reach, or an amount to add to the capacity now, which the
  proposal shows as a sum and writes as the size it comes to), larger than the current capacity, and at most
  max(2 x capacity, capacity + 50Gi) per request (a slipped digit is not approved by mistake).

Risk tier `high`: the disk cannot be made smaller again and costs more from then on. Verified from the PVC's
own status (`rollout.wait_pvc_expanded`): its capacity reaches the approved size.
"""

import re

from ..stepkit import refusal
from ..tools import policy, protection, quantities
from .base import ActionSpec, Operation

_GI = 2**30
GROWTH_FACTOR = 2
GROWTH_ALLOWANCE = 50 * _GI
# PVC conditions and resize states that mean a resize is already under way (or stuck).
_RESIZING_CONDITIONS = ("Resizing", "FileSystemResizePending", "ControllerResizeError", "NodeResizeError")


def _bytes(value) -> int | None:
    try:
        return quantities.memory_bytes(value) if value else None
    except (ValueError, ArithmeticError):
        return None


def _gib(num_bytes: int) -> str:
    gib = num_bytes / _GI
    return f"{gib:g}Gi" if gib == int(gib) else quantities.format_memory(num_bytes)


def _quantity(num_bytes: int) -> str:
    """Bytes as the shortest exact Kubernetes quantity (30Gi, 1536Mi, or plain bytes)."""
    for unit, factor in (("Ti", 2**40), ("Gi", _GI), ("Mi", 2**20), ("Ki", 2**10)):
        if num_bytes % factor == 0:
            return f"{num_bytes // factor}{unit}"
    return str(num_bytes)


def claim_template_owner(pvc: dict, statefulsets: list[dict]) -> tuple[dict, str, int] | None:
    """`(statefulset, template, ordinal)` when the PVC's name is `<template>-<statefulset>-<ordinal>` of one of
    `statefulsets`; None otherwise."""
    for sts in statefulsets:
        for template in sts.get("volume_claim_templates") or []:
            m = re.fullmatch(rf"{re.escape(template.get('name') or '')}-{re.escape(sts.get('name') or '')}-(\d+)", pvc.get("name") or "")
            if template.get("name") and m:
                return sts, template["name"], int(m.group(1))
    return None


def applier(pvc: dict) -> str | None:
    """What renders and re-applies this PVC (Helm, Flux, Argo CD), or None."""
    labels, annotations = pvc.get("applier_labels") or {}, pvc.get("applier_annotations") or {}
    if labels.get("helm.toolkit.fluxcd.io/name"):
        return f"Flux HelmRelease {labels.get('helm.toolkit.fluxcd.io/namespace') or pvc.get('namespace')}/{labels['helm.toolkit.fluxcd.io/name']}"
    if annotations.get("meta.helm.sh/release-name"):
        return f"Helm release {annotations['meta.helm.sh/release-name']}"
    if labels.get("kustomize.toolkit.fluxcd.io/name"):
        return f"Flux Kustomization {labels['kustomize.toolkit.fluxcd.io/name']}"
    if labels.get("argocd.argoproj.io/instance") or any(k.startswith("argocd.argoproj.io/") for k in annotations):
        return "Argo CD"
    if "kubectl.kubernetes.io/last-applied-configuration" in annotations:
        return "kubectl apply (a manifest applied as is; a StatefulSet's own PVCs never carry this)"
    if labels or annotations:
        return "a tool that marks it as managed (" + ", ".join(sorted({**labels, **annotations})) + ")"
    if pvc.get("applier_managers"):
        return "a tool that applies it (field managers " + ", ".join(pvc["applier_managers"]) + ")"
    return None


def resizing(pvc: dict) -> str | None:
    """Why a resize of this PVC is already under way, or None."""
    capacity, requested, allocated = _bytes(pvc.get("capacity")), _bytes(pvc.get("requested")), _bytes(pvc.get("allocated"))
    if pvc.get("resize_status"):
        return f"resize state {pvc['resize_status']}"
    busy = [c for c in pvc.get("conditions") or [] if c in _RESIZING_CONDITIONS]
    if busy:
        return "condition " + ", ".join(busy)
    if capacity is not None and requested is not None and requested > capacity:
        return f"{pvc.get('requested')} is asked for but the volume has {pvc.get('capacity')}"
    if capacity is not None and allocated is not None and allocated > capacity:
        return f"{pvc.get('allocated')} is allocated but the volume has {pvc.get('capacity')}"
    return None


def eligibility(state: dict) -> tuple[str | None, bool]:
    """`(why this PVC may not be grown by this agent, whether that is a protection refusal)`; `(None, False)`
    when it may. Checked at proposal and again at approval."""
    pvc = state.get("pvc") or {}
    ref = f"PVC {pvc.get('namespace')}/{pvc.get('name')}"
    tool = applier(pvc)
    if tool:
        return (
            f"{ref} is rendered and re-applied by {tool}. Grown here, its next upgrade or reconcile would try to set "
            "the smaller size from Git, which the API server rejects, and that release would stop deploying until "
            "Git is changed. Change the size in Git (the chart values or the manifest) instead; nothing was proposed."
        ), False
    owner = state.get("owner")
    if owner is None:
        return (
            f"{ref} was not created by a StatefulSet's volumeClaimTemplates, so this agent cannot tell whether "
            "something re-applies its size. This agent grows only such PVCs; change this one where it is defined. "
            "Nothing was proposed."
        ), False
    sts = owner["statefulset"]
    allowed_owners = {("StatefulSet", sts), ("Pod", f"{sts}-{owner['ordinal']}")}
    others = [o for o in pvc.get("owners") or [] if (o.get("kind"), o.get("name")) not in allowed_owners]
    if others:
        return f"{ref} is also owned by " + ", ".join(f"{o.get('kind')} {o.get('name')}" for o in others) + "; nothing was proposed.", False
    if owner.get("sts_owner"):
        return (
            f"{ref} belongs to StatefulSet {sts}, which {owner['sts_owner']} manages; an operator may reconcile its "
            "PVCs' size too. Change it through that resource; nothing was proposed."
        ), False
    protected = protection.why_protected(pvc.get("namespace"), sts)
    if protected:
        return f"{ref} belongs to StatefulSet {sts}. {protected}", True
    return None, False


class ExpandPvc(ActionSpec):
    name = "expand_pvc"
    # Never offered to the model for a workload: a PVC is resolved by its own name (`find_workload`).
    workload_kinds = ()
    target_kinds = ("PersistentVolumeClaim",)
    prompt_line = "grow a PVC to a larger size."
    not_asked = (
        "The request does not itself ask for a disk to be resized, so nothing was proposed. Ask for it directly, "
        "e.g. \"expand pvc {name} in namespace {namespace} to 20Gi\"."
    )
    rbac_rule = 'patch on "persistentvolumeclaims" (core API group; the oncallari chart grants it from 0.3.19)'

    async def load_state(self, ctx, target):
        pvc = await ctx.tool("get_pvc")(target["namespace"], target["name"])
        if pvc is None:
            return {"pvc": None}
        found = claim_template_owner(pvc, await ctx.tool("list_statefulsets")(target["namespace"]))
        owner = None
        if found:
            sts, template, ordinal = found
            sts_owner = sts.get("owner") or {}
            owner = {"statefulset": sts["name"], "template": template, "ordinal": ordinal, "replicas": sts.get("replicas"),
                     "sts_owner": f"{sts_owner['kind']} {sts_owner.get('name')}" if sts_owner.get("kind") else None,
                     "template_storage": next((t.get("storage") for t in sts.get("volume_claim_templates") or [] if t.get("name") == template), None)}
        return {"pvc": pvc, "owner": owner, "storage_class": await ctx.tool("get_storageclass")(pvc.get("storage_class"))}

    def target(self, action):
        return {"kind": "PersistentVolumeClaim", "namespace": action.get("namespace"), "name": action.get("pvc")}

    # ------------------------------------------------------------ plan

    def plan(self, ctx, state, change, env):
        pvc = state.get("pvc")
        if pvc is None:
            return None, refusal("The PVC does not exist; nothing was proposed.")
        ref = f"PVC {pvc['namespace']}/{pvc['name']}"
        if pvc.get("deleting"):
            return None, refusal(f"{ref} is being deleted; nothing was proposed.")
        why, protected = eligibility(state)
        if why:
            return None, refusal(why, **({"protected": True} if protected else {}))
        if pvc.get("phase") != "Bound":
            return None, refusal(f"{ref} is {pvc.get('phase') or 'not bound'}, not Bound: there is no volume to grow yet.")
        sc = state.get("storage_class")
        if sc is None or not sc.get("allow_volume_expansion"):
            return None, refusal(
                f"StorageClass {pvc.get('storage_class') or '(none)'} of {ref} "
                + ("does not exist" if sc is None else "does not allow volume expansion")
                + ", so the volume cannot be grown in place. Nothing was proposed."
            )
        busy = resizing(pvc)
        if busy:
            return None, refusal(f"A resize of {ref} is already under way ({busy}); ask again once it has finished.")
        asked, problem = policy.requested_storage(ctx.prompt)
        if problem or asked is None:
            return None, refusal(f"{problem} Nothing was proposed for {ref} (now {pvc.get('capacity')}).")
        amount, capacity = _bytes(asked["value"]), _bytes(pvc.get("capacity"))
        if amount is None or capacity is None:
            return None, refusal(f"The size {asked['quote']!r} or the capacity of {ref} could not be read; nothing was proposed.")
        # An amount to add becomes the size to reach here, from the capacity now; the proposal shows the sum and
        # the write sets that size, so an approval never adds twice.
        target = capacity + amount if asked.get("relative") else amount
        size = _quantity(target) if asked.get("relative") else asked["value"]
        if target <= capacity:
            return None, refusal(
                f"{ref} already has {pvc.get('capacity')}; {size} is not larger. This agent only grows a disk "
                "(a volume cannot be made smaller). Nothing was proposed."
            )
        cap = max(capacity * GROWTH_FACTOR, capacity + GROWTH_ALLOWANCE)
        if target > cap:
            return None, refusal(
                f"{size} is more than this agent grows a disk in one step: at most {_gib(cap)} for {ref} "
                f"(now {pvc.get('capacity')}; twice its size or 50Gi more, whichever is larger). Ask for a size up to "
                "that, or change it in Git. Nothing was proposed."
            )
        owner = state["owner"]
        action = {
            "action": "expand_pvc",
            "namespace": pvc["namespace"],
            "pvc": pvc["name"],
            "pvc_uid": pvc.get("uid"),
            "storage": size,
            "workload_kind": "StatefulSet",
            "workload": owner["statefulset"],
        }
        risks = [
            {
                "level": "high",
                "message": (
                    f"Permanent: {ref} cannot be made smaller again, and the larger volume costs more from now on "
                    f"({pvc.get('capacity')} -> {size})."
                ),
            }
        ]
        # The in-tree name still provisions EBS (migrated to the CSI driver); stg and prod keep a gp2 class with it.
        if (sc.get("provisioner") or "") in ("ebs.csi.aws.com", "kubernetes.io/aws-ebs"):
            risks.append(
                {
                    "level": "info",
                    "message": (
                        "AWS EBS: the new size is usable within seconds, but the volume can be changed again only once "
                        "this change has finished optimizing (minutes to many hours; longer for large volumes), and at "
                        "most four times in 24 hours. A resize asked for before then waits and is retried."
                    ),
                }
            )
        notes = [
            "Online expansion: pods keep running; the filesystem grows while mounted (or when the volume is next mounted).",
        ]
        if owner.get("template_storage"):
            notes.append(
                f"StatefulSet {owner['statefulset']}'s claim template still asks for {owner['template_storage']}: a "
                "replica added later gets that size. The template cannot be changed on a running StatefulSet."
            )
        facts = {
            "before": {"storage": pvc.get("capacity")},
            "after": {"storage": size},
            "requested": {
                "storage": asked["quote"],
                **({"computed": f"{pvc.get('capacity')} + {asked['value']} = {size}"} if asked.get("relative") else {}),
            },
            "scale_down": False,
            "high_replicas": False,
            "new_pods": 0,
            "risks": risks,
            "notes": notes,
            "changed_object": {"kind": "PersistentVolumeClaim", "name": pvc["name"], "labels": pvc.get("labels") or {}, "annotations": {}},
            "rendered_changes": [],
            "nothing_to_persist": (
                "Nothing to persist: nothing re-applies the size of a PVC that a StatefulSet's claim template created."
            ),
            "action_name": f"Grow {ref} from {pvc.get('capacity')} to {size}",
        }
        return (action, facts), None

    # ------------------------------------------------------------ write

    def operations(self, action):
        return [
            Operation(
                verb="patch",
                kind="PersistentVolumeClaim",
                resource="PersistentVolumeClaim",
                name=action["pvc"],
                namespace=action.get("namespace"),
                body={"spec": {"resources": {"requests": {"storage": action["storage"]}}}},
                patch_type="merge",
            )
        ]

    def summarize(self, op, raw, action):
        spec = raw.get("spec") or {}
        return {
            "resource": op.resource,
            "name": (raw.get("metadata") or {}).get("name") or action.get("pvc"),
            "storage": ((spec.get("resources") or {}).get("requests") or {}).get("storage"),
        }

    def patch_action(self, proposal):
        return {k: proposal.get(k) for k in ("action", "namespace", "pvc", "pvc_uid", "storage", "workload_kind", "workload")}

    # ------------------------------------------------------------ approval-time checks

    def snapshot(self, state):
        pvc = state.get("pvc") or {}
        return {"pvc_uid": pvc.get("uid"), "requested": pvc.get("requested"), "capacity": pvc.get("capacity")}

    def drift(self, proposal, state):
        pvc = state.get("pvc")
        name = proposal.get("pvc")
        if pvc is None:
            return [f"PVC {name} no longer exists"]
        snapshot = proposal.get("snapshot") or {}
        if pvc.get("uid") != snapshot.get("pvc_uid"):
            return [f"PVC {name} was replaced (new uid)"]
        changes = []
        if pvc.get("deleting"):
            changes.append(f"PVC {name} is being deleted")
        why, _ = eligibility(state)
        if why:
            changes.append(why)
        if _bytes(pvc.get("requested")) != _bytes(snapshot.get("requested")):
            changes.append(f"its requested size changed from {snapshot.get('requested')} to {pvc.get('requested')}")
        return changes

    def at_target(self, action, state):
        pvc = state.get("pvc") or {}
        asked, target = _bytes(pvc.get("requested")), _bytes(action.get("storage"))
        return pvc.get("uid") == action.get("pvc_uid") and asked is not None and target is not None and asked >= target

    def verify_target(self, action):
        return {"kind": "pvc_expanded", "pvc": action["pvc"], "storage": action["storage"]}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        return await ctx.tool("wait_pvc_expanded")(action["namespace"], action["pvc"], action["pvc_uid"], action["storage"], max_wait)

    def base_tier(self, facts):
        return "high"

    def changed_object(self, action, state):
        pvc = state.get("pvc") or {}
        return {"labels": pvc.get("labels") or {}, "annotations": {}}

    def lifetime_notice(self, ownership, chain):
        return (
            "Lasts: nothing re-applies the size of a PVC that a StatefulSet's claim template created, and a volume is "
            "never made smaller."
        )

    def lifetime_risk(self, ownership):
        return None
