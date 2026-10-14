# Agent Skills

Skills are how you add capabilities to the ARI agent without touching its code. A skill is a folder of instructions and optional scripts in the open [Agent Skills format](https://agentskills.io/specification). The agent reads the instructions when a request needs them and runs the scripts the skill provides.

The engine-level extension points (`configs/<kind>/capabilities.yaml`, `tools.yaml`, `AgentMeta`) are for maintainers of this repository. Skills are for everyone else.

## What a skill looks like

```
my-skill/
  SKILL.md          required: frontmatter + instructions
  scripts/          optional: code the instructions tell the agent to run
  references/       optional: longer documents the agent reads when needed
  assets/           optional: templates and data files
```

`SKILL.md` starts with YAML frontmatter:

```markdown
---
name: my-skill
description: What the skill does and when to use it, in one or two sentences.
compatibility: Needs python3 and network access to the payment API.
metadata:
  author: your-team
  version: "1.0"
---

# My skill

Step-by-step instructions for the agent.
```

| Field | Required | Rules |
|---|---|---|
| `name` | yes | 1 to 64 characters: lowercase letters, digits and single hyphens, not starting or ending with a hyphen. Must equal the folder name. |
| `description` | yes | 1 to 1024 characters. This is all the agent sees until it decides to use the skill, so say what it does **and** when to use it, with the words people will actually type. |
| `license` | no | A license name or the name of a bundled license file. |
| `compatibility` | no | Up to 500 characters on what the skill needs: tools, credentials, network. |
| `metadata` | no | Text keys mapped to text values. |
| `allowed-tools` | no | Space-separated, e.g. `Bash(dig:*) Bash(jq:*)`. Here only `Bash(...)` entries count, and only for skills run on their own (see below). |

Other fields are ignored, with a warning from the validator. The file must be named exactly `SKILL.md`.

Check a skill before installing it:

```
ari skills validate path/to/my-skill
ari skills validate path/to/folder-of-skills
```

## How the agent uses skills

There are two paths, and the agent picks between them for each request.

**On its own, with no repository.** The planner sees every installed skill's name and description. When one clearly fits and the request is not a code change, it plans a single `run_skill` step. That step starts an opencode agent, `ari-skill`, which loads the skill and follows it. "Is the certificate of example.com still valid?" goes this way and uses `tls-certificate-check`.

**Inside a code change.** For a request that changes a repository, the change agent (`ari-change`, in `generate_change`) and the verify agent see the same skills and load one when it helps. The `changelog-entry` skill works this way: the agent fixes the code, then records the change in `CHANGELOG.md` the way the skill describes.

In both cases opencode shows the model only names and descriptions up front, and loads a skill's full instructions when the model decides to use it.

## Writing scripts

- When the agent loads a skill, opencode tells it the skill's directory. Write commands in `SKILL.md` as `python3 <this skill's directory>/scripts/tool.py ...` and ask for the absolute path. Do not ask the agent to `cd` first.
- Print results the agent can read reliably, ideally one JSON document, and say in `SKILL.md` what each field means.
- Do not ask for input interactively. Everything comes from arguments, files in the skill folder, or the environment.
- Never decide what is allowed from an environment variable. The agent can run `export NAME=value` before your script, and opencode does not check such statements against its command rules. Put operator settings that limit what a script may do in a file inside the skill folder, as `aws-security-group-allowlist` does with `config.json`. The agent cannot write there.
- Finish well inside two minutes, which is opencode's default limit for one command, and print partial results rather than nothing. `tls-certificate-check` stops at its `--deadline` and marks the rest `skipped`.
- Exit non-zero when the result needs attention, and say in `SKILL.md` that this is expected.
- Do any writing step only behind an explicit flag (`--apply`), and have the instructions run a read-only pass first. See `aws-security-group-allowlist`.
- Stick to the standard library, or declare what else is needed in `compatibility`. The worker image has `python3`, `git`, `bash`, `curl`, Node, Go, a JDK and Maven, nothing more.

## What a skill is allowed to do

| | Skill run on its own (`run_skill`) | Skill used inside a code change |
|---|---|---|
| Edit files | no | yes, as the change agent always could |
| Read files | only the skill's own folder; no file search or listing | as the change agent always could |
| Run commands | only the skill's own `scripts/`, run directly or with `python3`, `python`, `bash` or `sh`, plus the `Bash(...)` prefixes from `allowed-tools` | the change agent's usual commands |
| Fetch web pages | no | no |
| Other skills | no, only the one chosen | all installed skills |

For a skill run on its own, opencode checks each command in a `&&`, `;` or `|` chain separately. Commands containing a `/../` path step, a `$`, a backtick or `>` are always refused, so arguments cannot expand variables or redirect output. The agent runs in an empty directory, reads its configuration from outside that directory, and ignores any opencode project configuration around it. The agent's own service secrets (`AGENT_API_KEYS`, `TEMPORAL_API_KEY`, `TEMPORAL_PAYLOAD_ENCRYPTION_KEY`, and GitHub tokens) are removed from the environment its commands run in.

A skill's scripts still run as the worker process, with its network access and the rest of its environment, including any cloud credentials you give it. Review a skill like any other code before installing it.

## Where skills come from

| Source | Location | Who controls it |
|---|---|---|
| Bundled | `skills/` in this repository, `/app/skills` in the image | maintainers |
| Deployment | one folder per skill inside `SKILLS_DIR` (`SKILLS_DIR/<name>/SKILL.md`), default `/var/lib/ari-agent/skills` | the operator, e.g. a mounted volume |
| The target repository | `.opencode/skill(s)/`, `.claude/skills/`, `.agents/skills/` at its root | whoever can commit to that repository |
| Home directories on the worker | the same folders under the worker's home and under the build cache, which is `verify`'s home | whatever ran on the worker before |

Bundled and deployment skills are trusted. A deployment skill with the same name as a bundled one replaces it. They are re-read for every request, so a mounted volume can change without a restart. Each request gets its own copy, and nothing is written into the target repository.

Skills from the target repository and from home directories are **not** trusted by default. opencode is told not to look in any `.claude/skills` or `.agents/skills` folder, and the agents may load only our own skills by name. If a skill with the same name as one of ours exists in the repository or in a home directory, that name is blocked, because opencode does not reliably pick one copy over the other. For a code change the check runs when the repository is cloned; `run_skill` refuses to run a blocked skill at all.

Set `TRUST_REPO_SKILLS=true` to let the change and verify agents load repository skills as well. Names that exist both there and among ours stay blocked, and so does opencode's built-in `customize-opencode` skill.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `SKILLS_ENABLED` | `true` | `false` turns skills off everywhere: no `run_skill`, nothing shown to the planner, nothing loaded by the change agent. |
| `SKILLS_DIR` | `/var/lib/ari-agent/skills` | Folder of deployment skills. Missing is fine. |
| `BUNDLED_SKILLS_DIR` | empty | Overrides where the bundled skills are read from. Empty means `skills/` next to the code, then `./skills`, then `/app/skills`. |
| `TRUST_REPO_SKILLS` | `false` | Lets the change and verify agents use skills shipped inside the target repository, and lets opencode look in `.claude/skills` and `.agents/skills` folders again. |

A skill that fails validation is skipped and logged as `skill_rejected` with the reasons. A folder that cannot be read is skipped too. The agent keeps running either way.

The Helm chart does not mount anything at `SKILLS_DIR` yet. Until it does, add skills by building them into the image under `skills/`.

## Checking what opencode sees

Inside the worker image, with a folder of skills under `$XDG_CONFIG_HOME/opencode/skills`, `opencode debug skill` lists what opencode discovers without calling a model. It ignores permissions, and it always includes opencode's own built-in `customize-opencode` skill, which the agent's permissions block.
