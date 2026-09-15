"""Shared context object passed to every capability's `run(ctx)`."""

from dataclasses import dataclass, field
from typing import Any, Callable

# Capabilities that talk to opencode. Used to figure out, from whatever
# subset of steps the planner actually chose, whether an opencode session
# already exists for this request (so we know to pass --continue).
_OPENCODE_CAPABILITIES = {"analyze", "generate_fix", "verify", "publish_pr"}


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

    def tool(self, name: str) -> Callable:
        return self.tools[name]

    @property
    def repo_dir(self) -> str:
        """Where clone_repo actually put the checkout — every opencode call
        runs with this as --dir. Falls back to the workspace root so a plan
        that skips clone_repo (a repo-less chat prompt) doesn't crash."""
        return self.results.get("clone_repo", {}).get("cloned_path", self.workspace)

    @property
    def opencode_session_started(self) -> bool:
        """Whether some earlier step in *this* run already opened an
        opencode session in repo_dir, regardless of which capabilities the
        planner actually chose or in what order."""
        return any(k in self.results for k in _OPENCODE_CAPABILITIES)
