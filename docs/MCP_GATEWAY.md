# MCP gateway: the ARI agent in Claude, Cursor and other AI apps

The MCP gateway is a remote [Model Context Protocol](https://modelcontextprotocol.io) server. People add
one URL to the AI app they already use, sign in with GitHub, and then ask that app to get work done by
the ARI agent: find the cause of an error in a repository and prepare a pull request, check or change a
Kubernetes workload, or answer an operations question. Nothing is installed on their machine.

```
Claude (desktop, web), Cursor, Claude Code, ...
        │  MCP over HTTPS, OAuth 2.1 sign-in
        ▼
  MCP gateway  (this process)  ── holds the LLM key, the GitHub token, the agent key
        │  A2A
        ▼
  Coordinator ──> oncall agent / k8s agent / Jenkins agent
```

The gateway is one more client of the agents, like the `ari` CLI and the Slack and Teams gateways
([CHAT_GATEWAY.md](CHAT_GATEWAY.md)), and shares their bridge code in `src/chat_gateway/`. A
conversation, a proposal and an approval behave the same way in all of them.

## What people do

1. Add the server in their app (see [Connecting an app](#connecting-an-app)) and sign in when asked.
   The browser shows a page naming the app that asked for access and where it will be sent back to;
   they confirm, sign in with GitHub, and are returned to the app.
2. Ask in plain language: "why does checkout throw an NPE since this morning? The repo is
   https://github.com/acme/payments, fix it against staging."
3. The app calls the agent and follows the work. When the agent has a change ready, the app shows the
   proposal: repository, branch, files, a diff excerpt and the full diff link.
4. They tell the app to approve it, or to decline it, or ask for changes. The pull request opens only
   after an approval, and the agent never merges.

## Tools

| Tool | What it does |
|---|---|
| `ari_ask` | Sends a request. Starts a conversation, or continues one with `conversation_id`. Optional `repo` (https URL), `ref` (branch to work from), `base` (branch a pull request targets). |
| `ari_status` | Shows where a conversation's work stands; waits briefly while the agent is working. |
| `ari_approve` | Approves the waiting proposal. Needs the `proposal_id` that was shown. |
| `ari_decline` | Declines the waiting proposal, or stops running work. |
| `ari_whoami` | Shows the signed-in GitHub user, whether they may approve, and what the agent can do. |

A call returns within `MCP_TOOL_WAIT_SECONDS` (default 25), because apps give up on long tool calls.
Work that takes longer keeps running on the agent; the result says "working" and the app calls
`ari_status` again. The server's instructions tell the app to do that.

## Approvals

An approval lets the agent open a pull request or change a cluster, so it is guarded in layers:

- **Only the person who asked, or someone in `MCP_APPROVERS`, can approve.** With
  `REQUESTER_CAN_APPROVE=false`, only an approver other than the requester can (four-eyes). Identity is
  the GitHub login from the sign-in, never a name the app passes in.
- **The app has to name the exact proposal.** `ari_approve` takes the `proposal_id` from the proposal
  it showed; a stale or guessed id is refused. Typing "yes" through `ari_ask` is refused as well, so an
  approval always goes through `ari_approve`.
- **The person confirms in the app.** When the app supports MCP elicitation, `ari_approve` asks the
  person directly ("Approve this proposal from the ARI agent?" with the repository, branch, files and
  diff link), outside the model's control. Declining leaves the proposal waiting. On protocol
  2026-07-28 connections the question travels as an `input_required` result and the app retries with
  the answer; the state that comes back is sealed by the SDK and bound to the signed-in user, so an
  answer cannot be replayed by someone else or for another proposal.
- **The app's own tool confirmation.** `ari_approve` and `ari_decline` are marked as destructive, so
  apps that ask before running such tools do so.

- **The browser approval page.** When the app cannot show the dialog, or the dialog was declined,
  `ari_approve` returns a single-use link instead (valid 15 minutes). The person opens it in their
  browser, signs in with GitHub, sees the proposal (repository, branch, files, diff link) and approves
  or declines it there. The page re-checks the signed-in GitHub login against the approval rules, so a
  link forwarded to someone in `MCP_APPROVERS` lets them approve as themselves, and anyone else is
  refused. The model cannot approve through the link: it needs a GitHub sign-in in the person's
  browser, the sign-in must finish in the browser that opened the link, the page cannot be framed, and
  the buttons carry a per-page token. Approve also needs "I have reviewed this change" ticked; without
  it nothing happens and the link keeps working. Decline needs no tick.

`MCP_APPROVAL_CONFIRMATION` chooses how the person confirms:

| Value | Behaviour |
|---|---|
| `dialog` (default) | The gateway's own dialog where the app shows it; the browser approval page otherwise. Nothing is approved without one of the two. |
| `browser` | Always the browser approval page. |
| `app` | No gateway dialog and no page; the app's own tool confirmation is the only check, so a session running without permission prompts approves without asking anyone. |

Claude Code inside the Claude desktop app advertises elicitation but declines the gateway's dialog
within a second without showing it; with `dialog`, those people get the browser link.

## Sign-in and secrets

The gateway is its own OAuth 2.1 authorization server, as the MCP authorization specification
describes, and uses GitHub only to find out who is signing in:

- Apps register themselves (Dynamic Client Registration) and use the authorization code flow with
  PKCE. Tokens are bound to this server's `/mcp` resource and are refused anywhere else.
- Before anyone is sent to GitHub, the gateway shows its own confirmation page naming the app and its
  return address. Without it, a registered look-alike app could ride on a GitHub session the person
  already has. The page cannot be framed, and the sign-in is bound to the browser that started it.
- A GitHub user may sign in only if listed in `MCP_ALLOWED_USERS` or an active member of an
  organisation in `MCP_ALLOWED_GITHUB_ORGS`. The gateway refuses to start with neither.
- The GitHub token from the sign-in is used once, to read the login (and organisation membership), and
  then dropped.
- Access tokens last `MCP_ACCESS_TOKEN_SECONDS` (1 hour) and are refreshed with rotating refresh tokens
  (`MCP_REFRESH_TOKEN_SECONDS`, 30 days). `MCP_STATE_FILE` keeps registered apps and **SHA-256 hashes**
  of tokens, never the tokens themselves, written atomically with mode 0600.
- The LLM key, the GitHub token the agent pushes with, the Jenkins token and the agent API key stay in
  the gateway's environment. People never see or hold them.

**Taking access away.** Remove the user from `MCP_ALLOWED_USERS` and restart: their tokens stop working
at once. For organisation-based access, run `python -m mcp_gateway revoke <github login>` and restart;
otherwise a refresh token keeps working until it expires, because the gateway cannot re-check
organisation membership without the person.

## Connecting an app

The server address is `https://<host>/mcp`.

- **Claude (desktop app and claude.ai):** Settings → Connectors → Add custom connector, paste the
  address, then Connect. On Team and Enterprise plans an owner adds it once for the organisation. Claude
  connects from Anthropic's servers, so the gateway must be reachable from the internet.
- **Cursor:** Settings → MCP → Add a server, or in `~/.cursor/mcp.json`:
  `{"mcpServers": {"ari": {"url": "https://<host>/mcp"}}}`. Cursor opens the sign-in when needed.
- **Claude Code:** `claude mcp add --transport http ari https://<host>/mcp`, then `/mcp` to sign in.
- **Claude Code plugin:** this repository is also a plugin marketplace. Run
  `claude plugin marketplace add insightfinder/ari-action-agent`, then
  `claude plugin install ari-ai-agent@ari-ai-agent`, and enter the gateway address when asked for `server_url`.
  The plugin lives in [`plugins/ari-ai-agent`](../plugins/ari-ai-agent/README.md); check it with
  `claude plugin validate --strict --profile directory plugins/ari-ai-agent`.
- **Cursor plugin:** the repository is also a Cursor plugin marketplace (`.cursor-plugin/marketplace.json`).
  The plugin lives in [`plugins/ari-ai-agent-cursor`](../plugins/ari-ai-agent-cursor/README.md) and reads the gateway
  address from the `ARI_SERVER_URL` environment variable, so each person points it at their own
  gateway. Cursor shows the approval form from the gateway inside the chat.

## Setting it up

1. **A GitHub OAuth App** (GitHub → Settings → Developer settings → OAuth Apps → New, under an
   organisation or a person). Homepage: `https://<host>`. Authorization callback URL:
   `https://<host>/oauth/github/callback`. Its client id and a client secret are
   `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET`. With `MCP_ALLOWED_GITHUB_ORGS`, the
   organisation may have to approve the OAuth App under its third-party access settings, or membership
   cannot be read.
2. **A public HTTPS address** in front of the gateway's port (default 8080). Any reverse proxy works;
   with Caddy:

   ```
   ari-mcp.example.com {
       reverse_proxy mcp-gateway:8080
   }
   ```

3. **Run it** from the chat gateway image with `python -m mcp_gateway`, one replica, with a small
   volume for `MCP_STATE_FILE` and `MCP_SESSION_FILE`:

   ```yaml
   mcp-gateway:
     image: docker.io/insightfinderinc/ari-chat-gateway:<tag>
     command: ["python", "-m", "mcp_gateway"]
     restart: unless-stopped
     env_file: [agent.env, mcp.env]
     environment:
       MCP_STATE_FILE: /data/oauth.json
       MCP_SESSION_FILE: /data/sessions.json
     volumes:
       - ./data/mcp:/data
   ```

   or `pip install ".[mcp]"` and `python -m mcp_gateway`. `.env.example.mcp-gateway` lists the variables.

One replica, because conversations, sign-ins in progress and MCP sessions live in that process.

| Variable | Meaning |
|---|---|
| `MCP_PUBLIC_URL` | The public address, `https://<host>`, no path. Required. Tokens and sign-in pages are issued for it. |
| `MCP_PATH` | The MCP endpoint path. Default `/mcp`. |
| `MCP_HOST`, `MCP_PORT` | Where the process listens. Default `0.0.0.0` and `8080`. |
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | The GitHub OAuth App people sign in with. Required. |
| `MCP_ALLOWED_USERS` | GitHub logins allowed to use it, comma-separated. |
| `MCP_ALLOWED_GITHUB_ORGS` | GitHub organisations whose active members may use it. At least one of these two is required. |
| `MCP_APPROVERS` | GitHub logins who may approve or decline anyone's proposal. |
| `REQUESTER_CAN_APPROVE` | Default `true`. `false` is the four-eyes rule, as in the chat gateways. |
| `MCP_APPROVAL_CONFIRMATION` | `dialog` (default), `browser` or `app`; see [Approvals](#approvals). |
| `MCP_STATE_FILE` | Registered apps and token hashes. Empty: memory only, so a restart signs everyone out. |
| `MCP_SESSION_FILE` | Conversations (ids only, as `SLACK_SESSION_FILE`). Empty: memory only. |
| `MCP_TOOL_WAIT_SECONDS` | How long one tool call follows the work before returning "working". Default 25, at most 50. |
| `MCP_ACCESS_TOKEN_SECONDS`, `MCP_REFRESH_TOKEN_SECONDS` | Token lifetimes. Default 3600 and 30 days. |
| `ARI_AGENT_URL`, `ARI_API_KEY`, `LLM_*`, `GITHUB_PAT`, `JENKINS_*`, `DEFAULT_REPO_*`, `ALLOWED_REPO_HOSTS` | As in the chat gateways. Point `ARI_AGENT_URL` at the [Coordinator](COORDINATOR.md) to reach every agent. |

## Limits

- A conversation is kept for 24 hours after its last activity.
- One call per conversation at a time; a second one is refused until the first returns.
- The agents do not see the person's local files or uncommitted changes. They work on the repository
  as it is on GitHub, at `ref`.
- Apps differ in what they show while a tool runs. Progress is sent as MCP progress notifications, which
  some apps display and others ignore.
