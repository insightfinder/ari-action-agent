"""The final, always-run step, shared by every agent kind. Never chosen by
the planner (`internal: true` in each agent's own capabilities.yaml) —
`orchestration/workflow.py` calls it directly after the planner-chosen
steps finish (or the pipeline gives up), regardless of what happened. This
is what turns raw step outputs into an actual answer:

- A prompt that needed no work at all (empty `results`) gets a real,
  direct answer to what was asked — not a canned "no action needed."
- A full pipeline run gets a synthesized summary of what was found and
  done, not a templated one-liner.
- A pipeline that gave up (`fail_needs_human`) gets a plain explanation of
  what's blocking it.

Every request is delivered the same way from this agent's point of view —
a caller may block for the result, poll for it, or register a webhook
(all handled by `a2a_server/executor.py`, this capability has no idea
which) — so the response always states things definitively and never ends
with a question awaiting a reply: this agent doesn't implement a paused
"waiting on you" task state in this pass (see `a2a_server/executor.py`),
so a dangling question would have nowhere to go. A caller who wants to
follow up is always free to send another message in the same context;
this capability just never assumes one is coming.

Uses the request's own LLM directly (`llm_complete`), not opencode — this
is pure text synthesis over already-collected results, not an external
operation. Mechanically identical for every agent kind; only
`agent_meta.respond_domain_note` (what "synthesize what was found and
done" actually means for this agent kind) varies.
"""

import json

from common.agent_meta import get_agent_meta
from common.capabilities_base import StepContext

_SYSTEM_PROMPT_TEMPLATE = """You are the final response-writing stage of an autonomous \
{description}. Given the user's message (and, for a multi-turn conversation, \
prior history) and whatever automated steps were run, write the response the \
user will actually see.

Rules:
- If no steps were run (results are empty), just answer the user's message \
directly and helpfully, like a knowledgeable engineer would — it may be a \
plain question with nothing to look up or do.
- If steps were run, synthesize what was found and done: {domain_note}. Do \
not just restate raw tool output verbatim — write a concise, well-organized \
summary a human can act on immediately.
- If the pipeline gave up needing a human (status is "failed"), say so \
plainly and explain what's blocking it, using the failure information \
provided.
- If the pipeline is deliberately pausing for your confirmation before \
proceeding (status is "needs_approval"), this is NOT a failure — say \
plainly what you're proposing to do (from the steps run so far) and that \
you're waiting for confirmation before acting, as a plain statement per \
the rule above, never phrased as giving up or as something having gone \
wrong.
- State things definitively. NEVER end with a question or an offer \
conditioned on a reply ("which should I do next?", "let me know if you'd \
like...") — this response is delivered once and there is no guarantee \
anyone is waiting to answer a question in it. If there's genuine follow-up \
work worth mentioning, phrase it as a plain recommendation, not a question.
- Be concise but complete. Never invent facts not present in what's given \
to you below.
- A line below beginning "FACT:" is established by the pipeline itself, \
not by a model. It overrides anything you infer from the step results, \
and you must not contradict it. In particular, never state or imply that \
an action was carried out, triggered, started, stopped or applied unless \
a FACT line says it ran: proposing an action is not performing it, and a \
reader acts on the difference.
"""


def _skill_run_proposal_exists(meta, step_results: dict) -> bool:
    ref = (getattr(meta, "artifacts", None) or {}).get("pending_action")
    if not ref or "." not in ref:
        return False
    step, _, field = ref.partition(".")
    result = step_results.get(step)
    return isinstance(result, dict) and bool(result.get(field))


def execution_fact(meta, step_results: dict) -> str:
    """Whether this agent kind's one mutating capability actually ran,
    stated as a fact rather than left for the response-writing model to
    work out from the step results.

    It does not reliably work it out. On 2026-10-05 a run whose plan was
    only `resolve_target` produced "Action executed: Trigger Buggy
    Deployment" - the step was never scheduled, nothing was sent to the
    action server, and this prompt's own "never invent facts" rule did
    not stop it (docs/MEMORY.md). An absence is the hard thing to notice:
    the model sees a proposal among the results and narrates the obvious
    next thing. So the absence is computed here and said out loud.

    Empty for an agent kind with no `confirm_capability` (oncall), which
    has no single step that means "something outside was changed".
    """
    capability = meta.confirm_capability
    direct = [name for name in getattr(meta, "direct_capabilities", ()) if step_results.get(name)]
    direct_fact = ""
    if direct:
        names = ", ".join(f"`{name}`" for name in direct)
        direct_fact = (
            f"FACT: the {names} step ran in this request, and the commands it ran may have read from "
            "or changed external systems. Report what its output says was checked or changed, exactly "
            "as the output shows it, and nothing beyond that."
        )
    if not capability:
        return direct_fact

    result = step_results.get(capability)
    ran = bool(result) and not (isinstance(result, dict) and result.get("status") == "failed")
    if ran:
        return (
            f"FACT: the `{capability}` step ran in this request. Report what it returned, "
            "including whether the target system actually accepted the action."
        )
    if direct_fact:
        proposal = "There IS a" if _skill_run_proposal_exists(meta, step_results) else "There is no"
        return (
            f"{direct_fact} FACT: the `{capability}` step did not run, so no pull request was opened. "
            f"{proposal} proposal awaiting confirmation in this request."
        )
    return (
        f"FACT: NOTHING WAS EXECUTED. The `{capability}` step did not run in this request, "
        "so no action reached any external system. Anything in the results is a proposal "
        "awaiting confirmation, never a completed action. Do not write that an action was "
        "executed, triggered, run, started, stopped or applied."
    )


async def run(ctx: StepContext) -> dict:
    llm_complete = ctx.tool("llm_complete")
    meta = get_agent_meta()
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        description=meta.planner_description,
        domain_note=meta.respond_domain_note,
    )

    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in ctx.history)
    step_results = {k: v for k, v in ctx.results.items() if k != "respond"}
    fact = execution_fact(meta, step_results)

    user_prompt = (
        f"User's message: {ctx.prompt}\n\n"
        + (f"Conversation history so far:\n{history_text}\n\n" if history_text else "")
        + f"Pipeline status: {ctx.params.get('pipeline_status', 'completed')}\n"
        + (f"Failure detail: {ctx.params['failure_reason']}\n" if ctx.params.get("failure_reason") else "")
        + "\n"
        + (
            f"Steps run and their results (JSON):\n{json.dumps(step_results, indent=2)}\n\n"
            if step_results
            else "No steps were run for this request — this is a direct question.\n\n"
        )
        + (f"{fact}\n\n" if fact else "")
        + "Write the final response now."
    )

    message = await llm_complete(ctx.config["llm"], system_prompt, user_prompt)
    return {"message": message}
