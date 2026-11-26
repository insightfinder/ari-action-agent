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
from 3 to 5" means nothing once someone else has already changed it. Each
action compares the fields its change depends on (`drift` in its spec:
the object's uid, replicas, a container's resources, the autoscaler's
identity and bounds, a rollout since a proposed restart, a pod's
readiness, a PVC's size and StorageClass, ...); any difference refuses
the change and asks for a fresh proposal. A proposal older than
`options.proposal_max_age_hours` (default 24) is refused the same way:
its risks and capacity estimate were read from a cluster that has moved on.

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

from .. import actions
from ..actions.base import artifact_int as _int, target_of
from ..stepkit import finish, options
from ..tools import governance, persistence, protection

_VERIFY_MARGIN_SECONDS = 60
_DEFAULT_STEP_SECONDS = 900
DEFAULT_PROPOSAL_MAX_AGE_HOURS = 24.0


def _require_confirmed(ctx: StepContext) -> dict:
    if not ctx.confirmed:
        raise RuntimeError(
            "apply_approved_change: refusing to change the cluster without an approved proposal. This step runs "
            "only from a confirmed propose_change pending_action (plan_task's confirmation bypass); it is never "
            "something a plan should schedule directly."
        )
    params = ctx.params
    target = target_of(params)
    missing = [k for k in ("id", "action", "snapshot") if not params.get(k)]
    missing += [f"target {k}" for k in ("kind", "namespace", "name") if not target.get(k)]
    if params.get("action") not in actions.ACTIONS or missing:
        raise RuntimeError(
            f"apply_approved_change: the confirmed proposal is not one this agent made (action="
            f"{params.get('action')!r}, missing fields: {missing or 'none'}). Ask again and a fresh one is proposed."
        )
    return params


def _proposed_at(proposal: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(proposal.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _max_age_hours(ctx: StepContext) -> float | None:
    """`options.proposal_max_age_hours`; 0 or less turns the limit off."""
    raw = options(ctx).get("proposal_max_age_hours")
    try:
        hours = DEFAULT_PROPOSAL_MAX_AGE_HOURS if raw is None or isinstance(raw, bool) else float(raw)
    except (TypeError, ValueError):
        hours = DEFAULT_PROPOSAL_MAX_AGE_HOURS
    return hours if hours > 0 else None


def _too_old(ctx: StepContext, proposal: dict) -> str | None:
    hours = _max_age_hours(ctx)
    made = _proposed_at(proposal)
    if hours is None or made is None:
        return None
    if made.tzinfo is None:
        made = made.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - made
    if age <= timedelta(hours=hours):
        return None
    return (
        f"apply_approved_change: this proposal was made {age.total_seconds() / 3600:.1f} hours ago, more than the "
        f"{hours:g} hours an approval stays valid; its risks and estimates describe the cluster as it was then. "
        "Nothing was changed. Ask again so a fresh proposal reflects the current state."
    )


async def run(ctx: StepContext) -> dict:
    proposal = _require_confirmed(ctx)
    spec = actions.get(proposal["action"])
    target = target_of(proposal)
    namespace = target["namespace"]
    # A proposal made before a kind of target was withdrawn (2026-11-26: StatefulSets, DaemonSets) is not applied.
    if target["kind"] not in spec.target_kinds:
        raise RuntimeError(
            f"apply_approved_change: this agent no longer changes a {target['kind']} with {proposal['action']}. "
            "Nothing was changed."
        )
    protected = protection.why_protected(namespace, proposal.get("deployment") or proposal.get("workload") or target["name"])
    if protected:
        raise RuntimeError(f"apply_approved_change: {protected}")
    action = spec.patch_action(proposal)

    state = await spec.load_state(ctx, target)
    dep = state.get("deployment") or {}
    drift = spec.drift(proposal, state)
    already = bool(drift) and spec.at_target(action, state)
    if drift and not already:
        raise RuntimeError(
            "apply_approved_change: the cluster changed after this proposal was made ("
            + "; ".join(drift)
            + "). Nothing was changed. Ask again so a fresh proposal reflects the current state."
        )

    # Too old to write, but not too old to verify what an earlier attempt of this step already wrote.
    stale = None if already else _too_old(ctx, proposal)
    if stale:
        raise RuntimeError(stale)

    # Second precision on pod creation timestamps: back off a little so pods
    # created by this very patch are never mistaken for older ones.
    applied_at = (datetime.now(timezone.utc) - timedelta(seconds=2)).replace(microsecond=0)
    # The state this attempt's write starts from (None when an earlier attempt already wrote it): a verifier
    # that expects one container restart counts it from here.
    before_write = None if already else state
    if already:
        # An earlier attempt of this step (or someone else) already put the approved values in place.
        applied = {
            "already_at_target": True,
            "note": "the cluster already held the approved values; nothing was written again",
            # What differs from the proposal's snapshot: this step's own earlier write, or someone else's change.
            "changed_since_proposal": drift,
        }
        # When it was written is unknown here, but not before the proposal: pods from before it (often the
        # very crash loop this change fixes) must not be judged as the change's own.
        applied_at = _proposed_at(proposal)
    else:
        try:
            applied = await ctx.tool("patch_workload")(action)
        except Exception as exc:  # noqa: BLE001 — the API may have applied it before the call failed
            state = await spec.load_state(ctx, target)
            dep = state.get("deployment") or dep
            if not spec.at_target(action, state):
                raise
            applied = {"applied_despite_error": f"{type(exc).__name__}: {exc}"}

    try:
        budget = (ctx.step_deadline_seconds or _DEFAULT_STEP_SECONDS) - _VERIFY_MARGIN_SECONDS
        max_wait = max(30, min((dep.get("progress_deadline_seconds") or 600) + 60, budget))
        verification = await spec.verify(
            ctx, action, proposal, before_write, applied_at.isoformat() if applied_at else None, max_wait
        )
    except Exception as exc:  # noqa: BLE001 — the change is in; say so, whatever verification hit
        verification = {
            "status": "verification_unavailable",
            "detail": f"The change was applied, but its rollout could not be checked: {type(exc).__name__}: {exc}",
        }
    if action["action"] == "set_autoscaler_bounds" and verification.get("status") == "succeeded":
        verification = await _check_bounds_held(ctx, action, verification)

    try:
        changed = spec.changed_object(action, state)
        chain = await ctx.tool("get_flux_chain")(changed["labels"], changed["annotations"])
        proposed = proposal.get("persistence") or {}
        hint = persistence.build_hint(
            chain,
            proposed.get("changes") or [],
            outcome=verification.get("status"),
            nothing_to_persist=proposed.get("reason") if proposed.get("needed") is False else None,
        )
        drift_notice = spec.lifetime_notice(chain.get("ownership") or {}, chain)
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
        "deployment": proposal.get("deployment"),
        # Added in II-25740 (docs/K8S_AGENT_INTERFACE.md): the object changed, for any kind, and a marker a
        # consumer can key on instead of a list of action names.
        "target": target,
        "kubernetes_change": True,
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
