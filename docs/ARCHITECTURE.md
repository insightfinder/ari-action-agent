# Repo structure, and how to add a new agent kind

This repo hosts a small shared framework plus any number of independent
"agent kinds" built on top of it — today the on-call agent
(`ari_oncall_agent/`) and the Kubernetes agent (`k8s_agent/`); the Jenkins
agent (`jenkins_agent/`, on its own `jenkins-agent` branch) is deprecated
and kept only as a reference. This doc maps the repo and gives the
concrete, step-by-step checklist for adding another. `docs/design.md` §0 covers the same split conceptually; this
doc is the practical "what files do I actually touch" companion,
written against the real second agent kind that was actually built this
way — every step below points at a real file, not a hypothetical one.

## 1. The repo map

```
src/
  common/              agent-kind-agnostic — never imports from an agent package
    a2a_server/        the real A2A protocol server (executor, agent card, auth)
    orchestration/     the Temporal workflow + activities + worker entrypoint
    agent_meta.py      the registry of agent kinds + the AgentMeta contract
    capabilities_base.py / capabilities_registry.py / tools_registry.py
                        the declarative capability/tool loading mechanism
    planner.py         LLM-driven step planning, generic over agent kind
    respond.py         the shared final-answer capability, generic over agent kind
    settings.py        deployment-level config (Settings.agent_kind selects everything)
    sandbox.py, llm/, opencode_runtime.py, models/
                        other agent-kind-agnostic mechanics

  ari_oncall_agent/    one agent kind's own code
    capabilities/      clone_repo, analyze, generate_change, verify, publish_pr
    tools/             git_ops.py, repo_cache.py, opencode_cli.py (GitHub MCP wiring)
    schemas.py         documents this kind's own request config shape
    meta.py            this kind's AgentMeta instance

  k8s_agent/           the Kubernetes agent kind — same shape, no opencode
    capabilities/      find_workload, check_health, propose_change, apply_approved_change
    tools/             client.py (in-cluster ServiceAccount), read.py, write.py, rollout.py,
                        views.py + redact.py (what may leave the cluster), pure rule modules
    schemas.py
    meta.py

  jenkins_agent/       DEPRECATED, only on the `jenkins-agent` branch

  api/                 thin deployable entrypoints only, zero agent-specific logic
    main.py             `uvicorn api.main:app` — the A2A/FastAPI process
    worker.py           `python -m api.worker` — the Temporal worker process

configs/
  oncall/capabilities.yaml, tools.yaml     declarative registries for that kind
  k8s/capabilities.yaml, tools.yaml        same, for the Kubernetes kind

Dockerfile.api          shared API image — used by every agent kind's API deployment
Dockerfile.oncall       the on-call agent's worker image (git + go/java/maven + opencode)
Dockerfile.k8s          the Kubernetes agent's worker image (no shell tooling, non-root)

docs/
  design.md                        current-state architecture reference
  DEVELOPER_GUIDE.md                how to call these agents over A2A
  INPUT_REQUIRED_INTEGRATION.md     the TASK_STATE_INPUT_REQUIRED integration contract
  MEMORY.md                         the decision/verification/bug-history log
  ARCHITECTURE.md                   this doc
  examples/                         runnable sample A2A clients, one per agent kind

tests/                  one flat directory, no per-agent-kind subfolders;
                        filenames say which kind they cover
                        (test_jenkins_capabilities.py, etc.) — common/ tests
                        don't have a kind in the name

.github/workflows/docker-build-push.yml
                        builds + pushes the API image and each kind's worker image on push to main/staging/test
```

**The Helm chart lives in a separate repo** —
`insightfinder/charts`, `charts/oncallari/` — not here. It has its own
per-agent-kind structure mirroring this one: `templates/ari-oncall-agent/`
and `templates/ari-jenkins-agent/`, each a self-contained set of
api-deployment/worker-deployment/secret/service/ingress manifests, plus
one shared `_helpers.tpl` and `values.yaml`.

## 2. The one rule that keeps this scaling cleanly

**`common/` never knows about a specific agent kind. Everything
kind-specific lives in that kind's own package, exposed to `common/`
through exactly one declarative contract: `AgentMeta`
(`common/agent_meta.py`).** When `common/` code needs to vary by kind —
the Agent Card's description/skill, the planner's domain guidance, what
counts as an artifact, which capability resolves a confirmed
human-in-the-loop action — it reads a field off `get_agent_meta()`,
never an `if agent_kind == "jenkins"` branch. If you find yourself
writing that branch anywhere under `common/`, that's the signal the
thing you're branching on belongs in `AgentMeta` instead.

The other standing rule, learned the hard way (`docs/MEMORY.md`,
2026-09-18): **`orchestration/workflow.py` must never import anything
that pulls in `a2a.types`** (that's what `agent_meta.py` does, for
`AgentSkill`) — Temporal's default sandboxed workflow runner cannot
tolerate it, and the failure mode is the worker refusing to even start.
Anything that needs `get_agent_meta()`, `get_capability()`, or any other
registry lookup belongs in an **activity**
(`orchestration/activities.py`), never in `workflow.py` itself — see
`plan_task` and `resolve_artifacts` there for the two existing examples
of this pattern.

## 3. Checklist: adding a new agent kind

Say you're adding one called `slack` (illustrative name — swap in
whatever it actually is). In the order you'd naturally hit them:

### 3.1 The package: `src/slack_agent/`

- `capabilities/<name>.py` per capability — each just
  `async def run(ctx: StepContext) -> dict`, reading `ctx.config`/
  `ctx.params`/`ctx.results` and calling whatever it needs via
  `ctx.tool("name")`. Look at `k8s_agent/capabilities/check_health.py`
  for a read-only one, `k8s_agent/capabilities/apply_approved_change.py`
  (or oncall's `publish_pr.py`) for one that gates a mutating action
  behind confirmation.
- `tools/<name>.py` — thin wrappers around whatever this agent kind
  actually talks to. If it's opencode-driven, copy the shape of
  `jenkins_agent/tools/opencode_cli.py`: a `CAPABILITY_AGENTS` map
  (capability name → scoped opencode agent name), `_AGENT_DEFS` (each
  opencode agent's `permission`/`tools` block — least privilege,
  `mode: "primary"` is required or `--agent` silently falls back to
  opencode's default agent), `write_config()`, and a `run_opencode()`
  wrapper delegating to `common/opencode_runtime.py`'s agent-kind-agnostic
  subprocess/tracing mechanics. If it's a direct API integration instead
  (no opencode), look at `k8s_agent/tools/` — typed client calls, every
  read reduced to whitelisted views and every result redacted before it
  leaves the capability. (The deprecated Jenkins kind on the
  `jenkins-agent` branch has the opencode-driven example.)
- `schemas.py` — a documentation-only pydantic model of what this kind
  expects in its config `DataPart` (see `k8s_agent/schemas.py`). Nothing at runtime
  actually validates against it today — capabilities just read
  `ctx.config["slack"]` as a plain dict — but it's the one place a
  caller-facing shape is written down.
- `meta.py` — the `AgentMeta` instance. This is the contract with
  `common/`; every field is required to make the agent function, most
  are load-bearing for something specific:
  - `description`, `skill` — the real Agent Card content.
  - `planner_description`, `planner_domain_guidance`,
    `planner_fallback_steps` — steer the shared planner
    (`common/planner.py`) without it knowing anything about this kind.
  - `respond_domain_note` — one clause slotted into the shared `respond`
    capability's system prompt.
  - `artifacts` — `{"artifact_name": "capability.result_key"}` — which
    capability results become named A2A artifacts.
  - `confirm_capability` — only set this if the kind has a
    human-in-the-loop confirm gate like Jenkins' `trigger_build` (see
    `docs/design.md` §5b); otherwise leave it `None` (the default).
  - `planner_skills` — set `False` if the kind has no `run_skill`
    capability, so its planner is not shown the installed Agent Skills
    (the k8s kind does this). Defaults to `True`.

### 3.2 Register the package: `common/agent_meta.py`

One line in `_AGENT_PACKAGES`:
```python
_AGENT_PACKAGES = {
    "oncall": "ari_oncall_agent",
    "jenkins": "jenkins_agent",
    "slack": "slack_agent",
}
```
This is genuinely the only place `common/` names a specific package.

### 3.3 The registries: `configs/slack/capabilities.yaml`, `configs/slack/tools.yaml`

Same shape as `configs/jenkins/{capabilities,tools}.yaml` — each
capability entry names its module, description (goes into the planner's
catalog), which tools it may call, and its retry/`on_failure` policy.
Every agent kind also registers the shared `common.respond` capability
with `internal: true` (never chosen by the planner, always run directly
by `workflow.py`).

### 3.4 `pyproject.toml`

Add `"src/slack_agent"` to `[tool.hatch.build.targets.wheel]`'s
`packages` list.

### 3.5 A worker Dockerfile: `Dockerfile.slack`

Only if this kind needs a different toolchain than the two that already
exist (`Dockerfile.oncall`'s full git/go/java/maven/opencode stack,
`Dockerfile.jenkins`'s much lighter opencode-only one). No new API
Dockerfile — `Dockerfile.api` is already agent-kind-agnostic and shared
by every kind's API deployment.

### 3.6 CI: `.github/workflows/docker-build-push.yml`

One new entry in the `matrix.include` list:
```yaml
- dockerfile: Dockerfile.slack
  image: docker.io/insightfinderinc/ari-slack-agent-worker
```

### 3.7 The Helm chart (separate repo: `insightfinder/charts`)

- A new `templates/ari-slack-agent/` directory, copied from
  `templates/ari-jenkins-agent/` and renamed throughout — api-deployment,
  worker-deployment, secret, service, ingress. Drop the repo-cache PVC
  unless this kind clones repos like the on-call agent does.
- New `_helpers.tpl` entries with a distinct suffix
  (`insightfinder.ariSlackAgent*`, `-slack-api`/`-slack-worker`/
  `-slack-secret`) — never reuse another kind's suffix, that's what
  keeps multiple kinds coexisting under one release without name
  collisions.
- A new `ariSlackAgent` block in `values.yaml`: its own `api.image`
  (can point at the same shared `ari-agent-api` image every other kind
  uses) and `worker.image` (its own), its own Temporal namespace and
  task queue (pick names without "fix" in them — see `docs/MEMORY.md`,
  2026-09-18, for why that convention exists), its own `agentName`/
  `agentPublicUrl`.

### 3.8 Tests

At minimum, mirroring what exists for Jenkins: a `tests/test_slack_
capabilities.py` covering each capability's `run()` against a `StepContext`
built by hand (see `test_jenkins_capabilities.py`'s `_ctx()` helper), and
adding `"slack"` to `tests/test_agent_meta.py`'s
`test_fallback_steps_only_name_capabilities_that_kind_actually_has` —
its loop is a hardcoded tuple (`for kind in ("oncall", "jenkins"):`), not
auto-discovered from `_AGENT_PACKAGES`, so a new kind needs adding there
by hand or its fallback-steps-vs-registry check silently never runs for
it.

### 3.9 Docs

- `docs/design.md` §0's table gets a new row.
- `docs/DEVELOPER_GUIDE.md` gets a new numbered section for this kind's
  own config `DataPart` shape, same pattern as §9's Jenkins section.
- A new `docs/examples/slack_sample_client.py`, mirroring the existing
  two.

## 4. What you should almost never need to touch

If adding a new agent kind requires editing `common/orchestration/
workflow.py`, `common/planner.py`'s prompt-building logic (not its
per-kind `domain_guidance` *content*, which lives in `meta.py`), or
`common/a2a_server/executor.py`'s core dispatch — stop and reconsider.
Every one of those is already generic over `AgentMeta`; a new kind
should only ever need a new package, a new `configs/<kind>/`, and one
line in `_AGENT_PACKAGES`. The one narrow exception so far: the
`trigger_build` confirm-gate mechanism (`requires_approval`/
`confirmed_action`/`StepContext.confirmed`) touches `common/` because
it's genuinely a reusable *mechanism* (any future kind's own mutating
action could opt into it via `AgentMeta.confirm_capability`), not
because Jenkins gets special-cased. Part of the same mechanism: when the
confirmed step raises past its retries, `workflow.py` records it as
`{"status": "interrupted"}` and `respond.execution_fact` reports the
outcome as unknown, never as "nothing was executed".
