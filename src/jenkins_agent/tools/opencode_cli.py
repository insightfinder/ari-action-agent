"""Jenkins agent's own opencode wiring: which capability maps to which
scoped opencode agent, each agent's least-privilege permission block, and
the per-request opencode.json pointing at the target Jenkins's own MCP
server (https://plugins.jenkins.io/mcp-server/ — requires Jenkins
2.541.3+). The actual subprocess/event-parsing/tracing mechanics are
agent-kind-agnostic and live in common/opencode_runtime.py.

Neither Jenkins agent needs local shell or file access at all — there is
no repo, no code to edit; every real action goes through the Jenkins MCP
server. Locked down further than the oncall agents' opencode wiring
accordingly: edit and bash both denied outright, not just scoped.

Tool permissions are written out as explicit, exact tool names rather
than a `jenkins_*` wildcard. That wildcard was the original design and it
was actively wrong: opencode prefixes a remote MCP server's tools with
the server's config key (`_MCP_SERVER_NAME` below), so `jenkins_*`
matches the plugin's 15 read-only tools exactly as well as its 4 mutating
ones — it denied the read agent everything and left it unable to call
Jenkins at all. See docs/MEMORY.md, 2026-09-28. Naming the four mutating
tools is also strictly safer than a wildcard in the other direction: with
opencode's `--auto` auto-approving anything not explicitly denied, a
wildcard that stops matching (a renamed server key, a future opencode
naming change) silently denies nothing, while an exact name that stops
matching denies nothing but also grants nothing.

The plugin has no stop/cancel tool — see `_MUTATING_TOOLS` — which is why
tools/jenkins_rest.py keeps a direct-REST `stop_build` fallback.
"""

import json
from pathlib import Path
from typing import Any

from common.opencode_runtime import merge_traces, run_opencode as _run_opencode  # noqa: F401  (re-exported)

# Mirrors configs/jenkins/capabilities.yaml's capability→agent mapping.
# resolve_target/inspect/diagnose/propose_action all only ever need read
# access to Jenkins, so they share one scoped agent; execute_action is the
# only one that may call a mutating Jenkins MCP tool.
CAPABILITY_AGENTS = {
    "resolve_target": "ari-jenkins-read",
    "inspect": "ari-jenkins-read",
    "diagnose": "ari-jenkins-read",
    "propose_action": "ari-jenkins-read",
    "execute_action": "ari-jenkins-act",
}


def _no_delegation_tools() -> dict[str, bool]:
    """Same reasoning as the oncall agent's identical helper (see
    ari_oncall_agent/tools/opencode_cli.py) — opencode's built-in `task`
    subagent-delegation tool runs outside the delegating agent's own
    `permission` block entirely, so it's disabled explicitly on every
    agent here too, not just relied on as a global default."""
    return {"task": False}


# The key this server is registered under in `write_config`'s `mcp` block.
# opencode prefixes a remote MCP server's tools with it (documented at
# https://opencode.ai/docs/mcp-servers: "MCP server tools are registered
# with server name as prefix"), so it is also the prefix every permission
# entry below has to use. Kept as one constant precisely so renaming the
# server cannot silently detach the permissions from the tools.
_MCP_SERVER_NAME = "jenkins"

# The Jenkins MCP Server plugin's complete set of non-read-only tools, from
# its own `readOnlyHint`/`destructiveHint` annotations over a live
# `tools/list` call (19 tools, 15 read-only) — see docs/MEMORY.md,
# 2026-09-18 and 2026-09-28. `replayBuild` is the one worth naming twice:
# its required `mainScript` argument is arbitrary Pipeline Groovy, executed
# on the controller, so it is a code-execution tool wearing a build's
# clothes. There is deliberately no stop/cancel entry here because the
# plugin exposes none.
_MUTATING_TOOLS = ("triggerBuild", "updateBuild", "rebuildBuild", "replayBuild")


def _jenkins_tools(**overrides: bool) -> dict[str, bool]:
    """Every mutating Jenkins tool denied by exact name, with any named
    tool re-allowed. Read-only tools are left unmentioned on purpose: an
    entry for each would have to be updated whenever the plugin adds one,
    and a forgotten entry there costs a capability its data. Forgetting an
    entry *here* is the failure worth engineering against, and the list it
    comes from is fixed by the plugin, not by us."""
    tools = {f"{_MCP_SERVER_NAME}_{name}": False for name in _MUTATING_TOOLS}
    tools.update({f"{_MCP_SERVER_NAME}_{name}": v for name, v in overrides.items()})
    return tools


_AGENT_DEFS = {
    "ari-jenkins-read": {
        "description": "Resolves targets, inspects status, diagnoses failures, and proposes actions "
        "against Jenkins. Read-only — cannot call a mutating Jenkins tool.",
        "mode": "primary",
        "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
        # Denying the mutating tools by name is the belt; execute_action
        # being the only capability mapped to ari-jenkins-act (below) is
        # the suspenders. The read-only tools must stay reachable — this
        # agent has no other way to see Jenkins at all.
        "tools": {**_no_delegation_tools(), **_jenkins_tools()},
    },
    "ari-jenkins-act": {
        "description": "The only agent allowed to trigger a Jenkins build. Runs strictly from a "
        "confirmed pending_action — see capabilities/execute_action.py.",
        "mode": "primary",
        "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
        # Only the one mutation execute_action actually performs. The other
        # three stay denied: nothing in this agent kind proposes them, and
        # `replayBuild` in particular would hand a confirmed pending_action
        # a route to arbitrary Groovy on the controller. Stopping a build
        # does not appear here at all — that path is jenkins_rest.stop_build,
        # because the plugin has no stop tool to allow.
        "tools": {**_no_delegation_tools(), **_jenkins_tools(triggerBuild=True)},
    },
}


def _auth_header(auth: dict) -> str:
    import base64

    auth_type = auth.get("type", "api_token")
    if auth_type in ("api_token", "basic"):
        raw = f"{auth['user']}:{auth['token']}".encode()
        return f"Basic {base64.b64encode(raw).decode()}"
    if auth_type == "bearer":
        return f"Bearer {auth['token']}"
    raise ValueError(f"unsupported jenkins auth.type: {auth_type!r}")


def tls_env(jenkins: dict, workdir: str) -> dict[str, str]:
    """opencode is a Node CLI, so a per-request CA bundle/TLS-verification
    override (design requirement — internal Jenkins instances often run
    behind a private CA or self-signed cert) has to travel as Node's own
    env vars for the opencode subprocess, not opencode.json — passed as
    `extra_env` to common.opencode_runtime.run_opencode. Not yet verified
    against a real self-signed Jenkins; NODE_EXTRA_CA_CERTS/
    NODE_TLS_REJECT_UNAUTHORIZED are Node's own documented mechanisms, but
    this hasn't been exercised against opencode's actual MCP HTTP client.
    """
    env: dict[str, str] = {}
    if jenkins.get("ca_bundle"):
        ca_path = Path(workdir) / "jenkins-ca.pem"
        ca_path.write_text(jenkins["ca_bundle"])
        env["NODE_EXTRA_CA_CERTS"] = str(ca_path)
    if jenkins.get("verify_tls") is False:
        env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    return env


def write_config(workdir: str, llm: dict, jenkins: dict) -> str:
    """Writes a per-request opencode.json into the (repo-less) sandbox
    workspace. Called once, before the first opencode invocation. Torn
    down with the rest of the sandbox in cleanup_workspace.
    """
    config: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {llm["provider"]: {"options": {"apiKey": llm["api_key"]}}},
        # Defence in depth under the per-agent blocks: same exact-name
        # denies, so a capability that reaches opencode without a resolved
        # agent still cannot mutate Jenkins. This must not be a wildcard —
        # a global `jenkins_*: False` overrides nothing but shadows
        # everything, which is the bug fixed on 2026-09-28.
        "tools": {"task": False, **_jenkins_tools()},
        "agent": _AGENT_DEFS,
        "mcp": {
            "jenkins": {
                "type": "remote",
                "url": f"{jenkins['base_url'].rstrip('/')}/mcp-server/mcp",
                "enabled": True,
                "oauth": False,
                "headers": {"Authorization": _auth_header(jenkins["auth"])},
            }
        },
    }

    path = Path(workdir) / "opencode.json"
    path.write_text(json.dumps(config, indent=2))
    return str(path)


async def run_opencode(ctx, instruction: str, *, continue_session: bool | None = None) -> dict:
    """Thin wrapper over common.opencode_runtime.run_opencode: resolves
    this agent kind's opencode agent name for the calling capability, plus
    this request's TLS env overrides. Every capability calls it the same
    way:

        result = await ctx.tool("opencode")(ctx, "<instruction>")
    """
    agent = CAPABILITY_AGENTS[ctx.capability]
    env = tls_env(ctx.config["jenkins"], ctx.repo_dir)
    return await _run_opencode(ctx, instruction, agent, continue_session=continue_session, extra_env=env or None)
