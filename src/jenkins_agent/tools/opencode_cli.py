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
than a `jenkins_*` wildcard. The plugin has no stop/cancel tool, which is
why tools/jenkins_rest.py keeps a direct-REST `stop_build` fallback.
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


_MCP_SERVER_NAME = "jenkins"

_MUTATING_TOOLS = ("triggerBuild", "updateBuild", "rebuildBuild", "replayBuild")


def _jenkins_tools(**overrides: bool) -> dict[str, bool]:
    tools = {f"{_MCP_SERVER_NAME}_{name}": False for name in _MUTATING_TOOLS}
    tools.update({f"{_MCP_SERVER_NAME}_{name}": v for name, v in overrides.items()})
    return tools


_AGENT_DEFS = {
    "ari-jenkins-read": {
        "description": "Resolves targets, inspects status, diagnoses failures, and proposes actions "
        "against Jenkins. Read-only — cannot call a mutating Jenkins tool.",
        "mode": "primary",
        "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
        "tools": {**_no_delegation_tools(), **_jenkins_tools()},
    },
    "ari-jenkins-act": {
        "description": "The only agent allowed to trigger a Jenkins build. Runs strictly from a "
        "confirmed pending_action — see capabilities/execute_action.py.",
        "mode": "primary",
        "permission": {"edit": "deny", "bash": "deny", "webfetch": "deny"},
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
