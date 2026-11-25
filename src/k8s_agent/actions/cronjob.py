"""CronJob actions: `suspend_cronjob` and `resume_cronjob` set `spec.suspend` (as `kubectl patch` would).

Each is proposed only when the request itself asks for it (`policy.asks_suspend_cronjob`,
`asks_resume_cronjob`), and only for a CronJob `find_workload` resolved.

- Suspend stops future scheduled runs; Jobs already running carry on. Resume lets the schedule fire again, and
  without `startingDeadlineSeconds` the CronJob controller starts a run at once for the most recent schedule
  missed while suspended (Kubernetes documents this); the proposal says so. Both change a field a chart or
  manifest usually sets, so a Helm upgrade or a reconcile puts the manifest's value back: the drift notice says
  how soon and the persistence hint names the field to change in Git.
- Running a CronJob now (creating a Job from it) is not offered: the user decided on 2026-11-25 that the agent
  does not do it (see docs/K8S_AGENT_INTERFACE.md).
"""

from ..stepkit import refusal
from ..tools import policy
from .base import ActionSpec, Operation

class _CronJobAction(ActionSpec):
    workload_kinds = ("CronJob",)
    target_kinds = ("CronJob",)

    async def load_state(self, ctx, target):
        return {"cronjob": await ctx.tool("get_workload")("CronJob", target["namespace"], target["name"])}

    def target(self, action):
        return {"kind": "CronJob", "namespace": action.get("namespace"), "name": action.get("cronjob")}

    def changed_object(self, action, state):
        cj = state["cronjob"]
        return {"labels": cj.get("labels") or {}, "annotations": cj.get("annotations") or {}}

    def base_tier(self, facts):
        return "medium"


class _SetSuspend(_CronJobAction):
    suspend: bool = True
    rbac_rule = 'patch on "cronjobs" (batch group; the oncallari chart grants it from 0.3.16)'

    def plan(self, ctx, state, change, env):
        cj = state["cronjob"]
        ref = f"CronJob {cj['namespace']}/{cj['name']}"
        if bool(cj.get("suspend")) == self.suspend:
            return None, refusal(f"{ref} is {'already' if self.suspend else 'not'} suspended; nothing to change.")
        status = cj.get("status") or {}
        active = status.get("active_jobs") or []
        risks: list[dict] = []
        notes = [f"Schedule {cj.get('schedule')!r}" + (f" ({cj['time_zone']})" if cj.get("time_zone") else "") + "."]
        if self.suspend:
            risks.append(
                {
                    "level": "medium",
                    "message": "Every scheduled run is skipped until the CronJob is resumed; nothing resumes it "
                    "automatically.",
                }
            )
            if active:
                notes.append(f"Runs already active carry on; a suspend does not stop them: {', '.join(active)}.")
            action_name = f"Suspend {ref}"
        else:
            deadline = cj.get("starting_deadline_seconds")
            if deadline is None:
                risks.append(
                    {
                        "level": "medium",
                        "message": "The CronJob has no startingDeadlineSeconds: if a scheduled time passed while it "
                        "was suspended, resuming starts one run at once for the most recent one.",
                    }
                )
            else:
                notes.append(
                    f"startingDeadlineSeconds is {deadline}: a run missed while suspended starts at once only if its "
                    f"time was less than {deadline}s ago."
                )
            action_name = f"Resume {ref}"
        action = {"action": self.name, "namespace": cj["namespace"], "cronjob": cj["name"], "suspend": self.suspend}
        facts = {
            "before": {"suspend": bool(cj.get("suspend"))},
            "after": {"suspend": self.suspend},
            "scale_down": False,
            "high_replicas": False,
            "new_pods": 0,
            "restart": False,
            "risks": risks,
            "notes": notes,
            "changed_object": {"kind": "CronJob", "name": cj["name"], "labels": cj.get("labels") or {}, "annotations": cj.get("annotations") or {}},
            "rendered_changes": [
                {"object": ref, "field": "suspend", "from": bool(cj.get("suspend")), "to": self.suspend}
            ],
            "action_name": action_name,
        }
        return (action, facts), None

    def operations(self, action):
        return [
            Operation(
                verb="patch",
                kind="CronJob",
                resource="CronJob",
                name=action["cronjob"],
                namespace=action.get("namespace"),
                body={"spec": {"suspend": bool(action["suspend"])}},
                patch_type="merge",
            )
        ]

    def summarize(self, op, raw, action):
        return {**super().summarize(op, raw, action), "suspend": bool((raw.get("spec") or {}).get("suspend"))}

    def patch_action(self, proposal):
        return {**{k: proposal.get(k) for k in ("action", "namespace", "cronjob")}, "suspend": bool(proposal.get("suspend"))}

    def snapshot(self, state):
        cj = state["cronjob"]
        return {"uid": cj.get("uid"), "suspend": bool(cj.get("suspend"))}

    def drift(self, proposal, state):
        cj, snapshot = state["cronjob"], proposal["snapshot"]
        changes = []
        if snapshot.get("uid") and cj.get("uid") != snapshot.get("uid"):
            changes.append(f"CronJob {cj['name']} was deleted and created again")
        if bool(cj.get("suspend")) != bool(snapshot.get("suspend")):
            changes.append(f"it is {'suspended' if cj.get('suspend') else 'not suspended'} now")
        return changes

    def at_target(self, action, state):
        return bool(state["cronjob"].get("suspend")) == bool(action["suspend"])

    def verify_target(self, action):
        return {"kind": "suspend", "suspend": bool(action["suspend"])}

    async def verify(self, ctx, action, proposal, before, applied_at, max_wait):
        return await ctx.tool("wait_cronjob_suspend")(action["namespace"], action["cronjob"], bool(action["suspend"]), max_wait)


class SuspendCronJob(_SetSuspend):
    name = "suspend_cronjob"
    suspend = True
    prompt_line = "suspend the CronJob: no scheduled run starts until it is resumed. Only when the request asks for it."
    not_asked = (
        "The request does not itself ask to suspend the CronJob, so nothing was proposed. To have it proposed, ask "
        "for it directly, e.g. \"suspend cronjob {name} in namespace {namespace}\"."
    )

    def accepts(self, prompt):
        return policy.asks_suspend_cronjob(prompt)


class ResumeCronJob(_SetSuspend):
    name = "resume_cronjob"
    suspend = False
    prompt_line = "resume a suspended CronJob so its schedule runs again. Only when the request asks for it."
    not_asked = (
        "The request does not itself ask to resume the CronJob, so nothing was proposed. To have it proposed, ask "
        "for it directly, e.g. \"resume cronjob {name} in namespace {namespace}\"."
    )

    def accepts(self, prompt):
        return policy.asks_resume_cronjob(prompt)
