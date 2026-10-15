"""Small helpers shared by this kind's capabilities."""

from common.capabilities_base import StepContext

from .tools.redact import redact_obj


def options(ctx: StepContext) -> dict:
    return ctx.config.get("options") or {}


def resolved_target(ctx: StepContext) -> dict:
    """The workload `find_workload` resolved, overridden by explicit params."""
    resolution = (ctx.results.get("find_workload") or {}).get("resolution") or {}
    target = {
        "scope": resolution.get("scope"),
        "namespace": resolution.get("namespace"),
        "name": resolution.get("name"),
    }
    for key in ("namespace", "name"):
        if ctx.params.get(key):
            target[key] = ctx.params[key]
    if target.get("name"):
        target["scope"] = "deployment"
    return target


def refusal(reason: str, **details) -> dict:
    """A deliberate "no" — the run completes and nothing is proposed or
    executed; the response step explains why from this structure."""
    return finish({"refusal": {"reason": reason, **details}})


def finish(result: dict) -> dict:
    """Every capability result passes through redaction before it becomes a
    step result, so `respond`, the A2A artifacts and Temporal history only
    ever see scrubbed data (see tools/redact.py)."""
    return redact_obj(result)
