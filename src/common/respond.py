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
- If the pipeline gave up needing a human (status is not "completed"), say \
so plainly and explain what's blocking it, using the failure information \
provided.
- State things definitively. NEVER end with a question or an offer \
conditioned on a reply ("which should I do next?", "let me know if you'd \
like...") — this response is delivered once and there is no guarantee \
anyone is waiting to answer a question in it. If there's genuine follow-up \
work worth mentioning, phrase it as a plain recommendation, not a question.
- Be concise but complete. Never invent facts not present in what's given \
to you below.
"""


async def run(ctx: StepContext) -> dict:
    llm_complete = ctx.tool("llm_complete")
    meta = get_agent_meta()
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        description=meta.planner_description,
        domain_note=meta.respond_domain_note,
    )

    history_text = "\n".join(f"{m['role']}: {m['content']}" for m in ctx.history)
    step_results = {k: v for k, v in ctx.results.items() if k != "respond"}

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
        + "Write the final response now."
    )

    message = await llm_complete(ctx.config["llm"], system_prompt, user_prompt)
    return {"message": message}
