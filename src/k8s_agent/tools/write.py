"""The only module that changes the cluster.

Registered as two tools in configs/k8s/tools.yaml:

- `dry_run_patch` — always server-side `dryRun=All`; there is no argument
  that turns it off. `propose_change` can reach only this one, so a
  proposal can validate a change against RBAC, admission and quota
  without being able to make it.
- `patch_workload` — the real write. Only `apply_approved_change` lists
  it, so no other capability can obtain it from the tool registry at all.

Both build the patch from the same `build_patch`, so what was dry-run is
exactly what is applied. Writes are never retried: a timed-out patch may
have been applied, and the caller re-reads state instead of guessing.
"""

from .client import PATCH_MERGE, PATCH_STRATEGIC, get_client, safe_error

ACTIONS = ("scale_replicas", "set_autoscaler_bounds", "set_resources")


def build_patch(action: dict) -> dict:
    """`{resource, name, namespace, body, patch_type, field}` for one action."""
    kind = action.get("action")
    namespace = action.get("namespace")
    if kind == "scale_replicas":
        return {
            "resource": "Deployment.Scale",
            "name": action["deployment"],
            "namespace": namespace,
            "body": {"spec": {"replicas": int(action["replicas"])}},
            "patch_type": "merge",
        }
    if kind == "set_autoscaler_bounds":
        target = action["autoscaler"]
        if target.get("kind") == "ScaledObject":
            fields = {"min": "minReplicaCount", "max": "maxReplicaCount"}
            resource = "ScaledObject"
        elif target.get("kind") == "HorizontalPodAutoscaler":
            fields = {"min": "minReplicas", "max": "maxReplicas"}
            resource = "HorizontalPodAutoscaler"
        else:
            raise ValueError(f"unsupported autoscaler kind {target.get('kind')!r}")
        spec = {fields[k]: int(action[k]) for k in ("min", "max") if action.get(k) is not None}
        if not spec:
            raise ValueError("set_autoscaler_bounds needs at least one of min/max")
        return {
            "resource": resource,
            "name": target["name"],
            "namespace": namespace,
            "body": {"spec": spec},
            "patch_type": "merge",
        }
    if kind == "set_resources":
        resources = {
            side: dict(values)
            for side, values in (action.get("resources") or {}).items()
            if side in ("requests", "limits") and values
        }
        if not resources:
            raise ValueError("set_resources needs at least one requests/limits value")
        # Strategic merge: containers merge by name and `resources` maps merge
        # key by key, so only the named fields of the named container change.
        return {
            "resource": "Deployment",
            "name": action["deployment"],
            "namespace": namespace,
            "body": {"spec": {"template": {"spec": {"containers": [{"name": action["container"], "resources": resources}]}}}},
            "patch_type": "strategic",
        }
    raise ValueError(f"unknown action {kind!r}")


_KINDS = {
    "Deployment.Scale": "Scale",
    "Deployment": "Deployment",
    "ScaledObject": "ScaledObject",
    "HorizontalPodAutoscaler": "HorizontalPodAutoscaler",
}
_PATCH_TYPES = {"merge": PATCH_MERGE, "strategic": PATCH_STRATEGIC}


def _summarize(resource: str, raw: dict, action: dict) -> dict:
    meta = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    out = {"resource": resource, "name": meta.get("name"), "generation": meta.get("generation")}
    if resource == "Deployment.Scale":
        out["replicas"] = spec.get("replicas")
    elif resource == "ScaledObject":
        out["min"], out["max"] = spec.get("minReplicaCount"), spec.get("maxReplicaCount")
    elif resource == "HorizontalPodAutoscaler":
        out["min"], out["max"] = spec.get("minReplicas"), spec.get("maxReplicas")
    elif resource == "Deployment":
        containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
        target = next((c for c in containers if c.get("name") == action.get("container")), {})
        res = target.get("resources") or {}
        out["container"] = action.get("container")
        out["resources"] = {
            side: {k: str(v) for k, v in (res.get(side) or {}).items() if k in ("cpu", "memory")}
            for side in ("requests", "limits")
        }
    return out


async def _patch(action: dict, dry_run: bool) -> dict:
    plan = build_patch(action)
    try:
        result = await get_client().patch(
            _KINDS[plan["resource"]],
            plan["name"],
            plan["namespace"],
            plan["body"],
            _PATCH_TYPES[plan["patch_type"]],
            dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as a clean, redacted error
        what = "dry-run" if dry_run else "patch"
        raise RuntimeError(f"{what} of {plan['resource']} {plan['namespace']}/{plan['name']} failed: {safe_error(exc)}") from None
    summary = _summarize(plan["resource"], result, action)
    summary["dry_run"] = dry_run
    return summary


async def dry_run_patch(action: dict) -> dict:
    return await _patch(action, dry_run=True)


async def patch_workload(action: dict) -> dict:
    return await _patch(action, dry_run=False)
