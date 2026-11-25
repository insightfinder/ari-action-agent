"""The only module that changes the cluster.

Registered as two tools in configs/k8s/tools.yaml:

- `dry_run_patch` — always server-side `dryRun=All`; there is no argument
  that turns it off. `propose_change` can reach only this one, so a
  proposal can validate a change against RBAC, admission and quota
  without being able to make it.
- `patch_workload` — the real write. Only `apply_approved_change` lists
  it, so no other capability can obtain it from the tool registry at all.

Both run the same `Operation`s, built by the action's spec
(`k8s_agent/actions`), so what was dry-run is exactly what is applied.
Writes are never retried: a timed-out write may have been applied, and the
caller re-reads state instead of guessing. (The tool names predate verbs
other than patch; they are kept because the capability registry and its
tests pin which capability holds which.)
"""

from .. import actions
from ..actions.base import Operation
from .client import PATCH_JSON, PATCH_MERGE, PATCH_STRATEGIC, get_client, safe_error

ACTIONS = actions.ACTIONS
_PATCH_TYPES = {"merge": PATCH_MERGE, "strategic": PATCH_STRATEGIC, "json": PATCH_JSON}


def build_operations(action: dict) -> list[Operation]:
    return actions.get(action.get("action")).operations(action)


async def _execute(kube, op: Operation, dry_run: bool) -> dict:
    if op.verb == "patch":
        return await kube.patch(op.kind, op.name, op.namespace, op.body, _PATCH_TYPES[op.patch_type], dry_run, op.subresource)
    if op.verb == "delete":
        return await kube.delete(op.kind, op.name, op.namespace, dry_run, preconditions=op.preconditions)
    if op.verb == "create":
        return await kube.create(op.kind, op.namespace, op.body, dry_run, op.name, op.subresource)
    raise ValueError(f"unknown verb {op.verb!r}")


async def _run(action: dict, dry_run: bool) -> dict:
    spec = actions.get(action.get("action"))
    kube = get_client()
    if spec.executes_itself:
        # A change that is a process rather than a list of calls (a drain: cordon, then evict whatever is on the
        # node, retrying what a disruption budget holds back) runs its own operations through `_execute`.
        try:
            return await spec.execute(action, kube, dry_run, _execute)
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced as a clean, redacted error
            what = "dry-run" if dry_run else "run"
            raise RuntimeError(f"{what} of {action.get('action')} failed: {safe_error(exc)}") from None
    try:
        operations = await spec.build_operations(action, kube)
    except Exception as exc:  # noqa: BLE001 — a read the operations need (a rollback's ReplicaSet) failed
        raise RuntimeError(f"could not prepare {action.get('action')}: {safe_error(exc)}") from None
    summaries = []
    for op in operations:
        try:
            raw = await _execute(kube, op, dry_run)
        except Exception as exc:  # noqa: BLE001 — surfaced as a clean, redacted error
            what = "dry-run" if dry_run else op.verb
            where = f"{op.namespace}/{op.name}" if op.namespace else op.name
            raise RuntimeError(f"{what} of {op.resource} {where} failed: {safe_error(exc)}") from None
        summary = spec.summarize(op, raw or {}, action)
        summary["dry_run"] = dry_run
        summaries.append(summary)
    # A single-operation change keeps the flat summary every caller and artifact has always seen.
    return summaries[0] if len(summaries) == 1 else {"operations": summaries, "dry_run": dry_run}


async def dry_run_patch(action: dict) -> dict:
    return await _run(action, dry_run=True)


async def patch_workload(action: dict) -> dict:
    return await _run(action, dry_run=False)
