# Chat gateway: talking to the agent from Slack and Microsoft Teams

The chat gateway is a small separate process that lets people use the ARI agent from Slack or Microsoft Teams. One process serves one platform: `python -m chat_gateway slack` or `python -m chat_gateway teams` (default `slack`, or set `CHAT_PLATFORM`). Everything below is about Slack until the [Microsoft Teams](#microsoft-teams) section, which lists only what differs. It holds the Slack connection, turns messages into A2A requests to the agent, follows each task, and shows approvals as buttons. The agent itself does not change; the gateway is just another client, like the `ari` CLI ([CLI.md](CLI.md)), and shares its bridge code in `src/chat_gateway/`.

## What people see

```
@ARI Agent fix the NPE in CustomerLookup base=staging
  └─ ⏳ Working on it...                        <- edited in place while the agent works
  └─ ✋ Waiting for approval (task 4bffcf4a)
  └─ Ready to open a pull request.
     Title: Null-check customer lookup ...
     Files: src/...  Full diff: https://github.com/...
     [Approve] [Decline] [Full diff]
  └─ (Approve clicked) ✅ Done (task 4bffcf4a)
     Pull request: https://github.com/.../pull/991
```

- **Mention the bot** in a channel it has been invited to. It answers in a thread under your message.
- **Reply in that thread** to continue; no mention needed there. While a proposal is waiting, a reply continues that same task and the agent sees the earlier turns. Once a task has finished, a reply starts a new task in the same conversation, and that new task does not see the earlier ones.
- **Direct messages** work too, unless `SLACK_ALLOWED_CHANNELS` is set. A DM is one running conversation.
- **Approve or Decline** a proposed pull request with the buttons, or by replying `yes` / `no` in the thread. Only the person who asked, or someone in `APPROVERS`, can decide. With `REQUESTER_CAN_APPROVE=false` the person who asked can no longer approve their own proposal; someone in `APPROVERS` has to.
- **Approving is not code review.** It lets the agent open the pull request it prepared. The pull request then goes through the repository's normal review; the agent never merges.
- `status` shows where the current task is, and shows the buttons again if a proposal is waiting. `help` explains the commands.
- `repo=<url>`, `ref=<branch>` and `base=<branch>` in a message choose the repository, the branch to work on and the pull request's target branch.

A thread handles one request at a time. A second message while the first is still running gets a short "still on the previous message" reply.

## Creating the Slack app

1. On https://api.slack.com/apps choose **Create New App**, then **From a manifest**, pick the workspace, and paste:

   ```yaml
   display_information:
     name: ARI Agent
     description: Chat with the ARI on-call agent
   features:
     app_home:
       messages_tab_enabled: true
       messages_tab_read_only_enabled: false
     bot_user:
       display_name: ARI Agent
       always_online: true
   oauth_config:
     scopes:
       bot:
         - app_mentions:read
         - chat:write
         - channels:history
         - groups:history
         - im:history
         - users:read
         - reactions:write
   settings:
     event_subscriptions:
       bot_events:
         - app_mention
         - message.channels
         - message.groups
         - message.im
     interactivity:
       is_enabled: true
     socket_mode_enabled: true
     token_rotation_enabled: false
   ```

2. **Basic Information > App-Level Tokens > Generate Token and Scopes**, scope `connections:write`. That is `SLACK_APP_TOKEN` (`xapp-...`).
3. **Install App > Install to Workspace**. The Bot User OAuth Token is `SLACK_BOT_TOKEN` (`xoxb-...`).
4. Invite the bot to a channel with `/invite @ARI Agent`.

Socket Mode means the gateway opens an outbound WebSocket to Slack. It needs no public URL, no ingress and no request signing secret.

## Running it

```
pip install ".[slack]"
python -m chat_gateway
```

or the image built from `Dockerfile.chat-gateway` (`docker.io/insightfinderinc/ari-chat-gateway`), which runs as a non-root user and contains only the gateway's own dependencies. Run one replica, rolled out with `strategy: Recreate`: conversations and per-thread locks belong to one process, and Slack hands each event or button click to only one of the open connections, so a second replica would get replies and clicks for conversations it does not hold.

Set `SLACK_SESSION_FILE` so a restart does not lose the conversations. In the image, mount a volume at `/data` (it belongs to user 10001) and set `SLACK_SESSION_FILE=/data/sessions.json`; on Kubernetes that is a small PersistentVolumeClaim, with `fsGroup: 10001` when the volume would otherwise belong to root. The file holds only thread ids, Slack user ids and task ids. The LLM key and the GitHub token are never written to it; they are rebuilt from the environment when a saved conversation continues.

| Variable | Meaning |
|---|---|
| `SLACK_APP_TOKEN` | App-level token, `xapp-...`, scope `connections:write`. Required. |
| `SLACK_BOT_TOKEN` | Bot token, `xoxb-...`. Required. |
| `SLACK_ALLOWED_CHANNELS` | Comma-separated channel ids the bot answers in. Empty: every channel it is in. When set, direct messages are refused, because a DM is not one of the listed channels. |
| `SLACK_ALLOWED_USERS` | Comma-separated user ids allowed to use it. Empty: everyone who can see the bot. Someone not on the list who mentions the bot gets a short refusal; their plain replies in a bot thread are ignored. |
| `SLACK_PROGRESS_SECONDS` | Minimum seconds between edits of the "working" message. Default 5. |
| `SLACK_SESSION_FILE` | Where conversations are saved, for example `/data/sessions.json`. The directory must be writable. Empty: memory only, so a restart forgets every conversation. |
| `ARI_AGENT_URL`, `ARI_API_KEY` | The agent's A2A endpoint and one of the keys in its `AGENT_API_KEYS`. In the cluster use the service, not the public ingress. |
| `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL` | Sent with every request; the agent has no model key of its own. |
| `GITHUB_PAT` | Lets the agent push branches and open pull requests. |
| `DEFAULT_REPO_URL`, `DEFAULT_REPO_REF` | The repository used when a message names none. |
| `REPO_MAP` | Per-channel repositories, `C0123=https://github.com/org/a,C0456=https://github.com/org/b`. |
| `ALLOWED_REPO_HOSTS` | Hosts a `repo=` URL may point at. The token is never sent anywhere else. Default `github.com`. |
| `APPROVERS` | Slack user ids who may approve or decline anyone's proposal. |
| `REQUESTER_CAN_APPROVE` | Default `true`. Set `false` for a four-eyes rule: only someone in `APPROVERS` other than the person who asked can approve, the proposal names who that is, and the requester's own click or `yes` is refused. The requester can still decline their own request. The gateway refuses to start with `false` and an empty `APPROVERS`. |
| `LOG_LEVEL` | Default `INFO`. |

`.env.example.chat-gateway` lists them all. The gateway refuses to start when either Slack token is missing or has the wrong prefix, or when `LLM_API_KEY` is empty.

## Limits

- A conversation is kept for 24 hours after its last activity. With `SLACK_SESSION_FILE` set it survives restarts; without it a restart forgets every conversation.
- In a thread the gateway no longer recognises (older than 24 hours, or lost to a restart without `SLACK_SESSION_FILE`), the first reply without a mention gets a one-line note saying so, and nothing else happens: `yes`, `no` and `status` do nothing there, and the next message has to mention the bot, which starts a new conversation. The note is only posted in threads the bot has written in, and at most once per thread until the gateway restarts. Buttons on an old proposal still work, because each button carries the task id and the requester.
- A button on an older proposal in the same thread is refused once a newer task has taken its place, so a leftover Decline cannot cancel the current work.
- The gateway polls the agent every `POLL_SECONDS` (default 5) while a task runs. It does not need an inbound webhook.
- Slack shows at most about 3,000 characters per block, so long answers are split across several messages.

## Microsoft Teams

Same conversations, same approval rules, same agent settings (`ARI_AGENT_URL`, `LLM_*`, `GITHUB_PAT`, `APPROVERS`, `REQUESTER_CAN_APPROVE` and the rest of the table above). What differs:

- **Teams calls the gateway, not the other way round.** Teams has no Socket Mode: Microsoft POSTs every message to `https://<your host>/api/messages`, so the gateway needs a public HTTPS address (an ingress, or a tunnel while testing). It listens on `TEAMS_PORT` (default 3978) and answers `GET /healthz` without authentication. Every POST must carry a token issued by Microsoft for this bot; anything else gets 401. The gateway answers each POST at once and does the work afterwards, sending its replies as new messages, because Teams retries a request that takes longer than about 15 seconds.
- **In a channel, every message must mention the bot.** Teams only delivers channel messages that @mention the bot, including replies in a thread the bot started. In a one-to-one chat with the bot no mention is needed. A conversation is a channel thread, a group chat, or the one-to-one chat.
- **Proposals are Adaptive Cards** with Approve, Decline and Full diff buttons. After a decision the card keeps Full diff and says who decided. Teams cards have no confirmation dialog, so Approve acts on the first click.
- **People are identified by their Microsoft Entra object id** (`aadObjectId`), so `APPROVERS` and `TEAMS_ALLOWED_USERS` hold object ids, not names or e-mail addresses. An admin can look them up in the Entra admin center under Users.
- Messages from any tenant other than `TEAMS_TENANT_ID` are ignored.

### Creating the bot and the Teams app

1. In the Teams Developer Portal (https://dev.teams.microsoft.com) open **Tools > Bot management > New bot**. Copy the bot id: that is `TEAMS_APP_ID`.
2. Under **Client secrets** add a secret: that is `TEAMS_APP_PASSWORD`. It is shown once.
3. Under **Configure** set the endpoint address to `https://<your host>/api/messages`. Under **Channels** make sure Microsoft Teams is enabled.
4. `TEAMS_TENANT_ID` is your Microsoft 365 tenant id (Entra admin center > Overview, or the "Organization ID" on most Microsoft admin error pages).
5. Build the app package and install it:

   ```
   python scripts/teams_app_package.py --bot-id <TEAMS_APP_ID> --name "ARI Agent" --developer "<your org>" --website https://<your site> --out ari-agent-teams.zip
   ```

   In Teams open **Apps > Manage your apps > Upload an app**. **Upload a custom app** needs an admin to allow custom app upload for you (Teams admin center > Teams apps > Setup policies). Without it, choose **Submit an app to your org** and have an admin approve it in Teams admin center > Teams apps > Manage apps; it then appears under **Built for your org**. Creating the bot in step 1 also needs permission to register applications in Microsoft Entra.
6. To use it in a channel, choose **Add to a team** on the app and pick a standard channel.

Changing the code, the endpoint address or the secret does not need a new package. Changing the package (name, icons, commands) needs another upload, or another approval.

### Teams variables

| Variable | Meaning |
|---|---|
| `TEAMS_APP_ID` | The bot id, a GUID. Required. |
| `TEAMS_APP_PASSWORD` | A client secret of the bot. Required. |
| `TEAMS_TENANT_ID` | The Microsoft 365 tenant id, a GUID. Required. Messages from other tenants are ignored. |
| `TEAMS_ALLOWED_TEAMS` | Comma-separated team ids (the team's `id` or its Microsoft 365 group id) the bot answers in. Empty: every team it is added to. When set, chats outside a team are refused. |
| `TEAMS_ALLOWED_USERS` | Comma-separated Entra object ids allowed to use it. Empty: everyone who can reach the bot. |
| `TEAMS_PROGRESS_SECONDS` | Minimum seconds between edits of the "working" message. Default 5. |
| `TEAMS_SESSION_FILE` | Where conversations are saved, as `SLACK_SESSION_FILE`. Empty: memory only. |
| `TEAMS_HOST`, `TEAMS_PORT` | Where the gateway listens. Default `0.0.0.0` and `3978`. |
| `TEAMS_MESSAGES_PATH` | The path Teams posts to. Default `/api/messages`. Set it to match the ingress when the gateway shares a host with something else, e.g. `/teams/api/messages`. |

Run it like the Slack gateway: one replica, `strategy: Recreate`, the same image with the command `python -m chat_gateway teams`, plus a Service and an HTTPS ingress for `/api/messages`.
