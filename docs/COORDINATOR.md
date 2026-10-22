# Coordinator: one entry point for several agents

ARI agents are used in two ways. Customers with InsightFinder UIE and MCP Service reach them through
MCP Service, which decides which agent handles a request. The open-source edition has neither, so
this repository ships its own small Coordinator for the `ari` CLI and the Slack and Teams gateways.

```
ari CLI / Slack / Teams --A2A--> Coordinator --A2A--> oncall agent
                                             \--A2A--> k8s agent
```

The Coordinator is itself an A2A server. To a client it looks like one more agent, so the CLI and the
chat gateway need no changes: point `ARI_AGENT_URL` at the Coordinator instead of an agent.

## What it does

- **Routes each new request to one agent.** It asks the request's own model (the `llm` section the
  client already sends; the Coordinator holds no model key) which agent fits, given each agent's card
  (name, description, skills) and the agent the conversation used last.
- **Sticks to the conversation's agent.** Follow-ups stay with the agent that answered before unless
  the request clearly belongs to another one. If the model call fails, the conversation's agent is
  used, then `default_agent`. With only one reachable agent, no model is asked at all.
- **Gives follow-ups their context.** An agent only remembers within one task, so a follow-up in
  the same conversation would start blank. The Coordinator keeps the last few requests and the
  agents' answers, and prefixes a new request with them ("Earlier in this conversation ...",
  `COORDINATOR_CONTEXT_TURNS`, default 3; 0 turns it off). A reply on the same task, such as an
  approval, needs no prefix and gets none.
- **Relays everything the agent produces**: answers, `pending_action` proposals, pull request links,
  and the final state, including `TASK_STATE_INPUT_REQUIRED`. The routing decision is published as a
  `route` artifact (`agent`, `reason`, `decided_by`).
- **Forwards approvals and declines to the agent's own task.** The proposal id is passed through
  unchanged and checked by the agent, so the approval contract in
  [`INPUT_REQUIRED_INTEGRATION.md`](INPUT_REQUIRED_INTEGRATION.md) holds end to end.
- **Forwards only the config an agent needs.** Clients send one config shaped for the oncall agent,
  including the GitHub token in `repo`. Each agent lists the keys it receives (`forward_config`); the
  k8s agent gets `llm` and `options` and never sees the token.
- **Survives restarts.** Which agent task backs each Coordinator task is kept under
  `TASK_STORE_ROOT`. After a restart it resumes following running tasks, and a proposal that was
  waiting can still be approved.

One request goes to one agent. Splitting a request across several agents is planned for a later version.

## Agents

`configs/coordinator/agents.yaml`, or the file named by `COORDINATOR_AGENTS_FILE`:

```yaml
agents:
  - name: oncall
    url_env: ONCALL_AGENT_URL
    api_key_env: ONCALL_AGENT_API_KEY
    forward_config: [repo, llm, options, system_prompt]
  - name: k8s
    url_env: K8S_AGENT_URL
    api_key_env: K8S_AGENT_API_KEY
    forward_config: [llm, options]
default_agent: oncall
```

`url` can be given literally instead of `url_env`. API keys only come from the environment. An agent
without a URL, or whose card cannot be read, is shown as unavailable on `GET /healthz` and is not
routed to. Cards are re-read every `COORDINATOR_CARD_REFRESH_SECONDS`; the Coordinator's own card
offers the skills of every reachable agent.

## Running it

```
uvicorn coordinator.main:app --host 0.0.0.0 --port 8000
```

It needs no Temporal, no opencode and no git, and it ships in the API image (`Dockerfile.api`) with
this command. Run one replica with `strategy: Recreate` and a persistent volume for `TASK_STORE_ROOT`.
The `oncallari` Helm chart deploys it with `orchestration: coordinator` (the default, `mcp`, deploys
nothing extra); list the agents under `coordinator.agents` with their in-cluster URLs and keys.

| Variable | Meaning |
|---|---|
| `AGENT_API_KEYS` | Keys the CLI and gateways must send in `x-api-key`. |
| `COORDINATOR_AGENTS_FILE` | The agents file. Default `configs/coordinator/agents.yaml`. |
| `COORDINATOR_NAME`, `COORDINATOR_PUBLIC_URL` | Name and address on its agent card. Default `ARI`, `http://localhost:8000`. |
| `TASK_STORE_ROOT` | Where tasks and routes are kept. Empty: memory only, a restart loses them. |
| `COORDINATOR_POLL_SECONDS` | How often a running agent task is polled. Default 5. |
| `COORDINATOR_FOLLOW_BUDGET_SECONDS` | Longest a task is followed. Default 7200. |
| `COORDINATOR_AGENT_OUTAGE_SECONDS` | How long an agent may be unreachable mid-task before the task fails. Default 600. |
| `COORDINATOR_CARD_REFRESH_SECONDS` | How often agent cards are re-read. Default 300. |
| `COORDINATOR_ROUTE_TURNS` | Earlier requests of the conversation shown to the routing model. Default 6. |
| `COORDINATOR_CONTEXT_TURNS` | Earlier requests and answers passed to the agent with a follow-up. Default 3; 0 turns it off. |

## Limits

- One agent per request; a request spanning two agents goes to the better match.
- Clients still send the oncall-shaped config, and `ari` still asks for a repository even for a
  Kubernetes question. Config filtering makes this harmless.
- Follow-up context is the last few requests with answers cut to 2,000 characters, not the full
  history, and it is plain text in the prompt, not structured data.
