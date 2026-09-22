"""This agent kind's own descriptive metadata — see common/agent_meta.py
for what reads this and why it lives here instead of in common/."""

from a2a.types import AgentSkill

from common.agent_meta import AgentMeta

META = AgentMeta(
    description=(
        "On-call agent that takes an incident/prompt plus repo + LLM "
        "config, investigates and fixes the problem in an isolated "
        "sandbox using opencode, and opens a pull request — optionally "
        "verifying the fix first when asked to. Every request is "
        "independent and self-contained."
    ),
    skill=AgentSkill(
        id="code_analysis_fix",
        name="Code Analysis & Fix",
        description=(
            "Given a prompt describing a bug/incident (e.g. a stack "
            "trace) and a repo config, clone the repo, investigate and "
            "fix it, and open a PR — running tests/verification first "
            "only when the request asks for it. Also answers plain "
            "questions directly when no repo action is needed."
        ),
        tags=["code", "incident", "pull-request", "opencode"],
        examples=[
            "null pointer exception in the code <stacktrace>, please analyse and create a fix",
            "check the repo in the dev branch, fix the memory leak, and raise a PR against staging",
            "same issue as before but run the tests first before opening the PR",
        ],
    ),
    planner_description="on-call agent that fixes bugs and ships code changes",
    planner_domain_guidance="""Requests vary in kind — a bug fix, a new feature, a specific edit/refactor \
("remove field X, keep it empty", "rename Y to Z", "add a param to this \
endpoint"), or a bare question. Don't default to treating every repo \
request as a bug fix:
- "generate_change" both investigates (root cause for a bug, scope for a \
feature/edit) and edits, in one step — there is no separate investigation \
step to choose or skip.
- "verify" is opt-in, NOT part of the default pipeline: only include it \
when the request explicitly asks for tests/verification to be run (e.g. \
"verify it works", "run the tests", "make sure it builds"). Omit it for \
an ordinary bug fix/feature/edit request — building/testing an arbitrary \
target repo is this pipeline's slowest, least predictable step, a real \
cost only worth paying when actually asked for.
- Set a step's `instruction` param with wording tailored to what's actually \
being asked (e.g. "implement this feature: ...", "make this specific edit: \
...", "fix this bug: ...") whenever "generate_change"'s generic default \
wording would fit poorly. Leave it unset when the generic default already \
fits fine.

Branches are dynamic per request, not fixed — read the prompt carefully for \
them: if it names a specific branch to work from ("check the repo in dev \
branch", "look at the release-2.3 branch"), set that as the clone_repo \
step's `ref` param. If it names a specific branch to open the PR against \
("raise a PR against staging", "target main"), set that as the publish_pr \
step's `base_ref` param — this can be a *different* branch than `ref` \
(work from dev, PR against staging is a normal, valid request). Leave \
either param unset when the prompt doesn't name that branch explicitly; \
the capability already has a sensible default.""",
    planner_fallback_steps=[
        {"capability": "clone_repo"},
        {"capability": "generate_change"},
        {"capability": "publish_pr"},
    ],
    respond_domain_note=(
        "the root cause or feature identified, what was changed and why, whether "
        "verification passed (only if it was actually run), and the pull request "
        "link if one was opened"
    ),
    artifacts={
        "root_cause": "generate_change.root_cause",
        "pull_request": "publish_pr.pr",
    },
)
