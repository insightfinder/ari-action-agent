from common import skills
from common.capabilities_base import StepContext

MAX_OUTPUT_CHARS = 20000
MAX_COMMANDS = 20


def _instruction(ctx: StepContext, skill: skills.Skill) -> str:
    request = (ctx.params.get("instruction") or "").strip() or ctx.prompt
    return (
        f"Use the {skill.name} skill for this request. Load it with the skill tool first and follow its "
        "instructions exactly.\n\n"
        f"Request: {request}\n\n"
        "There is no repository here and you cannot edit files. Run only the commands the skill tells you to "
        "run, using the absolute path of the skill's directory. If a command is refused or fails, say so plainly "
        "instead of working around it. Finish with a short report of what you found or did, keeping the concrete "
        "values (names, numbers, dates, identifiers) from the commands' output."
    )


def _commands(result: dict) -> list[dict]:
    commands = []
    for call in result.get("tool_calls") or []:
        if call.get("tool") != "bash":
            continue
        command = (call.get("input") or {}).get("command")
        if command:
            commands.append({"command": command, "status": call.get("status")})
    return commands[:MAX_COMMANDS]


async def run(ctx: StepContext) -> dict:
    installed = skills.available_skills()
    name = str(ctx.params.get("skill") or "").strip()
    listing = ", ".join(sorted(installed)) or "none"
    if not name:
        raise RuntimeError(f"run_skill: the step did not name a skill; installed skills: {listing}")
    skill = installed.get(name)
    if skill is None:
        raise RuntimeError(f"run_skill: no installed skill is named {name!r}; installed skills: {listing}")
    if name in skills.foreign_skill_names(None, skills.skill_homes()):
        raise RuntimeError(
            f"run_skill: another skill named {name!r} exists in a home directory on the worker, and opencode "
            "could load that copy instead, so the skill was not run"
        )
    result = await ctx.tool("opencode_skill")(ctx, skill, _instruction(ctx, skill))
    output = (result.get("text") or "").strip()
    return {
        "skill": name,
        "source": skill.source,
        "status": "completed" if output else "no_report",
        "output": output[:MAX_OUTPUT_CHARS] or "The skill finished without writing a report.",
        "commands": _commands(result),
        "trace": result.get("trace", {}),
    }
