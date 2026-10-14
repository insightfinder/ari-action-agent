# `ari` — chat with an ARI agent from your terminal

`ari` is a terminal client for the on-call agent. You ask in plain
language; the agent answers, or investigates a repository, makes a
change, pushes it to a branch and asks you before it opens a pull
request. It runs on your machine and talks to a deployed agent over its
A2A API; it never runs the agent itself.

It is built on `src/chat_gateway/`, the platform-neutral bridge the
Slack and Teams adapters use, so a conversation behaves the same way in
every one of them.

## Install

```bash
pip install -e ".[cli]"
```

## Start

```bash
ari
```

The first time, `ari` walks you through a short setup:

| question | what it is |
|---|---|
| Agent URL | where the agent answers, e.g. `https://ari.example.com` |
| Agent API key | one of the agent's `AGENT_API_KEYS` |
| LLM provider and model | the model the agent uses for your requests (`anthropic`, `openai`, or another provider the agent supports) |
| LLM API key | your key for that provider |
| GitHub token | needed to push branches and open pull requests |
| Default repository and branch | optional; you can also set them per conversation |

Secrets are not echoed, and stray spaces around them are trimmed. `ari`
then checks that the agent answers and saves everything to
`~/.config/ari/config.toml`, readable by you only; nothing is saved if
you cancel, if a value is unusable (no LLM key, an unknown provider
without a model, an agent URL without `http://` or `https://`), or if
the agent cannot be reached and you do not confirm.

Run `ari setup` (or `/setup` inside the chat) to change it later.
Pressing Enter keeps each current value; typing `-` clears an optional
value or a secret. Defaults come from what is in effect, including
`ARI_AGENT_URL` and `LLM_PROVIDER` in the environment, so pressing Enter
never switches you to a different provider. Answers can also be piped
in, one per line.

## The chat

```
╭─ ARI On-Call Agent ─────────────────────────────────────────────╮
│ Connected to ari.example.com                                    │
│ Repository: acme/payments                                       │
│ Works on branch dev; pull requests target dev                   │
│ Type a request, or /help for commands.                          │
╰─────────────────────────────────────────────────────────────────╯

you › What is a deadlock?
ARI ›
  A deadlock is when two or more processes wait forever for
  resources the others hold.

you › Add a SECURITY.md with a short vulnerability reporting policy
⠋ Working… 0:42 · root cause found

ARI ›
  I've prepared a pull request that adds SECURITY.md ...
╭─ Proposed change ───────────────────────────────────────────────╮
│ Add SECURITY.md with a vulnerability reporting policy           │
│ Repository  acme/payments                                       │
│ Branch      ari/add-security-md → dev                           │
│ Files       SECURITY.md                                         │
│ Tests       not run                                             │
│ Full diff   https://github.com/acme/payments/compare/dev...     │
│                                                                 │
│ +# Security Policy                                              │
╰─────────────────────────────────────────────────────────────────╯
? What do you want to do?
  ❯ Approve and open the pull request
    Decline
    Show the full diff link
    Ask a follow-up
    Decide later

✔ Pull request opened: https://github.com/acme/payments/pull/24
```

- **Asking.** Anything that is not a `/command` is sent to the agent.
  Answers appear under `ARI ›`; errors start with a red `Error:`, so an
  answer is never mistaken for a failure.
- **Proposals.** When the agent has a change ready it shows the card and
  a menu (arrow keys and Enter). The branch is already pushed when you
  see it; the pull request opens only if you approve. Declining leaves
  the branch on the repository. Keys typed while the agent was working
  are discarded before the menu appears, so they cannot pick an option.
- **You only approve what you have seen.** `/approve`, or typing a bare
  `yes`, `ok`, `approve`, opens the pull request only for the proposal
  whose card was last shown. If the proposal changed, or was never shown
  (for example after Ctrl-C), `ari` shows the card and menu first.
  `/decline`, `no` or `decline` work the same way.
- **Decide later** returns to the prompt with the proposal still
  waiting and names the task. `/status` shows the card and menu again.
- **When the agent asks a question** instead of proposing a change, no
  card or menu appears; type your reply, and a `yes` there is sent as an
  answer, not taken as an approval.
- **Follow-ups** ("also run the tests first") continue the same task, so
  the agent sees the conversation so far. If a follow-up finishes
  without a new proposal, the earlier one can no longer be approved and
  `ari` says so. A `/command` typed at the follow-up prompt runs as a
  command; it is never sent to the agent.
- **Leaving** with `/new`, `/setup`, `/quit` or Ctrl-D while a proposal
  waits or a task runs prints the task id and the command to pick it up
  again (`ari approve <id>` or `ari tasks follow <id>`).
- **Ctrl-C** while the agent is working stops waiting; the task carries
  on, `/wait` follows it again and `/cancel` stops it. A message typed
  while it still runs is held back with that hint rather than sent.
  Ctrl-C at the prompt clears the line. **Ctrl-D** or `/quit` leaves.
- **Commands** can be shortened to any unique prefix (`/st` is
  `/status`). An `ari ...` shell command typed into the chat is
  explained, not sent to the agent.

| command | what it does |
|---|---|
| `/help` | list the commands |
| `/repo <url>` | repository for your next requests |
| `/branch <name>` | branch to work from |
| `/base <name>` | branch the pull request targets; each request then says "if this request leads to a pull request, open it against `<name>`", so a plain question stays a question |
| `/status` | where the current task is; a waiting proposal is shown with its menu |
| `/wait` | follow the current task again until it finishes, e.g. after Ctrl-C |
| `/cancel` | stop the current task (asks first); on a waiting proposal it declines it |
| `/approve` / `/decline` | act on the waiting proposal |
| `/new` | start a new conversation |
| `/config` | show the current settings, secrets masked |
| `/setup` | change the connection and keys |
| `/quit` | leave |

Commands complete with Tab and earlier input is in the history (Up
arrow), kept in `~/.config/ari/history` (or `ARI_HISTORY_FILE`); when
that file cannot be written, history lasts for the session only.

When `ari` is not attached to a terminal (piped input, CI), the same
chat runs without colours or arrow-key menus: menus become numbered
choices. Output that cannot be encoded on the current console, such as
`→` or `✔` on a Windows code page, is replaced rather than crashing.
Agent text is shown literally; brackets in file names such as
`app/[slug]/page.tsx` are never read as formatting.

## Settings

Highest priority first:

1. command-line flags (`--url`, `--key`, `--repo`, `--ref`)
2. environment variables (`ARI_AGENT_URL`, `ARI_API_KEY`, `LLM_PROVIDER`,
   `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`, `GITHUB_PAT`,
   `DEFAULT_REPO_URL`, `DEFAULT_REPO_REF`, `ALLOWED_REPO_HOSTS`,
   `OPTIONS_JSON`, `POLL_SECONDS`, `WAIT_SECONDS`,
   `AGENT_OUTAGE_SECONDS`, `REQUEST_TIMEOUT_SECONDS`)
3. `~/.config/ari/config.toml` (or the file named by `ARI_CONFIG_FILE`),
   written by the setup or by `ari config set`

The setup is skipped when the environment already supplies an agent URL
and an LLM key, which is how a shared or scripted installation is
configured.

### Repository allow-list

`GITHUB_PAT` is attached to every repository URL `ari` sends, and the
agent embeds it in the clone URL. A URL on an unexpected host would hand
the token to that host, so a request is refused unless the host is
`github.com`, a host named in `ALLOWED_REPO_HOSTS` (comma-separated), or
the host of `DEFAULT_REPO_URL` / a `REPO_MAP` entry. Plain `http://` and
URLs with embedded credentials are always refused.

## Scripting and automation

The chat is for people. For scripts, the same operations are available
as one-shot commands that print plain text and return an exit code:

```bash
ari card                                   # the agent card, as integrations see it
ari send "fix the NPE in CustomerLookup"   # one request, followed to the end
ari send "add a CONTRIBUTING.md" --ref dev --base staging
ari send "..." --no-wait                   # prints the task id and returns
ari approve <task id>                      # confirm a paused proposal
ari tasks follow <task id>                 # re-attach to a running task
ari tasks get <task id>                    # the task as JSON, secrets redacted
ari tasks cancel <task id>
ari config show
ari config set <name> [value]              # omit the value to be prompted
```

Inside a `send` message, `repo=<url>`, `ref=<branch>` and
`base=<branch>` are read out of the text and applied to the request.

`ari approve` needs `GITHUB_PAT` on the machine it runs from: the resume
is a single `publish_pr` step on the agent, and without a token in the
confirm it would fail after the proposal has already been consumed.

| situation | behaviour |
|---|---|
| task ends `TASK_STATE_FAILED` | message printed, exit 1 |
| agent answers 4xx/5xx after retries, or is unreachable | `error: <operation> ...`, exit 1 |
| missing LLM key, repository, or a disallowed host | `error: ...`, exit 1 |
| the agent stops answering while a task is followed (a rollout, a pod restart) | `… agent unavailable (...); still waiting` once, then polling continues; after `AGENT_OUTAGE_SECONDS` (600) of unbroken failure: `error: agent unreachable for Ns ...`, exit 1 |
| `WAIT_SECONDS` elapses while the task runs | `error: task ... still TASK_STATE_WORKING after Ns; the run continues on the agent`, exit 1 |
| a bad value in the environment or the config file | `error: invalid setting: ...`, exit 1 (`config set` refuses it up front with exit 2) |

Giving up on a task never cancels it: `ari tasks follow <id>` picks it
up again. Reads (`tasks get`, polling) are retried on transient failures
with the agent's own policy (`src/common/http_retry.py`); `message:send`
is never retried, because it creates a task and a retry after a lost
response would start a second run. A confirm sent while the agent is
down therefore fails at once and leaves the task paused, so running
`ari approve` again is safe.

## Testing

Unit tests run on Windows and Linux without a deployed agent. They need
the `cli` extra; without `typer` installed the CLI test modules skip
themselves so the rest of the suite is unaffected.

```bash
pip install -e ".[cli,dev]"
python -m pytest tests/test_chat_gateway_*.py tests/test_ari_cli*.py -q
```

`tests/test_ari_cli_tty.py` drives the real terminal interface (arrow-key
menus, Tab completion, the setup wizard) through a pseudo-terminal
against a small fake agent. It needs `pexpect` and a POSIX system, and
skips itself otherwise.

Against a deployed agent, the acceptance sequence mirrors
`scripts/oncall_live_test.py`: start `ari`, ask a plain question, ask
for a small change, approve it from the menu, and check the pull request
on GitHub. Point the default repository at one you do not mind
collecting branches in: every proposal pushes one before it pauses.
