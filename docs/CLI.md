# `ari` — talking to a deployed agent from the command line

`ari` is a thin client for the on-call agent's A2A surface. It runs on
your machine, needs nothing but Python and network access to a deployed
agent, and never runs the agent itself. It is also the first consumer of
`src/chat_gateway/`, the platform-neutral bridge the Slack and Teams
adapters are built on, so anything that works here works there.

## Install

```bash
pip install -e ".[cli]"
ari --help
```

## Configure

Three layers, highest priority first:

1. command-line flags (`--url`, `--key`, `--repo`, `--ref`)
2. environment variables (`ARI_AGENT_URL`, `ARI_API_KEY`, `LLM_PROVIDER`,
   `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, `GITHUB_PAT`,
   `DEFAULT_REPO_URL`, `DEFAULT_REPO_REF`, `ALLOWED_REPO_HOSTS`,
   `OPTIONS_JSON`, `POLL_SECONDS`, `WAIT_SECONDS`,
   `AGENT_OUTAGE_SECONDS`, `REQUEST_TIMEOUT_SECONDS`)
3. `~/.config/ari/config.toml` (or the file named by `ARI_CONFIG_FILE`),
   written by `ari config set`

No dotenv file is read. The gateway process reads the same variables
from its environment.

```bash
ari config set ari_agent_url http://127.0.0.1:18001
ari config set ari_api_key            # prompted, not echoed
ari config set llm_provider anthropic
ari config set llm_api_key            # prompted
ari config set github_pat             # prompted
ari config set default_repo_url https://github.com/<org>/<repo>
ari config show
```

Give a secret on the command line only when the shell history is not a
concern; omitting the value prompts for it without echo. The file is
created readable by the owner only (`0600`), and `config show` masks
every secret to its first four characters.

The agent itself is reached the way `README.md` describes under "Live
scenarios against a deployed agent": directly when an ingress exists, or
through `kubectl port-forward svc/ari-oncall-api 18001:80`.

The `llm` and `repo` blocks the agent expects (see
`docs/DEVELOPER_GUIDE.md` §5) are built from these settings on every
request. The agent stores nothing between requests; the CLI is the one
holding the credentials.

### Repository allow-list

`GITHUB_PAT` is attached to every repository URL the CLI sends, and the
agent embeds it in the clone URL. A URL on an unexpected host would hand
the token to that host, so a request is refused unless the host is
`github.com`, a host named in `ALLOWED_REPO_HOSTS` (comma-separated), or
the host of `DEFAULT_REPO_URL` / a `REPO_MAP` entry. Plain `http://` and
URLs with embedded credentials are always refused.

## Use

```bash
ari card                                   # the agent card, as integrations see it
ari send "fix the NPE in CustomerLookup"   # one request, follows it to the end
ari send "add a CONTRIBUTING.md" --ref dev --base staging
ari send "..." --no-wait                   # prints the task id and returns
ari tasks get <task id>                    # the task as JSON, secrets redacted
ari tasks follow <task id>                 # re-attach to a running task and wait for its end
ari tasks cancel <task id>
ari approve <task id>                      # confirm a paused proposal
ari chat                                   # interactive, one conversation
```

Inside a message, `repo=<url>`, `ref=<branch>` and `base=<branch>` are
read out of the text and applied to the request, so `ari chat` can switch
repositories without restarting:

```
you> fix the flaky retry test repo=https://github.com/org/other ref=dev base=staging
```

`ref` becomes the branch the agent checks out; `base` is appended to the
prompt as "Open the pull request against the staging branch", because the
agent's planner reads the target branch from the prompt, not from config.

## What a run looks like

~~~
$ ari send "add a minimal CONTRIBUTING.md"
  … working (task 4bffcf4a)
  … working (task 4bffcf4a); produced: root_cause
Ready to open a pull request.

Proposed: Open pull request
Repo: org/repo (ari/add-contributing-4bffcf4a -> main)
Title: Add a minimal CONTRIBUTING.md
Files: CONTRIBUTING.md
Verification: not run
Full diff: https://github.com/org/repo/compare/main...ari/add-contributing-4bffcf4a
```
diff --git a/CONTRIBUTING.md b/CONTRIBUTING.md
+# Contributing
...
```

task 4bffcf4a-38c9-4493-b2ad-54e2a9b1b94f
approve with: ari approve 4bffcf4a-38c9-4493-b2ad-54e2a9b1b94f
~~~

The pause is the agent's `TASK_STATE_INPUT_REQUIRED`, exactly as
`docs/INPUT_REQUIRED_INTEGRATION.md` describes it. By the time you see
it the branch is already pushed (that is what the `Full diff` link
shows); the pull request is opened only after `ari approve`. Declining is
`ari tasks cancel <id>`, which leaves the pushed branch in place.

`ari approve` needs `GITHUB_PAT` on the machine it runs from: the resume
is a single `publish_pr` step on the agent, and without a token in the
confirm it would fail after the proposal has already been consumed.

In `ari chat` the same pause is offered as a `y/N` prompt. `/approve`,
`/decline` and `/status` act on the conversation's current task; `/quit`
leaves. Typing a bare `yes`, `ok`, `approve`, `no` or `decline` while a
proposal is waiting is treated as the decision rather than sent to the
agent, because the agent ignores free-text confirmations by design.

Any other message typed while a proposal is waiting continues that same
task, so the agent sees the conversation so far ("also run the tests
first" is understood). The agent keeps history per task, and a task that
finishes is final: if the follow-up ends without a new proposal, the
earlier one can no longer be approved and the CLI says so. Approve or
decline first when you want to keep it.

## Exit codes and errors

| situation | behaviour |
|---|---|
| task ends `TASK_STATE_FAILED` | message printed, exit 1 |
| agent answers 4xx/5xx after retries, or is unreachable | `error: <operation> ...`, exit 1 |
| missing LLM key, repository, or a disallowed host | `error: LLM_API_KEY is not set` / `no repository: ...` / `repository host ... is not allowed`, exit 1 |
| the agent stops answering while a task is followed (a rollout, a pod restart) | `… agent unavailable (...); still waiting` once, then polling continues; after `AGENT_OUTAGE_SECONDS` (600) of unbroken failure: `error: agent unreachable for Ns ...`, exit 1 |
| `WAIT_SECONDS` elapses while the task runs | `error: task ... still TASK_STATE_WORKING after Ns; the run continues on the agent`, exit 1 |

Giving up on a task never cancels it. `ari tasks follow <id>` picks it up
again; `ari tasks get <id>` shows where it is.

The test environment redeploys the agent on every push to the `test`
branch, so a run can easily overlap a rollout. The agent keeps paused and
running tasks across a restart when `TASK_STORE_ROOT` is set (see
`docs/MEMORY.md`, 2026-10-10 and 2026-10-12), which is what makes riding
it out worthwhile. A confirm sent while the agent is down fails at once
(`message:send` is never retried); the task is then still waiting, and
running `ari approve` again is safe.
| a bad value in the environment or the config file | `error: invalid setting: ...`, exit 1 (`config set` refuses it up front with exit 2) |

Inside `ari chat` these are printed and the session continues.

Reads (`tasks get`, polling) are retried on transient failures with the
same policy the agent uses for its own outbound calls
(`src/common/http_retry.py`). `message:send` is never retried: it
creates a task on the agent, and a retry after a lost response would
start a second run.

## Testing the CLI

Unit tests run on Windows and Linux without a deployed agent. They need
the `cli` extra; without `typer` installed `tests/test_ari_cli.py` skips
itself so the rest of the suite is unaffected.

```bash
pip install -e ".[cli,dev]"
python -m pytest tests/test_chat_gateway_a2a_client.py tests/test_chat_gateway_bridge.py \
  tests/test_chat_gateway_config.py tests/test_chat_gateway_rendering.py \
  tests/test_chat_gateway_session.py tests/test_ari_cli.py -q
```

Against a deployed agent, the acceptance sequence is the same one
`scripts/oncall_live_test.py` proves, driven by hand:

```bash
ari card                                          # skills[0].id == code_analysis_fix
ari send "what is a race condition, in one sentence?"   # completes with no repo work
ari send "add a minimal CONTRIBUTING.md"          # pauses with a proposal
ari approve <task id>                             # opens the pull request
```

Point `DEFAULT_REPO_URL` at a repository you do not mind collecting
branches in: every proposal pushes one before it pauses.
