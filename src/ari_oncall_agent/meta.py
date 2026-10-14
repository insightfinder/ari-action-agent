"""This agent kind's own descriptive metadata — see common/agent_meta.py
for what reads this and why it lives here instead of in common/."""

from a2a.types import AgentSkill

from common.agent_meta import AgentMeta
from common.models.plan import Plan, Step


def _ensure_propose_pr_by_default(plan: Plan, options: dict) -> list[Step]:
    """Proposing a PR is this agent's actual deliverable for any code
    change — always append "propose_pr" when "generate_change" ran and
    the planner's own step list didn't already include it, unless the
    request explicitly opted out (`options.skip_pr`, or the planner set
    `skip_publish_pr: true` because the request itself said not to open
    a PR).

    Prompt guidance alone isn't reliable enough for this "by default"
    policy — confirmed live (docs/MEMORY.md): a smaller/cheaper planner
    model inconsistently included this same step for the exact same kind
    of request across separate runs, purely from wording in its own
    system prompt. This makes it a deterministic guarantee instead of
    something the LLM has to remember every time — the same lesson as
    `StepContext.require_cloned_repo` for `clone_repo`, applied to a
    default-behavior policy instead of a hard technical prerequisite.

    It appends "propose_pr", never "publish_pr": since the human-in-the-
    loop gate was added, "publish_pr" is reachable only through
    `plan_task`'s confirmation bypass, so appending it to a planned run
    would schedule a step that refuses to run by design.
    """
    if options.get("skip_pr") or plan.skip_publish_pr:
        return plan.steps
    names = {step.capability for step in plan.steps}
    if "generate_change" in names and "propose_pr" not in names:
        return [*plan.steps, Step(capability="propose_pr")]
    return plan.steps

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
- **"clone_repo" MUST be the first step of every plan that includes \
"generate_change", "verify", or "propose_pr"** — those three all operate \
on the repo clone_repo produces (via --dir); without it there is no \
checked-out repo to investigate, edit, test, or publish from at all, and \
the step will fail. Never schedule "generate_change"/"verify"/"propose_pr" \
without "clone_repo" appearing earlier in the same step list.
- "generate_change" both investigates (root cause for a bug, scope for a \
feature/edit) and edits, in one step — there is no separate investigation \
step to choose or skip.
- "verify" is opt-in, NOT part of the default pipeline: only include it \
when the request explicitly asks for tests/verification to be run (e.g. \
"verify it works", "run the tests", "make sure it builds"). Omit it for \
an ordinary bug fix/feature/edit request — building/testing an arbitrary \
target repo is this pipeline's slowest, least predictable step, a real \
cost only worth paying when actually asked for.
- "propose_pr" is the OPPOSITE — opt-OUT, part of the default pipeline: \
whenever "generate_change" runs, the change gets proposed for approval by \
default, whether or not you include "propose_pr" yourself (a code change \
that's never shipped isn't useful). Only skip it when the request \
explicitly says not to open a PR / just wants to see the change / asks to \
skip the PR (e.g. "just show me the diff, don't open a PR", "no PR \
needed", "make the change but don't ship it yet") — in that case, and \
only that case, set the top-level `"skip_publish_pr": true` in your JSON \
response (a sibling of "steps" and "rationale"), not just leaving \
"propose_pr" out of the step list (that alone is treated as "you forgot," \
not "the user declined," and the change gets proposed anyway).
- **NEVER schedule "publish_pr".** It is not a step you can plan. A pull \
request is opened only after a person approves the proposal "propose_pr" \
produces, through a confirmation path that bypasses planning entirely; \
"publish_pr" refuses to run from a planned step and the request will \
fail. Schedule "propose_pr" and stop there — the approval, and the pull \
request that follows it, are not yours to arrange.
- "run_skill" runs one installed skill from the request's \
"available_skills" list on its own, with no repository. Plan it as the \
only step, without "clone_repo", when a listed skill clearly fits and the \
request asks for no change to a repository (e.g. checking an endpoint, \
looking something up, running an operational task a skill describes). Set \
its `skill` param to the exact listed name and its `instruction` param to \
what the user asked for. When the request does ask for a repository \
change, use the normal pipeline instead: the change agent can already use \
the same skills inside "generate_change". If no listed skill fits, or the \
request has no "available_skills" at all, never plan "run_skill".
- Set a step's `instruction` param with wording tailored to what's actually \
being asked (e.g. "implement this feature: ...", "make this specific edit: \
...", "fix this bug: ...") whenever "generate_change"'s generic default \
wording would fit poorly. Leave it unset when the generic default already \
fits fine.

Branches are dynamic per request, not fixed — read the prompt carefully for \
them: if it names a specific branch to work from ("check the repo in dev \
branch", "look at the release-2.3 branch"), set that as the clone_repo \
step's `ref` param. If it names a specific branch to open the PR against \
("raise a PR against staging", "target main"), set that as the propose_pr \
step's `base_ref` param — this can be a *different* branch than `ref` \
(work from dev, PR against staging is a normal, valid request). Leave \
either param unset when the prompt doesn't name that branch explicitly; \
the capability already has a sensible default.""",
    # A degraded run (the planner's own output was unusable — see
    # common/planner.py) has to do the safe *whole* job, not a fragment of
    # it. Ending at "propose_pr" is that whole job here: the change is
    # made, pushed and put in front of a person, and the one step that
    # reaches a reviewer's inbox as an open pull request still cannot
    # happen without them. This used to end at "publish_pr", which meant a
    # malformed planner response opened a pull request on the target
    # repository with no plan behind it at all — flagged as "worth its own
    # decision" in docs/MEMORY.md on 2026-10-05, and this is that decision.
    planner_fallback_steps=[
        {"capability": "clone_repo"},
        {"capability": "generate_change"},
        {"capability": "propose_pr"},
    ],
    respond_domain_note=(
        "the root cause or feature identified, what was changed and why, whether "
        "verification passed (only if it was actually run), and — when a change was "
        "proposed — the pull request's title, its target branch, the files it "
        "touches and the key excerpt of the diff, so the person approving can judge "
        "it from this message alone. A proposal is not an opened pull request: say "
        "plainly that it is waiting for approval, and give the pull request link "
        "only once one has actually been opened. The branch itself is already "
        "pushed by the time a proposal is reported, and its compare link is in the "
        "results — say so and include it, which is both true and the fastest way "
        "for a reviewer to see the whole change. Pushing a branch is not opening a "
        "pull request, so saying it is pushed does not contradict a FACT line "
        "stating that no pull request has been opened. When a skill ran (run_skill), "
        "report what it found or did using the concrete values from its output, name "
        "the skill, and say plainly if any of its commands were refused or failed"
    ),
    # Order matters: `a2a_server/executor.py` publishes these in this
    # order, and both its own `_find_pending_action` and MCP Service's
    # `pending_action_id()` resolve a confirmation against "the first
    # artifact carrying an id". `pending_action` is the only one of these
    # that has one, and listing it first keeps that true if another ever
    # gains one.
    artifacts={
        "pending_action": "propose_pr.pending_action",
        "root_cause": "generate_change.root_cause",
        "pull_request": "publish_pr.pr",
    },
    default_step_policy=_ensure_propose_pr_by_default,
    # Turns on the human-in-the-loop gate for this agent kind: tells
    # `plan_task`'s confirmation bypass which capability an approved
    # proposal resumes into, and makes `respond.execution_fact` compute
    # and state whether a pull request was actually opened instead of
    # leaving the response model to infer it from a proposal in the
    # results. That inference is exactly what went wrong on 2026-10-05.
    confirm_capability="publish_pr",
    direct_capabilities=("run_skill",),
)
