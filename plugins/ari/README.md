# ARI plugin for Claude Code

Connects Claude Code to your own ARI gateway, so you can ask the open-source ARI operations agents
to get work done from the terminal or the IDE:

- find the root cause of a problem in a code repository, prepare a fix on a branch and open a pull
  request;
- check the health of Kubernetes workloads and change them (scale, resources);
- query Jenkins builds and trigger jobs.

Every change is proposed first and carried out only after a person approves it. The agent never
merges a pull request.

## Before you install

ARI is self-hosted. You need an ARI MCP gateway that your organisation runs, with its Coordinator and
agents behind it. Deployment instructions:
https://github.com/insightfinder/ari-action-agent/blob/main/docs/MCP_GATEWAY.md

## Install

```
claude plugin marketplace add insightfinder/ari-action-agent
claude plugin install ari@ari-agent
```

When the plugin is enabled, Claude Code asks for one setting:

| Setting | Example |
|---|---|
| `server_url` | `https://ari-mcp.example.com/mcp` |

Sign in once: run `claude` in a terminal, type `/mcp`, choose `plugin:ari:ari` and Authenticate. A
browser window opens to sign in with GitHub. The Claude desktop app shares this sign-in but does not
offer it for plugin servers, so sign in from the terminal first and then start a new session in the app.
Your gateway decides who may use it and who may approve changes.

## Use

Ask in plain language, for example:

- "Use ARI on https://github.com/acme/payments, branch staging: checkout throws an NPE since this
  morning. Find the cause and open a PR."
- "Use ARI to check the pods in the payments namespace."
- "Use ARI to show the last 3 builds of the Jenkins job deploy-payments."

When ARI has a change ready, Claude shows the proposal (repository, branch, files, diff link). Tell it
to approve or decline. If the app cannot show the approval dialog, ARI returns a link: open it, sign
in with GitHub and press Approve.

## Tools

| Tool | What it does |
|---|---|
| `ari_ask` | Sends a request, or continues a conversation |
| `ari_status` | Shows where the work stands |
| `ari_approve` | Approves the waiting proposal |
| `ari_decline` | Declines the waiting proposal or stops running work |
| `ari_whoami` | Shows the signed-in user and what the agent can do |

## Privacy

The plugin talks only to the gateway URL you configure. What the gateway and agents store is decided
by your own deployment. InsightFinder privacy policy: https://insightfinder.com/privacy-policy/

## License

Apache-2.0
