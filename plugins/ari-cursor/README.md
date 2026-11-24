# ARI plugin for Cursor

Connects Cursor to your own ARI gateway, so you can ask the open-source ARI operations agents to get
work done from the editor:

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

## Set your gateway address

The plugin reads the gateway address from the `ARI_SERVER_URL` environment variable. Set it once for
your user account, then quit Cursor completely and start it again so it picks the value up.

Windows (PowerShell):

```
setx ARI_SERVER_URL "https://ari-mcp.example.com/mcp"
```

macOS and Linux (add to your shell profile, and start Cursor from a session that has it):

```
export ARI_SERVER_URL="https://ari-mcp.example.com/mcp"
```

## Install and sign in

Install the plugin from the Cursor marketplace, then open Customize, Plugins, Ari and press
Authenticate next to `ari-gateway`. A browser window opens to sign in with GitHub. Your gateway
decides who may use it and who may approve changes.

If you also added the same gateway address by hand under Customize, MCPs, remove that entry: Cursor
hides a plugin server whose address matches a server you added yourself. If the ARI plugin for
Claude Code is installed on the same computer, Cursor imports it too and lists a second `ari` server
that cannot connect; use `ari-gateway`.

## Use

Ask in plain language, for example:

- "Use ARI on https://github.com/acme/payments, branch staging: checkout throws an NPE since this
  morning. Find the cause and open a PR."
- "Use ARI to check the pods in the payments namespace."
- "Use ARI to show the last 3 builds of the Jenkins job deploy-payments."

When ARI has a change ready, Cursor shows the proposal (repository, branch, files, diff link). Tell it
to approve or decline. Cursor then asks you to confirm in a form from the gateway; tick the box and
submit.

## Tools

| Tool | What it does |
|---|---|
| `ari_ask` | Sends a request, or continues a conversation |
| `ari_status` | Shows where the work stands |
| `ari_approve` | Approves the waiting proposal |
| `ari_decline` | Declines the waiting proposal or stops running work |
| `ari_whoami` | Shows the signed-in user and what the agent can do |

## Privacy

The plugin talks only to the gateway address you configure. What the gateway and agents store is
decided by your own deployment. InsightFinder privacy policy: https://insightfinder.com/privacy-policy/

## License

Apache-2.0
