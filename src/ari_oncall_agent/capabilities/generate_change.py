from common.opencode_runtime import merge_traces
from common.capabilities_base import StepContext

_TITLE_MARKER = "TITLE:"
_SUMMARY_MARKER = "SUMMARY:"
_MAX_TITLE_LEN = 72

_ENFORCEMENT_INSTRUCTION = (
    "Reply with ONLY two marked sections, nothing else: a line reading exactly 'TITLE:' "
    "followed on the same line by a short, complete PR title (one finished sentence or "
    "phrase, e.g. 'Add fetch_all_log_and_metric_projects tool' — never a fragment cut off "
    "mid-word or mid-sentence, under 70 characters), then a line reading exactly 'SUMMARY:' "
    "followed by a concise, PR-ready description of what you just changed and why — written "
    "for someone who did not watch you work. No narration, no 'let's do X' commentary, no "
    "code, no markdown headers, no tool calls."
)


def _extract_marked_line(text: str, marker: str) -> str | None:
    """Everything on the same line as the *last* occurrence of `marker`,
    stopping at the next newline or the start of a `SUMMARY:` section,
    whichever comes first. A title is always one short line — unlike
    `_extract_summary` below, this never has to guard against a
    multi-line blob."""
    idx = text.rfind(marker)
    if idx == -1:
        return None
    rest = text[idx + len(marker) :]
    end = len(rest)
    newline_idx = rest.find("\n")
    if newline_idx != -1:
        end = min(end, newline_idx)
    summary_idx = rest.find(_SUMMARY_MARKER)
    if summary_idx != -1:
        end = min(end, summary_idx)
    return rest[:end].strip()


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


def _safe_title(text: str, max_len: int = _MAX_TITLE_LEN) -> str:
    """Last-resort title derivation for the rare case opencode never
    produces a `TITLE:` line even after the enforcement follow-up —
    confirmed live as a real gap, not theoretical: a PR (insightfinder/
    insightfinder-mcp-server#55) got the title "ARI: Implemented a new
    tool, fetch_all_log_and_metric_projects, that aggreg-", a blind
    72-character slice of the *summary* text cut off mid-word, because
    there was no separate title at all — title and body were the exact
    same string at two different lengths. This takes the first sentence
    of whatever text is available and truncates at a word boundary (never
    mid-word) with an ellipsis if it's still too long. Never returns
    empty."""
    first_line = text.strip().split("\n", 1)[0].strip()
    for sep in (". ", "! ", "? "):
        idx = first_line.find(sep)
        if idx != -1:
            first_line = first_line[: idx + 1].strip()
            break
    if len(first_line) > max_len:
        truncated = first_line[:max_len].rsplit(" ", 1)[0].rstrip(".,;:")
        first_line = (truncated or first_line[:max_len]) + "…"
    return first_line or "automated change"


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
    # Fails fast and clearly if the planner's step list omitted clone_repo
    # — confirmed live as a real failure mode (a smaller/cheaper planner
    # model dropped it), which otherwise surfaces only as opencode's own
    # opaque crash against an empty, non-git --dir. See
    # StepContext.require_cloned_repo's own docstring.
    ctx.require_cloned_repo()

    opencode = ctx.tool("opencode")
    git_diff = ctx.tool("git_diff")
    untracked_files = ctx.tool("git_untracked_files")

    # Everything untracked *before* opencode runs, so a file it creates can
    # be told apart from one that was already lying around — which on an
    # `on_failure` loop back from `verify` includes whatever scratch files
    # that verify run left behind. See git_ops.intent_to_add for what goes
    # wrong without this.
    untracked_before = set(await untracked_files(ctx.repo_dir))

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
          "with two marked sections: a line reading exactly 'TITLE:' followed on the same "
          "line by a short, complete PR title (one finished sentence or phrase, e.g. 'Add "
          "fetch_all_log_and_metric_projects tool' — never a fragment cut off mid-word, "
          "under 70 characters), then a line reading exactly 'SUMMARY:' followed by a "
          "concise, PR-ready description of the root cause (if any) and what changed and "
          "why — written for someone who did not watch you work, with no step-by-step "
          "narration or 'let's do X' commentary. The title and summary must be different "
          "text — the title is a short heading, not a truncated copy of the summary."
    )
    result = await opencode(ctx, instruction)
    trace = result["trace"]
    summary = _extract_summary(result["text"])
    title = _extract_marked_line(result["text"], _TITLE_MARKER)
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
        summary = _extract_summary(followup["text"]) or followup["text"].strip() or result["text"].strip()
        title = title or _extract_marked_line(followup["text"], _TITLE_MARKER)
        trace = merge_traces(trace, followup["trace"])
    if not title:
        title = _safe_title(summary)
    # A brand-new file is untracked, and `git diff HEAD` cannot see one at
    # all; staging just the ones that appeared during this call makes both
    # the diff and the file list below complete without ever sweeping in a
    # scratch file. Only files, not the whole tree.
    created = sorted(set(await untracked_files(ctx.repo_dir)) - untracked_before)
    await ctx.tool("git_intent_to_add")(ctx.repo_dir, created)

    diff = await git_diff(ctx.repo_dir)
    changed_files = await ctx.tool("git_changed_files")(ctx.repo_dir)
    # An empty working tree means opencode edited nothing, whatever its
    # prose says. It is never a legitimate outcome here: this capability
    # is only ever scheduled to make a change, and on an `on_failure` loop
    # back from `verify` the earlier attempt's edits are still uncommitted
    # in this same tree, so a clean tree means *nothing at all* was done.
    #
    # Confirmed live (stg workflow 9b32a817, 2026-10-08): opencode made
    # exactly one tool call, which was invalid, finished in 20s with an
    # empty diff — and still returned success carrying a confident,
    # entirely fabricated summary ("now includes exponential backoff retry
    # logic, DNS resolution validation, circuit breaker pattern..."). The
    # empty-change guard lived only in propose_pr/publish_pr, which run
    # *after* `verify`, so `verify` was sent to build and test a change
    # that did not exist, timed out at its full 480s, looped back here,
    # and did it again — 45 minutes to discover that nothing happened in
    # the first 20 seconds.
    #
    # Raised, not reported as a soft `{"status": "failed"}`: this step has
    # no `on_failure` policy, so the raise is retried once by its own
    # retry policy (a second genuine attempt at the edit) and then ends
    # the run with this message as the stated reason, instead of handing
    # an empty change downstream.
    #
    # The message states only what was observed, and deliberately does not
    # accuse opencode of having claimed anything. Both of these end up
    # here and they are not the same thing:
    #   - it fabricated a change it never made (the 9b32a817 case above)
    #   - it correctly concluded nothing needed changing and said so
    #     ("No changes were required as per the instructions..." — seen
    #     live on stg, 2026-10-09)
    # An earlier wording opened with "opencode reported a change but left
    # the working tree unmodified", which is simply false in the second
    # case and tells whoever is reading the failure to go looking for a
    # lying model that isn't there. Telling the two apart would mean
    # trusting the model's own account of itself, which is the one thing
    # this guard exists because we cannot do. So: report the tree, the
    # tool calls and the account verbatim, and let a person judge.
    if not changed_files:
        raise RuntimeError(
            "generate_change: the working tree is unmodified — nothing was edited, so there is "
            f"no change to verify or publish. Tool calls: {trace.get('tool_call_counts') or 'none'}. "
            f"opencode's own account: {summary[:500]!r}"
        )
    return {
        "root_cause": {"summary": summary, "evidence": []},
        "title": title,
        "summary": summary,
        "diff": diff,
        "changed_files": changed_files,
        "trace": trace,
    }
