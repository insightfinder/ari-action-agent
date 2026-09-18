from ..tools.opencode_cli import merge_traces
from .base import StepContext

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
    opencode = ctx.tool("opencode")
    git_diff = ctx.tool("git_diff")

    analysis = ctx.results.get("analyze", {}).get("root_cause", {}).get("summary")
    prior_failure = ctx.results.get("verify", {}).get("failure")

    instruction = ctx.params.get("instruction") or (
        f"Make the following change to this repository — a bug fix, a feature, a "
        f"refactor, or a specific edit, whatever it actually asks for.\n\n"
        f"Original request: {ctx.prompt}\n\n"
        + (f"Prior analysis: {analysis}\n\n" if analysis else "")
        + (f"The previous attempt failed verification with:\n{prior_failure}\n\n" if prior_failure else "")
        + "This may be a large monorepo with many unrelated modules — if the request "
          "names a specific module/component/directory, scope your search to it first "
          "and only look elsewhere if something it directly imports/references genuinely "
          "requires it. Search efficiently: consolidate related terms into one regex "
          "search rather than issuing several overlapping single-term searches, and don't "
          "re-search for something you've already found. "
          "Make the smallest change that fully satisfies the request. End your response "
          "with a line reading exactly 'SUMMARY:' followed by a concise, PR-ready "
          "description of what changed and why — written for someone who did not watch "
          "you work, with no step-by-step narration or 'let's do X' commentary."
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
    return {"summary": summary, "diff": diff, "changed_files": changed_files, "trace": trace}
