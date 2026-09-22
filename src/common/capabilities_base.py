"""Shared context object passed to every capability's `run(ctx)`."""

from dataclasses import dataclass, field
from typing import Any, Callable

# Capabilities that talk to opencode, across every agent kind this process
# might be running as (see settings.agent_kind) — used to figure out, from
# whatever subset of steps the planner actually chose, whether an opencode
# session already exists for this request (so we know to pass --continue).
# A flat set rather than one per agent kind: capability names are already
# namespaced by what each agent kind's own capabilities.yaml registers, so
# there's no collision risk, and this stays a one-line check regardless of
# how many agent kinds share this codebase.
_OPENCODE_CAPABILITIES = {
    "generate_change", "verify", "publish_pr",  # oncall
    "resolve_target", "inspect", "diagnose", "propose_action", "execute_action",  # jenkins
}


@dataclass
class StepContext:
    request_id: str
    workspace: str  # sandbox working directory for this request
    prompt: str
    config: dict[str, Any]  # RequestConfig.model_dump()
    capability: str  # name of the capability currently running (this step)
    params: dict[str, Any]  # this step's planner-supplied params
    results: dict[str, Any]  # prior steps' outputs, keyed by capability name
    history: list[dict[str, Any]] = field(default_factory=list)  # chat turns, [] for task requests
    tools: dict[str, Callable] = field(default_factory=dict)
    # True only when this step came from orchestration/activities.py's
    # plan_task bypass branch (a resolved, structured confirm_pending_action
    # from a2a_server/executor.py) — never from the LLM planner, which only
    # ever populates `params`, not this field. execute_action.py's
    # trigger_build gate checks this, not anything in `params`, so it can't
    # be spoofed by a planner's own JSON output.
    confirmed: bool = False

    def tool(self, name: str) -> Callable:
        return self.tools[name]

    @property
    def repo_dir(self) -> str:
        """Where clone_repo actually put the checkout — every opencode call
        runs with this as --dir. Falls back to the workspace root so a plan
        that skips clone_repo (a repo-less chat prompt, or an agent kind
        that has no clone_repo capability at all — e.g. Jenkins, which just
        needs *some* directory for opencode's per-request config/session,
        never a git checkout) doesn't crash."""
        return self.results.get("clone_repo", {}).get("cloned_path", self.workspace)

    def require_cloned_repo(self) -> str:
        """`repo_dir`, but for the capabilities that only ever make sense
        against an actual git checkout (generate_change/verify/publish_pr) —
        raises clearly if clone_repo never actually ran, instead of silently
        falling back to the empty workspace root the way `repo_dir` itself
        does for capabilities that are fine without one (see its own
        docstring).

        Confirmed live as a real failure mode, not theoretical: a planner
        using a smaller/cheaper model produced a step list that dropped
        clone_repo entirely while still including generate_change. With no
        checked-out repo, opencode ran against an empty, non-git directory
        and failed with an opaque `Unexpected server error. Check server
        logs for details.` — no indication anywhere of the actual, simple
        cause. This turns that into an immediate, actionable error instead.
        """
        if "clone_repo" not in self.results:
            raise RuntimeError(
                f"{self.capability}: clone_repo must run before this step, but the planner's step list "
                f"didn't include it — there is no cloned repo to work with."
            )
        return self.repo_dir

    @property
    def opencode_session_started(self) -> bool:
        """Whether some earlier step in *this* run already opened an
        opencode session in repo_dir, regardless of which capabilities the
        planner actually chose or in what order."""
        return any(k in self.results for k in _OPENCODE_CAPABILITIES)
