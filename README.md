# ARI Agent

An engineering agent you send a request to, in plain language, together with everything it needs for that one request: which model to use and the key for it, which repository, which credentials. It plans the work, does it, and answers.

Out of the box it is an **on-call code agent**: it clones the repository, analyzes the problem, writes and verifies a fix with [opencode](https://github.com/sst/opencode) in a sandbox, pushes a branch, and asks a person to approve before it opens the pull request. A plain question gets a plain answer, with no repository work at all.

**Nothing irreversible happens without a person saying yes.** When the agent is ready to open a pull request it pauses in A2A's `TASK_STATE_INPUT_REQUIRED` with everything needed to judge the change (title, target branch, files, a diff excerpt, a compare link to the pushed branch), and continues only after the caller approves. See [`docs/INPUT_REQUIRED_INTEGRATION.md`](docs/INPUT_REQUIRED_INTEGRATION.md).

It is a standard **A2A (Agent2Agent) protocol server**, built on the official [`a2a-sdk`](https://pypi.org/project/a2a-sdk/): JSON-RPC and REST transports, a real `/.well-known/agent-card.json`, real `Task`/`Message`/`Part` objects. Anything that speaks A2A can call it.

## Ways to use it

| From | How | Guide |
|---|---|---|
| Your own service | A2A over HTTP, blocking, polling or webhook | [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md), runnable [`docs/examples/sample_client.py`](docs/examples/sample_client.py) |
| A terminal | `ari`, an interactive chat with approval menus, plus scriptable commands | [`docs/CLI.md`](docs/CLI.md) |
| Slack or Microsoft Teams | the chat gateway: mention the bot, approve with buttons | [`docs/CHAT_GATEWAY.md`](docs/CHAT_GATEWAY.md) |

## Extend it with Agent Skills

The supported way to add what the agent can do, without touching its code, is an [Agent Skill](https://agentskills.io/specification): a folder with a `SKILL.md` of instructions and, optionally, scripts. The planner sees every installed skill and runs one when a request fits; the code-change agent can use skills while it edits a repository.

- [`docs/SKILLS.md`](docs/SKILLS.md) is the guide, starting with a ten-minute "Write your first skill".
- `skills/` has three examples: `tls-certificate-check` (no repository needed), `aws-security-group-allowlist` (a skill that changes infrastructure, with operator-set guard rails), and `changelog-entry` (used during a code change).
- `ari skills validate <folder>` checks a skill before you install it.

## Agent kinds

The same engine hosts several independently deployed *agent kinds* (`AGENT_KIND`), each with its own Agent Card, its own registries under `configs/<kind>/`, and its own worker image:

| Kind | What it does | Worker image |
|---|---|---|
| `oncall` | Code analysis and fixes, pull request after approval, Agent Skills | `ari-oncall-agent-worker` (`Dockerfile.oncall`) |
| `k8s` | Pod-level health of the cluster it runs in; Deployment scaling and CPU/memory changes, each proposed first and applied only after a person approves (see [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) §10) | `ari-k8s-agent-worker` (`Dockerfile.k8s`) |

The API image (`Dockerfile.api`) is shared by every kind. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) explains how a new kind is added. The Jenkins kind is deprecated and lives on its own branch.

## Run it locally

You need Docker with Compose, about 3 GB of disk for the images, an LLM API key (Anthropic, OpenAI, or another provider the agent supports) and, for changes to a repository, a GitHub token.

```bash
# 1. A throwaway Temporal server, with its web UI on http://localhost:8233
docker compose -f deploy/temporal-dev/docker-compose.yml up -d

# 2. The agent: the API on http://localhost:8000 and one worker
cp .env.example .env
docker compose up -d --build

# 3. Talk to it
pip install -e ".[cli]"
export ARI_AGENT_URL=http://localhost:8000 ARI_API_KEY=change-me-dev-key
export LLM_PROVIDER=anthropic LLM_MODEL=claude-haiku-4-5 LLM_API_KEY=...
export DEFAULT_REPO_URL=https://github.com/<org>/<repo> GITHUB_PAT=...
ari send "what is a deadlock?"
ari
```

`.env.example` is already set up for this: Temporal at `host.docker.internal:7233`, namespace `default`, no TLS, API key `change-me-dev-key`. Change `AGENT_API_KEYS` before the agent is reachable by anyone else. The model key and the GitHub token are never configured on the agent; they travel with each request, which is why they are set for `ari` above. `ari` sends a repository with every request, even a plain question, so it needs `DEFAULT_REPO_URL` (or `--repo`); `GITHUB_PAT` is needed for a private repository and for pushing a branch or opening a pull request.

The compose file mounts `src/`, `configs/` and `skills/`, so code changes reload the API and new skills are picked up on the next request. Without Docker, `scripts/run_api.sh` and `scripts/run_worker.sh` run the same two processes from a `pip install -e .`, provided `opencode` is on your `PATH`.

## Deploy it

Run one API Deployment and one worker Deployment per agent kind, against a Temporal service of your own. Everything the processes need is in `.env.example`; set `TEMPORAL_PAYLOAD_ENCRYPTION_KEY` for any shared deployment, because requests carry model keys and repository tokens through Temporal's history.

- [`deploy/k8s/`](deploy/k8s/README.md) has plain manifests for a scratch cluster.
- The `oncallari` Helm chart is what production uses (persistent volumes for the task store and caches, ingress, one release per kind).
- Images are published as `docker.io/insightfinderinc/ari-agent-api`, `ari-oncall-agent-worker`, `ari-k8s-agent-worker` and `ari-chat-gateway`, tagged `main`, `staging` and `test` after those branches.

## Why it looks the way it does

- **Temporal-only execution.** Every request runs as one Temporal workflow and one A2A `Task`. Temporal gives durability, retries and an audit trail without this service needing a database of its own.
- **Plan, then execute.** Requests vary ("...and skip tests", "just open the PR", "work from dev and target staging", a bare question). An LLM planning step, using the request's own model, reads the prompt and the registered capability catalog and emits an ordered plan; a generic executor runs it. Nothing is a hardcoded pipeline.
- **opencode does the coding.** `generate_change` and `verify` are thin wrappers around opencode running against the cloned repository in an isolated sandbox. This service does not reimplement code editing or test running.
- **Stateless by request.** Model keys, repository credentials and options arrive with each request and are never stored by the agent. The API keeps A2A tasks in memory, or on disk under `TASK_STORE_ROOT` so a task waiting for approval survives a restart.
- **Repositories are cached, not re-cloned.** A mirror per repository is kept fresh with an incremental fetch; every request still gets its own independent working copy.
- **One A2A operation, three ways to get the result.** A blocking `message/send` returns the finished task; a non-blocking one returns at once, and the caller polls `tasks/get` or registers a push-notification webhook.

[`docs/design.md`](docs/design.md) is the full design; [`docs/MEMORY.md`](docs/MEMORY.md) is the dated history of decisions and of what has been verified against real systems.

## Layout

```
├── configs/<kind>/          # capability and tool registries per agent kind (extend here, not in engine code)
├── deploy/
│   ├── k8s/                 # plain Kubernetes manifests for a scratch cluster
│   ├── k8s-agent-e2e/       # test-cluster deployment and fixtures for the k8s kind
│   └── temporal-dev/        # a throwaway local Temporal for development
├── docs/                    # guides (see above), design, and MEMORY.md
├── scripts/                 # local run helpers, live scenario scripts, the Teams app packager
├── skills/                  # bundled Agent Skills
├── src/
│   ├── api/                 # entry points: api.main (A2A server), api.worker (Temporal worker)
│   ├── common/              # the agent-kind-agnostic engine: A2A server, workflow, planner, respond, skills
│   ├── ari_oncall_agent/    # the oncall kind's capabilities, tools and metadata
│   ├── k8s_agent/           # the k8s kind's capabilities, tools and metadata
│   ├── ari_cli/             # the `ari` command
│   └── chat_gateway/        # the Slack and Teams gateway, sharing its bridge with `ari`
└── tests/
```

Beyond skills, a new **capability** is a module under `<kind package>/capabilities/` plus an entry in `configs/<kind>/capabilities.yaml`; the planner reads the catalog at plan time, so no engine change is needed. A new **tool** is a module under `<kind package>/tools/` plus an entry in `configs/<kind>/tools.yaml`.

## Testing

Two layers, and they catch different things.

### Unit tests

`pytest tests/` covers the engine, every capability against mocked tools, the CLI and the chat gateway. Several modules need Linux (`fcntl`, a real pty), so run the suite in the worker image, with the source mounted read-only:

```bash
docker run --rm -v "$PWD:/work:ro" -w /work -e PYTHONPATH=/work/src -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint sh insightfinderinc/ari-oncall-agent-worker:main -c \
  'pip install -q --root-user-action=ignore pytest pytest-asyncio pexpect "kubernetes>=37,<38" typer rich questionary prompt_toolkit \
     slack_bolt aiohttp microsoft-agents-hosting-aiohttp microsoft-agents-authentication-msal \
   && python -m pytest tests/ -q -rs -p no:cacheprovider'
```

Tests whose optional dependency is missing are skipped, not failed, so check the skip count.

**A green suite is not enough on its own.** Mocks always return something, so they cannot produce the conditions that have actually broken this agent: a model that edits nothing while reporting that it did, a clock that accumulates across retries, a brand-new file that `git diff HEAD` cannot see. All three shipped past a passing suite (`docs/MEMORY.md`, 2026-10-09).

### Live scenarios against a deployed agent

`scripts/oncall_live_test.py` is a plain HTTP client (its only dependency is `httpx`) that runs real scenarios against a running agent:

```bash
export ARI_BASE_URL=...  ARI_API_KEY=...
export LLM_PROVIDER=anthropic LLM_API_KEY=... LLM_MODEL=claude-haiku-4-5
export GITHUB_PAT=...    REPO_URL=https://github.com/<org>/<repo>  REPO_REF=<branch>

python scripts/oncall_live_test.py --list           # the six scenarios
python scripts/oncall_live_test.py                  # all of them, no writes
python scripts/oncall_live_test.py budget           # just one
python scripts/oncall_live_test.py --allow-writes   # include the one that opens a pull request
```

`ARI_BASE_URL` is wherever the agent answers: an ingress, or the in-cluster Service through `kubectl port-forward svc/<api service> 18001:80`, which is the path in-cluster callers use. `ARI_API_KEY` is one of the agent's `AGENT_API_KEYS`.

| scenario | what it proves |
|---|---|
| `card` | the agent card is served where integrations look for it |
| `budget` | a request given N seconds stops at about N, instead of running to the plan's design maximum |
| `empty` | a change that edits nothing fails fast, before `verify` is handed something that does not exist |
| `propose` | the pause: `INPUT_REQUIRED` plus a `pending_action` whose branch is already pushed |
| `confirm` | approve the proposal and a pull request opens |
| `reject` | a confirm carrying an id the task never proposed is refused, not quietly planned around |

It is deliberately not in `tests/`: each scenario costs real tokens and build time, and two of them write a branch and a pull request to `REPO_URL`. Run it by hand against a test environment and a repository you do not mind collecting branches in. Assert on `status.state` and on which artifacts resolved, never on the final message, which is an LLM writing for a person and is worded differently every run.

The `k8s` kind has its own equivalent, `scripts/k8s_agent_live_test.py`, run against the fixtures in [`deploy/k8s-agent-e2e/`](deploy/k8s-agent-e2e/README.md). Without `--allow-writes` it only reads and cancels its proposals.

Neither layer sees what only shows up inside a long-running worker (leaked child processes, growing caches); that is found by looking at the process, not at a response.

## Contributing

- Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) first. `common/` never names an agent kind; new behaviour enters through `AgentMeta`, `configs/<kind>/`, a new package, or a skill.
- Every change extends `tests/` and appends a dated entry to [`docs/MEMORY.md`](docs/MEMORY.md) saying what changed, why, and what was verified for real. Do not rewrite earlier entries.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
