"""This agent kind's own descriptive metadata — see common/agent_meta.py
for what reads this and why it lives here instead of in common/."""

from a2a.types import AgentSkill

from common.agent_meta import AgentMeta

META = AgentMeta(
    description=(
        "On-call agent for a Jenkins the platform cannot reach directly. Given a "
        "prompt plus Jenkins + LLM config, it resolves the target job/build, reads "
        "status/logs/test results, diagnoses failures, and proposes actions "
        "(triggering or stopping a build) that only run after the user confirms. "
        "Every request is independent and self-contained; no credentials are stored."
    ),
    skill=AgentSkill(
        id="jenkins_read_diagnose_act",
        name="Jenkins Read, Diagnose & Act",
        description=(
            "Given a question about a Jenkins job/build and a Jenkins config, resolves "
            "the target, reads whatever the question needs (status, console log, test "
            "results, queue, nodes), diagnoses a failure with evidence, and — for a "
            "requested change — proposes it and executes only once the user confirms."
        ),
        tags=["jenkins", "ci", "build", "diagnose"],
        examples=[
            "how's the checkout deployment doing? show me the log if it failed",
            "why has the nightly build been red since Tuesday?",
            "deploy checkout-service to staging",
        ],
    ),
    planner_description="on-call agent for a Jenkins the platform cannot reach directly",
    planner_domain_guidance="""Requests vary in kind — a read ("how's the checkout deploy doing?"), a \
diagnosis ("why has the nightly been red since Tuesday?"), or a change \
("deploy checkout-service to staging", "stop that build"). Don't default to \
treating every request as an action:
- "resolve_target" runs first whenever a specific job/build is named or \
implied — it turns the user's words into a concrete Jenkins object. Skip it \
only for a bare question needing no Jenkins lookup at all.
- Use "inspect" for status/parameters/history/queue questions. Use \
"diagnose" only for a failure — it reads the console log and test report \
and produces a cause with evidence, not just a pass/fail restatement.
- For any change (trigger or stop a build), the plan is resolve_target, \
propose_action — never execute_action directly. "propose_action" resolves \
the job and every parameter and stops; it never calls a mutating tool.
- Never schedule "execute_action". It is unreachable from a plan by \
design: it refuses to run unless the caller confirmed the proposal through \
the structured confirm flow, and that flow bypasses planning entirely (see \
orchestration/activities.py's plan_task). If the user appears to be \
confirming something, use "respond" to say the confirmation has to come \
back through that flow — scheduling execute_action yourself only produces \
a loud failure.""",
    planner_fallback_steps=[
        {"capability": "resolve_target"},
        {"capability": "inspect"},
    ],
    respond_domain_note=(
        "what was found (status/logs/test results), the diagnosed cause if there was a "
        "failure, and whether an action was proposed (say so plainly and ask for "
        "confirmation as a plain statement, not a dangling question), confirmed, or "
        "executed (and its result if so)"
    ),
    artifacts={
        "pending_action": "propose_action.pending_action",
        "action_result": "execute_action.result",
    },
    confirm_capability="execute_action",
    planner_skills=False,
)
