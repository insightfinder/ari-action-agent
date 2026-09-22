from common.opencode_runtime import merge_traces
from common.capabilities_base import StepContext

_SUMMARY_MARKER = "SUMMARY:"

_ENFORCEMENT_INSTRUCTION = (
    "Reply with ONLY a concise, PR-ready description of what you just changed and why — "
    "written for someone who did not watch you work. No narration, no 'let's do X' "
    "commentary, no code, no markdown headers, no tool calls. Just the summary text."
)


def _extract_summary(text: str) -> str | None:
    """opencode's raw turn output (`result["text"]`) is every text chunk
    from the whole agentic session concatenated together — step-by-step
    narration ("Now let's make the edits...", "Let me fix gofmt
    alignment...") interleaved with tool calls, not a clean final summary.
    Confirmed live as a real bug, not theoretical: that raw blob was used
    directly as this capability's `summary`, which `publish_pr.py` both
    truncates to 72 chars for the PR *title* and uses verbatim as the PR
    *body* — so a real PR (insightfinder/InsightAgent#983) ended up titled
    "ARI: Now let's make the edits. First `models.go`:\\nLet me fix gofmt
    alignment " (the first 72 characters of the narration, not the actual
    summary that appeared later in the same blob).

    Fix: the instruction asks the agent to end with a line reading exactly
    "SUMMARY:" followed by a clean, PR-ready description with no
    narration. This extracts everything after the *last* such marker (the
    final one, in case the word appears earlier in the agent's own
    narration). Returns None — not the full text — when the marker never
    appears, since the full text is exactly the raw-narration blob this
    exists to avoid; `run()` below treats a `None` as "issue an
    enforcement follow-up call" rather than silently accepting the
    narration as PR-facing text. Confirmed live this matters: even with
    the marker instruction in place, opencode doesn't always comply (seen
    on a real run against `positron-agent`) — a plain-text fallback here
    would have quietly reintroduced the exact bug being fixed."""
    idx = text.rfind(_SUMMARY_MARKER)
    if idx == -1:
        return None
    return text[idx + len(_SUMMARY_MARKER) :].strip()


async def run(ctx: StepContext) -> dict:
    """Investigates and edits in one opencode call — there used to be a
    separate `analyze` capability (read-only, its own opencode agent/
    session) that ran first and handed this one a summary. Merged because
    that split cost a whole second opencode subprocess + LLM session start
    for every request that was always going to edit anyway: opencode's own
    agentic loop already reads/greps as needed before editing within one
    turn, same as a competent engineer investigates-as-they-go rather than
    working in two rigid phases. `root_cause` in the returned dict (this
    agent kind's own `root_cause` artifact, see meta.py) reuses the same
    SUMMARY-marked text as the change summary — for a single-pass agent
    "what I found and did" is one coherent narrative, not two.
    """
    opencode = ctx.tool("opencode")
    git_diff = ctx.tool("git_diff")

    prior_failure = ctx.results.get("verify", {}).get("failure")

    instruction = ctx.params.get("instruction") or (
        f"Investigate this repository as needed, then make the following change — a "
        f"bug fix, a feature, a refactor, or a specific edit, whatever it actually "
        f"asks for.\n\n"
        f"Original request: {ctx.prompt}\n\n"
        + (f"The previous attempt failed verification with:\n{prior_failure}\n\n" if prior_failure else "")
        + "This may be a large monorepo with many unrelated modules — if the request "
          "names a specific module/component/directory, scope your search to it first "
          "and only look elsewhere if something it directly imports/references genuinely "
          "requires it. Search efficiently: consolidate related terms into one regex "
          "search rather than issuing several overlapping single-term searches, and don't "
          "re-search for something you've already found. If this is a bug report or "
          "incident, briefly identify the root cause before editing; if the request "
          "already names the exact fields/files/behavior to change, there's nothing to "
          "investigate first — go straight to editing. "
          "Make the smallest change that fully satisfies the request. End your response "
          "with a line reading exactly 'SUMMARY:' followed by a concise, PR-ready "
          "description of the root cause (if any) and what changed and why — written "
          "for someone who did not watch you work, with no step-by-step narration or "
          "'let's do X' commentary."
    )
    result = await opencode(ctx, instruction)
    trace = result["trace"]
    summary = _extract_summary(result["text"])
    if summary is None:
        # opencode didn't end with the required marker — ask again,
        # narrowly, rather than falling back to the raw narration (which
        # is exactly the bug this whole mechanism exists to avoid).
        # `continue_session=True` is required here, not optional: from
        # `ctx.results`'s point of view this capability hasn't "started" a
        # session yet (its own entry is only set by the orchestrator after
        # `run()` returns), so the automatic --continue detection would
        # otherwise start a second, context-less session that has no idea
        # what was just changed.
        followup = await opencode(ctx, _ENFORCEMENT_INSTRUCTION, continue_session=True)
        summary = followup["text"].strip() or result["text"].strip()
        trace = merge_traces(trace, followup["trace"])
    diff = await git_diff(ctx.repo_dir)
    changed_files = await ctx.tool("git_changed_files")(ctx.repo_dir)
    return {
        "root_cause": {"summary": summary, "evidence": []},
        "summary": summary,
        "diff": diff,
        "changed_files": changed_files,
        "trace": trace,
    }
