"""The only capability that changes the cluster — and only a change a
person approved.

Two independent gates, the same shape as the oncall kind's `publish_pr`:

1. `ctx.confirmed` is set only by `plan_task`'s confirmation bypass, after
   the A2A executor matched a structured `confirm_pending_action` id
   against this task's own `pending_action` artifact. The planner's JSON
   cannot set it, so no plan can schedule this step into running.
2. The params must be a complete proposal this kind made (known action,
   id, snapshot). A confirmation of anything else fails here.

Before writing, the target is re-read and compared with the snapshot taken
at proposal time: the approval window is hours long, and approving "scale
from 3 to 5" means nothing once someone else has already changed it. Only
the fields the change depends on are compared (replicas and the absence
of an autoscaler; the container's resources; the autoscaler's identity and
bounds); any difference refuses the change and asks for a fresh proposal.

The write happens at most once. A retried attempt (the worker restarted,
or a read failed, after the patch landed) finds the cluster already at the
approved values and skips straight to verification instead of refusing or
writing again; a patch call that errored is judged the same way, by
re-reading the target. Once the patch is in, nothing below it may turn the
run into "nothing was executed": a verification that cannot finish is
reported as `verification_unavailable` with the change applied. The
outcome is otherwise verified from cluster state (tools/rollout.py), never
assumed from the API's acknowledgement.
"""

from datetime import datetime, timedelta, timezone

from common.capabilities_base import StepContext

from ..stepkit import finish
from ..tools import governance, persistence, quantities
from ..tools.write import ACTIONS

_VERIFY_MARGIN_SECONDS = 60
_DEFAULT_STEP_SECONDS = 900


def _require_confirmed(ctx: StepContext) -> dict:
    if not ctx.confirmed:
        raise RuntimeError(
            "apply_approved_change: refusing to change the cluster without an approved proposal. This step runs "
            "only from a confirmed propose_change pending_action (plan_task's confirmation bypass); it is never "
            "something a plan should schedule directly."
        )
    params = ctx.params
    missing = [k for k in ("id", "action", "namespace", "deployment", "snapshot") if not params.get(k)]
    if params.get("action") not in ACTIONS or missing:
        raise RuntimeError(
            f"apply_approved_change: the confirmed proposal is not one this agent made (action="
            f"{params.get('action')!r}, missing fields: {missing or 'none'}). Ask again and a fresh one is proposed."
        )
    return params


def _int(value):
    # A2A artifacts round-trip through protobuf Value, whose only number type
    # is a double: 3 comes back as 3.0.
    return int(value) if isinstance(value, float) and value == int(value) else value


def _drift_since_proposal(proposal: dict, dep: dict, autoscaler: dict | None) -> list[str]:
    snapshot = proposal["snapshot"]
    changes = []
    action = proposal["action"]
    current_fp = governance.autoscaler_fingerprint(autoscaler)
    proposed_fp = snapshot.get("autoscaler")
    if (current_fp is None) != (proposed_fp is None):
        changes.append(
            "an autoscaler now governs this Deployment" if current_fp else "the autoscaler governing it is gone"
        )
    elif current_fp and proposed_fp:
        for key in ("kind", "name", "min", "max", "generation"):
            if _int(current_fp.get(key)) != _int(proposed_fp.get(key)):
                changes.append(f"autoscaler {key} changed from {_int(proposed_fp.get(key))} to {current_fp.get(key)}")
    # Only the fields the approved change depends on. The Deployment's
    # `generation` is deliberately not compared: image automation upgrades
    # Helm releases several times an hour (observed on test, 2026-10-13),
    # each bumping it, so an approval hours later would almost always be
    # refused for a change that has nothing to do with it.
    if action == "scale_replicas" and _int(snapshot.get("deployment_replicas")) != dep.get("replicas"):
        changes.append(f"replicas changed from {_int(snapshot.get('deployment_replicas'))} to {dep.get('replicas')}")
    if action == "set_resources":
        container = proposal.get("container")
        now = next((c["resources"] for c in dep["template"]["containers"] if c["name"] == container), None)
        then = (snapshot.get("container_resources") or {}).get(container)
        if now != then:
            changes.append(f"container {container} resources changed since the proposal")
    return changes


def _same_quantity(resource: str, have, want) -> bool:
    try:
        return have is not None and quantities.to_units(resource, have) == quantities.to_units(resource, want)
    except (ValueError, TypeError):
        return False


def _at_target(action: dict, dep: dict, autoscaler: dict | None) -> bool:
    """Whether the cluster already holds exactly what `action` would write."""
    if action["action"] == "scale_replicas":
        return dep.get("replicas") == action["replicas"]
    if action["action"] == "set_autoscaler_bounds":
        fingerprint = governance.autoscaler_fingerprint(autoscaler) or {}
        if fingerprint.get("name") != (action.get("autoscaler") or {}).get("name"):
            return False
        return all(_int(fingerprint.get(b)) == action[b] for b in ("min", "max") if action.get(b) is not None)
    current = next((c["resources"] for c in dep["template"]["containers"] if c["name"] == action["container"]), None)
    if current is None:
        return False
    return all(
        _same_quantity(resource, (current.get(side) or {}).get(resource), value)
        for side, values in (action.get("resources") or {}).items()
        for resource, value in (values or {}).items()
    )


def _patch_action(proposal: dict) -> dict:
    action = {k: proposal.get(k) for k in ("action", "namespace", "deployment")}
    if proposal["action"] == "scale_replicas":
        action["replicas"] = _int(proposal["replicas"])
    elif proposal["action"] == "set_autoscaler_bounds":
        action["autoscaler"] = proposal["autoscaler"]
        for bound in ("min", "max"):
            if proposal.get(bound) is not None:
                action[bound] = _int(proposal[bound])
    else:
        action["container"] = proposal["container"]
        action["resources"] = proposal["resources"]
    return action


def _verify_target(action: dict) -> dict:
    if action["action"] == "scale_replicas":
        return {"kind": "replicas", "replicas": action["replicas"]}
    if action["action"] == "set_autoscaler_bounds":
        return {"kind": "autoscaler", "min": action.get("min"), "max": action.get("max")}
    return {"kind": "resources", "container": action["container"], "resources": action["resources"]}


async def run(ctx: StepContext) -> dict:
    proposal = _require_confirmed(ctx)
    namespace, name = proposal["namespace"], proposal["deployment"]
    action = _patch_action(proposal)

    dep = await ctx.tool("get_deployment")(namespace, name)
    autoscaler = await ctx.tool("find_autoscaler")(namespace, name)
    drift = _drift_since_proposal(proposal, dep, autoscaler)
    already = bool(drift) and _at_target(action, dep, autoscaler)
    if drift and not already:
        raise RuntimeError(
            "apply_approved_change: the cluster changed after this proposal was made ("
            + "; ".join(drift)
            + "). Nothing was changed. Ask again so a fresh proposal reflects the current state."
        )

    # Second precision on pod creation timestamps: back off a little so pods
    # created by this very patch are never mistaken for older ones.
    applied_at = (datetime.now(timezone.utc) - timedelta(seconds=2)).replace(microsecond=0)
    if already:
        # An earlier attempt of this step (or someone else) already put the approved values in place.
        applied = {"already_at_target": True, "note": "the cluster already held the approved values; nothing was written again"}
        applied_at = None
    else:
        try:
            applied = await ctx.tool("patch_workload")(action)
        except Exception as exc:  # noqa: BLE001 — the API may have applied it before the call failed
            dep = await ctx.tool("get_deployment")(namespace, name)
            autoscaler = await ctx.tool("find_autoscaler")(namespace, name)
            if not _at_target(action, dep, autoscaler):
                raise
            applied = {"applied_despite_error": f"{type(exc).__name__}: {exc}"}

    try:
        budget = (ctx.step_deadline_seconds or _DEFAULT_STEP_SECONDS) - _VERIFY_MARGIN_SECONDS
        max_wait = max(30, min((dep.get("progress_deadline_seconds") or 600) + 60, budget))
        verification = await ctx.tool("wait_rollout")(
            namespace, name, _verify_target(action), applied_at.isoformat() if applied_at else None, max_wait
        )
    except Exception as exc:  # noqa: BLE001 — the change is in; say so, whatever verification hit
        verification = {
            "status": "verification_unavailable",
            "detail": f"The change was applied, but its rollout could not be checked: {type(exc).__name__}: {exc}",
        }
    if action["action"] == "set_autoscaler_bounds" and verification.get("status") == "succeeded":
        verification = await _check_bounds_held(ctx, action, verification)

    try:
        changed = (
            {"labels": (autoscaler or {}).get("labels") or {}, "annotations": (autoscaler or {}).get("annotations") or {}}
            if action["action"] == "set_autoscaler_bounds"
            else {"labels": dep["labels"], "annotations": dep["annotations"]}
        )
        chain = await ctx.tool("get_flux_chain")(changed["labels"], changed["annotations"])
        hint = persistence.build_hint(
            chain, (proposal.get("persistence") or {}).get("changes") or [], outcome=verification.get("status")
        )
        drift_notice = governance.drift_notice(chain.get("ownership") or {}, chain)
    except Exception as exc:  # noqa: BLE001 — supporting information only
        hint = {"available": False, "note": f"The GitOps chain could not be read: {type(exc).__name__}: {exc}"}
        drift_notice = None

    action_result = {
        "proposal_id": proposal["id"],
        "action": proposal["action"],
        "action_name": proposal.get("action_name"),
        # Read by UIE (on-call Slack summary, incident record) as "this action ran". Every path that
        # reaches here has the change in place.
        "ran": True,
        "namespace": namespace,
        "deployment": name,
        "applied": applied,
        "requested": proposal.get("requested"),
        "clamped": proposal.get("clamped") or [],
        "status": verification.get("status"),
        "verification": verification,
        "drift_notice": drift_notice,
        "persistence": hint,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }
    return finish({"action_result": action_result})


async def _check_bounds_held(ctx: StepContext, action: dict, verification: dict) -> dict:
    """The autoscaler still holds the approved bounds after the wait (a GitOps reconcile may have reverted it)."""
    try:
        autoscaler = await ctx.tool("find_autoscaler")(action["namespace"], action["deployment"])
    except Exception as exc:  # noqa: BLE001
        return {**verification, "note": f"The autoscaler could not be re-read: {type(exc).__name__}: {exc}"}
    fingerprint = governance.autoscaler_fingerprint(autoscaler) or {}
    reverted = {
        bound: _int(fingerprint.get(bound))
        for bound in ("min", "max")
        if action.get(bound) is not None and _int(fingerprint.get(bound)) != action[bound]
    }
    if not reverted:
        return verification
    return {
        **verification,
        "status": "reverted",
        "detail": (
            f"The autoscaler's bounds were changed back after the change was applied (now {reverted}); "
            "something that manages it, most likely a GitOps reconcile, restored its own values."
        ),
    }
