"""The action registry: one module per kind of change this agent can propose and, once approved, apply.

`propose_change` and `apply_approved_change` are generic over these specs; `tools/write.py` executes the
`Operation`s a spec builds, so what was dry-run is exactly what is applied. Adding a kind of change is one
module here plus its registration in `actions/__init__.py` — the approval gate, the drift re-check, the
idempotent retry and the verification stay where they are.

Everything a spec decides is code: the model only says which change was asked for and quotes the words each
value came from (see `propose_change._extract`).
"""

from dataclasses import dataclass

from ..tools import governance


@dataclass(frozen=True)
class Operation:
    """One API call a change makes. `resource` is the label results and summaries use ("Deployment.Scale")."""

    verb: str  # "patch" | "delete"
    kind: str
    name: str | None
    namespace: str | None
    resource: str
    body: dict | None = None
    patch_type: str | None = None  # "merge" | "strategic" (patch only)
    subresource: str | None = None
    # delete only: e.g. {"uid": ...}, so the API server deletes that very object or answers 409.
    preconditions: dict | None = None


def extracted_int(value) -> int | None:
    """A whole number from the model's JSON, or None (bools, fractions and text are not counts)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number == int(number) else None


def artifact_int(value):
    """A2A artifacts round-trip through protobuf Value, whose only number type is a double: 3 comes back
    as 3.0. Anything else is returned unchanged."""
    return int(value) if isinstance(value, float) and value == int(value) else value


def target_of(action: dict) -> dict:
    """`{kind, namespace, name}` of the object a proposal is about. Proposals made before `target` existed
    (an approval can arrive hours later, across a deploy) carry only `namespace` and `deployment`."""
    target = action.get("target") or {}
    if target.get("kind") and target.get("name"):
        return {"kind": target["kind"], "namespace": target.get("namespace"), "name": target["name"]}
    return {"kind": "Deployment", "namespace": action.get("namespace"), "name": action.get("deployment")}


class ActionSpec:
    """The parts of a change that differ from one kind of change to the next."""

    name: str = ""
    # Kinds of the object the change lands on (the proposal's `target.kind`).
    target_kinds: tuple[str, ...] = ()
    # Kinds of workload `find_workload` resolves the request to for which this action is offered to the model.
    workload_kinds: tuple[str, ...] = ("Deployment",)
    # One line for the extraction prompt: what this action is, in the request's terms.
    prompt_line: str = ""
    # Whether planning needs the node list and every active pod (the node-capacity cap).
    needs_capacity: bool = False
    # Set when the RBAC this action needs is newer than some deployed charts: a 403 on the dry run then
    # becomes a refusal naming the missing rule instead of a failed step.
    rbac_rule: str | None = None
    # When the extracted action is not what the request itself asks for (`accepts` is False), plan this one
    # instead. Decided in code, never by the model.
    fallback: str | None = None

    # Whether the step may be retried after failing before its write was sent. A retry that finds the approved
    # result in place counts it as its own only by the fields' owner (`written_fields`); an action that leaves
    # no owner to read (a delete) ends the step on such a failure instead (user, 2026-11-27), so only a lost
    # worker, which may have sent the write, reaches a retry.
    retry_before_write: bool = True

    # Said when `accepts` is False and there is no fallback: the request does not itself ask for this change.
    not_asked: str = ""

    def accepts(self, prompt: str) -> bool:
        """Whether the request's own words ask for this kind of change, decided in code. The model's choice of
        action alone is never enough for a change that does not take a quoted value (a restart, a delete): an
        on-call prompt quoting "restarted 5 times" is not a request to restart."""
        return True

    async def resolve_target(self, ctx, workload: dict, change: dict) -> tuple[dict | None, dict | None]:
        """`(target, None)` or `(None, refusal)`. `find_workload` resolves a workload (its view is `workload`);
        an action on something inside it (a pod) narrows it down here."""
        return {"kind": workload.get("kind") or "Deployment", "namespace": workload["namespace"], "name": workload["name"]}, None

    async def load_state(self, ctx, target: dict) -> dict:
        raise NotImplementedError

    def target(self, action: dict) -> dict:
        """`{kind, namespace, name}` of the object the change lands on (the proposal's `target`)."""
        return {"kind": "Deployment", "namespace": action.get("namespace"), "name": action.get("deployment")}

    async def verify(self, ctx, action: dict, proposal: dict, before: dict | None, applied_at: str | None, max_wait: float) -> dict:
        """The outcome, read from cluster state after the write. `before` is the state read just before this
        attempt's write, or None when the write had already landed (a retried attempt)."""
        target = target_of(proposal)
        return await ctx.tool("wait_rollout")(target["namespace"], target["name"], self.verify_target(action), applied_at, max_wait)

    def lifetime_notice(self, ownership: dict, chain: dict) -> str:
        """One plain sentence on how long the change lasts."""
        return governance.drift_notice(ownership, chain)

    def lifetime_risk(self, ownership: dict) -> str | None:
        """The level the lifetime notice is listed at among the proposal's risks (None: not listed). A change
        to a GitOps- or Helm-owned object that the next reconcile or upgrade can undo is a `medium` risk."""
        return "medium" if (ownership or {}).get("class") != "unowned" else None

    def plan(self, ctx, state: dict, change: dict, env: dict):
        """`((action, facts), None)` or `(None, refusal)`."""
        raise NotImplementedError

    def operations(self, action: dict) -> list[Operation]:
        raise NotImplementedError

    def summarize(self, op: Operation, raw: dict, action: dict) -> dict:
        meta = raw.get("metadata") or {}
        return {"resource": op.resource, "name": meta.get("name"), "generation": meta.get("generation")}

    def patch_action(self, proposal: dict) -> dict:
        """The action to execute, rebuilt from an approved (round-tripped) proposal."""
        raise NotImplementedError

    def drift(self, proposal: dict, state: dict) -> list[str]:
        raise NotImplementedError

    def at_target(self, action: dict, state: dict) -> bool:
        raise NotImplementedError

    def written_fields(self, action: dict, proposal: dict) -> dict | None:
        """`{kind, namespace, name, paths}`: the object and the fields (`tools/ownership` paths) this change sets
        to a value other than the one in the proposal's snapshot. Once this agent's write lands, each of them is
        owned by its field manager; set by anyone else, they are not. None when the change leaves no such record
        (a delete, a restart)."""
        return None

    def verify_target(self, action: dict) -> dict:
        raise NotImplementedError

    def base_tier(self, facts: dict) -> str:
        return "high"

    def changed_object(self, action: dict, state: dict) -> dict:
        """Labels and annotations of the object the change lands on, for the GitOps chain."""
        raise NotImplementedError

    def snapshot(self, state: dict) -> dict:
        raise NotImplementedError


class DeploymentAction(ActionSpec):
    """A change to a Deployment or its autoscaler. The state is the Deployment's view and its autoscaler."""

    target_kinds = ("Deployment",)

    async def load_state(self, ctx, target: dict) -> dict:
        dep = await ctx.tool("get_deployment")(target["namespace"], target["name"])
        autoscaler = await ctx.tool("find_autoscaler")(target["namespace"], target["name"])
        return {"deployment": dep, "workload": dep, "autoscaler": autoscaler}

    def snapshot(self, state: dict) -> dict:
        dep = state["deployment"]
        return {
            "deployment_uid": dep.get("uid"),
            "deployment_generation": dep["generation"],
            "deployment_replicas": dep["replicas"],
            "container_resources": {c["name"]: c["resources"] for c in dep["template"]["containers"]},
            "autoscaler": governance.autoscaler_fingerprint(state.get("autoscaler")),
        }

    def changed_object(self, action: dict, state: dict) -> dict:
        dep = state["deployment"]
        return {"labels": dep["labels"], "annotations": dep["annotations"]}

    def drift(self, proposal: dict, state: dict) -> list[str]:
        """Compared for every Deployment action: the Deployment's uid, and the autoscaler governing it (its
        appearance, disappearance or change invalidates any approval)."""
        snapshot = proposal["snapshot"]
        changes = []
        # Deleted and made again (by hand, or by a GitOps tool after a rename) is another Deployment. Proposals
        # made before the uid was recorded carry none.
        uid = (state.get("deployment") or {}).get("uid")
        if snapshot.get("deployment_uid") and uid != snapshot["deployment_uid"]:
            changes.append("the Deployment was deleted and created again (new uid)")
        current_fp = governance.autoscaler_fingerprint(state.get("autoscaler"))
        proposed_fp = snapshot.get("autoscaler")
        if (current_fp is None) != (proposed_fp is None):
            changes.append(
                "an autoscaler now governs this Deployment" if current_fp else "the autoscaler governing it is gone"
            )
        elif current_fp and proposed_fp:
            for key in ("kind", "name", "min", "max", "generation"):
                if artifact_int(current_fp.get(key)) != artifact_int(proposed_fp.get(key)):
                    changes.append(f"autoscaler {key} changed from {artifact_int(proposed_fp.get(key))} to {current_fp.get(key)}")
        return changes
