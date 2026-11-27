"""`set_startup_probe`: give one container of a Deployment more time to start (II-25740, 2026-11-27, user decision).

A container whose start takes longer than its probes allow (an index rebuild, a Java service's warm-up) is
restarted by the kubelet before it is up, forever. A startup probe holds the liveness and readiness probes off
until it passes once, so the fix is a startup probe with enough time; after the start nothing changes.

- A container without a startup probe gets one whose check is a copy of its liveness probe's (its readiness
  probe's when it has no liveness probe); with neither it is refused: this agent never makes up a check. The
  check is copied from the live Deployment when the change runs (`build_operations`), never carried in the
  proposal (an exec command can hold a password); the proposal holds its fingerprint, and the copy is refused
  when the check changed since.
- A container with a startup probe gets a larger failureThreshold (and periodSeconds when the request states
  one). Its check is left as it is.
- Only ever more time: the result must allow longer than the container is allowed now (its startup probe, or,
  without one, its liveness probe), at most `MAX_STARTUP_SECONDS`.
- The time comes from the request's own words: a duration ("10 minutes", "600s", "10分钟"), or failureThreshold
  and/or periodSeconds. Liveness and readiness probes are never changed: loosening them affects the running
  service (a hung pod restarted later, a broken pod serving longer).
"""

import math

from ..stepkit import refusal
from ..tools import ownership, policy, views
from .base import DeploymentAction, Operation
from .restart_rollout import rollout_blocked, rollout_effects
from .set_resources import pick_container

# The longest start a proposal allows: past it a container that hangs while starting is restarted only after
# that long. Never configurable here; a longer one is set where the workload is defined.
MAX_STARTUP_SECONDS = 3600  # one hour (user decision, 2026-11-27)
_PATH = ["f:spec", "f:template", "f:spec", "f:containers"]


def allowance(probe: dict | None) -> int | None:
    """Seconds a probe lets a container run, from its start, before it is restarted when the check never passes."""
    if not probe:
        return None
    return int(probe.get("initial_delay_seconds") or 0) + int(probe.get("failure_threshold") or 3) * int(probe.get("period_seconds") or 10)


def _fmt(seconds: int) -> str:
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60} min" + (f" ({seconds}s)" if seconds >= 120 else "")
    return f"{seconds}s"


def _probes_of(container: dict) -> dict:
    return {kind: container.get(f"{kind}_probe") for kind in ("startup", "liveness", "readiness")}


class SetStartupProbe(DeploymentAction):
    name = "set_startup_probe"
    prompt_line = (
        "give one container more time to start: add a startup probe (copying its liveness check) or give its "
        "existing startup probe more time. Only when the request asks about the startup probe or how long the "
        "container may take to start. Report `startup_time_quote` (the verbatim words stating how long it may take "
        "to start, with their unit), or `failure_threshold` / `period_seconds` with their quotes."
    )
    not_asked = (
        "The request does not itself ask about the startup probe of {kind} {namespace}/{name} or how long it may take "
        "to start, so nothing was proposed. Ask for it directly, e.g. \"give deployment {name} in namespace "
        "{namespace} 10 minutes to start\"."
    )

    def accepts(self, prompt):
        return policy.asks_startup_change(prompt)

    # ------------------------------------------------------------ plan

    def plan(self, ctx, state, change, env):
        dep = state["deployment"]
        containers = {c["name"]: c for c in dep["template"]["containers"]}
        name, refused = pick_container(change, containers)
        if refused:
            return None, refused
        blocked = rollout_blocked(dep, "this change")
        if blocked:
            return None, refusal(blocked)
        probes = _probes_of(containers[name])
        startup = probes["startup"]
        source = None if startup else ("liveness" if probes["liveness"] else "readiness" if probes["readiness"] else None)
        if not startup and source is None:
            return None, refusal(
                f"Container {name} of Deployment {dep['namespace']}/{dep['name']} has no liveness, readiness or startup "
                "probe: no probe restarts it, so more time to start would change nothing, and this agent never makes "
                "up a check for a new probe. Nothing was proposed."
            )
        if source and not probes[source].get("handler"):
            return None, refusal(f"The {source} probe of container {name} has no check to copy. Nothing was proposed.")
        if source and not probes[source].get("check_digest"):
            # Without its key the agent cannot tell at approval whether the check it would copy changed.
            return None, refusal(
                "This agent cannot add a startup probe here: the key it fingerprints the copied check with "
                "(TEMPORAL_PAYLOAD_ENCRYPTION_KEY) is not set. Nothing was proposed."
            )

        period, threshold, requested, refused = self._requested(ctx, change, startup, probes[source] if source else None)
        if refused:
            return None, refused
        new_allowance = (startup or {}).get("initial_delay_seconds", 0) * bool(startup) + threshold * period
        # What restarts it while starting today: its startup probe, or without one its liveness probe.
        now = allowance(startup) if startup else allowance(probes["liveness"])
        if new_allowance > MAX_STARTUP_SECONDS:
            return None, refusal(
                f"That would let container {name} take {_fmt(new_allowance)} to start, more than this agent sets "
                f"({_fmt(MAX_STARTUP_SECONDS)}): a container that hangs while starting would be restarted only after "
                "that long. Change it where the Deployment is defined if it really needs longer. Nothing was proposed."
            )
        if startup and (threshold < startup["failure_threshold"] or (now is not None and new_allowance <= now)):
            return None, refusal(
                f"Container {name} already has a startup probe allowing {_fmt(now)} "
                f"(failureThreshold {startup['failure_threshold']} x periodSeconds {startup['period_seconds']}"
                f"{', initialDelaySeconds ' + str(startup['initial_delay_seconds']) if startup['initial_delay_seconds'] else ''}). "
                "This agent only gives a container more time to start, never less. Nothing was proposed."
            )
        if not startup and now is not None and new_allowance <= now:
            return None, refusal(
                f"Its liveness probe already lets container {name} run {_fmt(now)} before restarting it; a startup probe "
                f"allowing {_fmt(new_allowance)} would not give it more time. Ask for longer. Nothing was proposed."
            )

        action = {
            "action": "set_startup_probe",
            "namespace": dep["namespace"],
            "deployment": dep["name"],
            "container": name,
            "failure_threshold": threshold,
            "period_seconds": period,
            # Adding one: whose check is copied (by fingerprint; the check itself never leaves the cluster).
            "copy_check_from": f"{source}Probe" if source else None,
            "check_digest": probes[source]["check_digest"] if source else None,
            "timeout_seconds": probes[source]["timeout_seconds"] if source else None,
        }
        before = (
            {"startupProbe": f"failureThreshold {startup['failure_threshold']} x periodSeconds {startup['period_seconds']} ({_fmt(now)})"}
            if startup
            else {"startupProbe": None}
        )
        after_text = f"failureThreshold {threshold} x periodSeconds {period} ({_fmt(new_allowance)})"
        after = {"startupProbe": after_text if startup else f"{probes[source]['handler']} check copied from the {source} probe, {after_text}"}
        risks, notes = rollout_effects(dep)
        risks.append(
            {
                "level": "info",
                "message": (
                    f"While starting, container {name} is restarted only after {_fmt(new_allowance)} without passing the "
                    f"check (now: {_fmt(now) if now is not None else 'never, by a probe'}); after it starts, its liveness "
                    "and readiness probes work as before."
                ),
            }
        )
        deadline = dep.get("progress_deadline_seconds") or 600
        if new_allowance > deadline:
            risks.append(
                {
                    "level": "medium",
                    "message": (
                        f"The Deployment's progressDeadlineSeconds ({deadline}s) is shorter than this start time: a rollout "
                        "whose pods take that long to start is marked as failed (ProgressDeadlineExceeded) although the "
                        "pods keep starting; the change's verification may then report rollout_failed."
                    ),
                }
            )
        rendered = [
            {
                "object": f"Deployment {dep['namespace']}/{dep['name']} container {name}",
                "field": f"startupProbe.{field}",
                "from": str((startup or {}).get(key)) if startup else None,
                "to": str(value),
            }
            for field, key, value in (("failureThreshold", "failure_threshold", threshold), ("periodSeconds", "period_seconds", period))
            if not startup or (startup.get(key) != value)
        ]
        if not startup:
            rendered.insert(
                0,
                {
                    "object": f"Deployment {dep['namespace']}/{dep['name']} container {name}",
                    "field": "startupProbe",
                    "from": None,
                    "to": f"the same check as its {source}Probe",
                },
            )
        facts = {
            "before": before,
            "after": after,
            "requested": requested,
            "scale_down": False,
            "high_replicas": False,
            "new_pods": 0,
            "rolls_pods": True,
            "roll_message": "Changing a probe triggers a rolling restart of every pod of this Deployment.",
            "risks": risks,
            "notes": notes,
            "changed_object": {"kind": "Deployment", "name": dep["name"], "labels": dep["labels"], "annotations": dep["annotations"]},
            "rendered_changes": rendered,
            "action_name": (
                f"{'Set' if startup else 'Add'} startupProbe on {dep['namespace']}/{dep['name']} ({name}): "
                f"up to {_fmt(new_allowance)} to start"
            ),
        }
        return (action, facts), None

    def _requested(self, ctx, change, startup, source):
        """`(periodSeconds, failureThreshold, requested, None)` from the request's own words, or a refusal."""
        prompt = ctx.prompt
        quote = change.get("startup_time_quote")
        seconds = policy.stated_duration(quote, prompt) if quote else None
        threshold_value = change.get("failure_threshold")
        period_value = change.get("period_seconds")
        threshold = period = None
        if threshold_value is not None:
            if not policy.quote_in_prompt(change.get("failure_threshold_quote"), prompt, threshold_value):
                return None, None, None, refusal("failureThreshold was not stated in the request with a value. Nothing was proposed.")
            threshold = int(threshold_value)
        if period_value is not None:
            if not policy.quote_in_prompt(change.get("period_seconds_quote"), prompt, period_value):
                return None, None, None, refusal("periodSeconds was not stated in the request with a value. Nothing was proposed.")
            period = int(period_value)
        if quote and seconds is None:
            return None, None, None, refusal(
                f"\"{quote}\" does not state one start time with a unit (e.g. \"10 minutes\", \"600s\"). Nothing was proposed."
            )
        if seconds is not None and threshold is not None:
            return None, None, None, refusal(
                "The request states both a start time and a failureThreshold; state one of them. Nothing was proposed."
            )
        if seconds is None and threshold is None and period is None:
            return None, None, None, refusal(
                "How long the container may take to start was not stated. Ask with a time, e.g. \"give it 10 minutes to "
                "start\", or a failureThreshold. Nothing was proposed."
            )
        if (threshold is not None and threshold < 1) or (period is not None and period < 1):
            return None, None, None, refusal("failureThreshold and periodSeconds must be at least 1. Nothing was proposed.")
        period = period or (startup or source or {}).get("period_seconds") or 10
        if seconds is not None:
            delay = (startup or {}).get("initial_delay_seconds") or 0
            threshold = max(1, math.ceil(max(seconds - delay, 1) / period))
        elif threshold is None:
            threshold = startup["failure_threshold"] if startup else max(1, int((source or {}).get("failure_threshold") or 3))
        requested = {
            k: v
            for k, v in (("start_time", quote if seconds is not None else None), ("failureThreshold", threshold_value), ("periodSeconds", period_value))
            if v is not None
        }
        return period, threshold, requested, None

    # ------------------------------------------------------------ write

    def operations(self, action):
        raise ValueError("set_startup_probe builds its operation from the live Deployment (build_operations)")

    async def build_operations(self, kube, action):
        probe = {"failureThreshold": int(action["failure_threshold"]), "periodSeconds": int(action["period_seconds"])}
        source = action.get("copy_check_from")
        if source:
            raw = await kube.get("Deployment", action["deployment"], action.get("namespace"))
            containers = ((((raw.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers")) or []
            container = next((c for c in containers if c.get("name") == action["container"]), None)
            copied = (container or {}).get(source)
            if container is None or views.probe_digest(copied) != action.get("check_digest"):
                raise RuntimeError(
                    f"the {source} check of container {action['container']} changed since the proposal, so it was not copied"
                )
            probe = {**views.probe_check(copied), "initialDelaySeconds": 0, "timeoutSeconds": (copied or {}).get("timeoutSeconds", 1), **probe}
        # Strategic merge: containers merge by name and the probe's fields merge one by one, so an existing
        # startup probe keeps its check and only its timing changes.
        return [
            Operation(
                verb="patch",
                kind="Deployment",
                resource="Deployment",
                name=action["deployment"],
                namespace=action.get("namespace"),
                body={"spec": {"template": {"spec": {"containers": [{"name": action["container"], "startupProbe": probe}]}}}},
                patch_type="strategic",
            )
        ]

    def summarize(self, op, raw, action):
        containers = (((raw.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
        target = next((c for c in containers if c.get("name") == action.get("container")), {})
        probe = views._probe(target.get("startupProbe"))
        return {**super().summarize(op, raw, action), "container": action.get("container"), "startup_probe": probe}

    def patch_action(self, proposal):
        return {
            **{k: proposal.get(k) for k in ("action", "namespace", "deployment", "container", "copy_check_from", "check_digest", "timeout_seconds")},
            "failure_threshold": int(proposal["failure_threshold"]),
            "period_seconds": int(proposal["period_seconds"]),
        }

    # ------------------------------------------------------------ approval-time checks

    def snapshot(self, state):
        snap = super().snapshot(state)
        dep = state["deployment"]
        snap["container_probes"] = {c["name"]: _probes_of(c) for c in dep["template"]["containers"]}
        return snap

    def drift(self, proposal, state):
        changes = super().drift(proposal, state)
        container = proposal.get("container")
        now = next((_probes_of(c) for c in state["deployment"]["template"]["containers"] if c["name"] == container), None)
        then = (proposal["snapshot"].get("container_probes") or {}).get(container)
        if now is None:
            changes.append(f"container {container} is no longer in the pod template")
        elif _comparable(now) != _comparable(then):
            changes.append(f"container {container} probes changed since the proposal")
        if state["deployment"].get("paused"):
            changes.append("the Deployment is paused now")
        return changes

    def at_target(self, action, state):
        container = next((c for c in state["deployment"]["template"]["containers"] if c["name"] == action["container"]), None)
        startup = (container or {}).get("startup_probe")
        if not startup:
            return False
        held = startup.get("failure_threshold") == int(action["failure_threshold"]) and startup.get("period_seconds") == int(action["period_seconds"])
        return held and (not action.get("check_digest") or startup.get("check_digest") == action["check_digest"])

    def written_fields(self, action, proposal):
        then = ((proposal["snapshot"].get("container_probes") or {}).get(action["container"]) or {}).get("startup") or {}
        base = [*_PATH, ownership.keyed(action["container"]), "f:startupProbe"]
        paths = [
            [*base, f"f:{field}"]
            for field, key in (("failureThreshold", "failure_threshold"), ("periodSeconds", "period_seconds"))
            if then.get(key) != int(action[key])
        ]
        return {"kind": "Deployment", "namespace": action.get("namespace"), "name": action["deployment"], "paths": paths}

    def verify_target(self, action):
        return {
            "kind": "startup_probe",
            "container": action["container"],
            "failure_threshold": int(action["failure_threshold"]),
            "period_seconds": int(action["period_seconds"]),
        }

    def base_tier(self, facts):
        return "medium"  # every probe change restarts pods


def _comparable(probes: dict | None) -> dict:
    """Probe views as compared at approval (A2A turns their numbers into doubles)."""
    out = {}
    for kind, probe in (probes or {}).items():
        out[kind] = {k: (int(v) if isinstance(v, float) and v == int(v) else v) for k, v in probe.items()} if probe else None
    return out
