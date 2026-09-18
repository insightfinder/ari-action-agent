"""
Runtime configuration for the ARI on-call agent process itself.

This is deliberately small: everything about a *request* (LLM key/model,
repo credentials, callback target, prompt overrides, ...) travels in the
request payload, never here (see api/schemas.py). Settings only covers what
this process needs to stand up: how to reach the externally hosted Temporal
cluster, which API keys it accepts on its own inbound endpoints, and where
its sandbox workspaces live.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Which agent this process is — selects which configs/<agent_kind>/
    # capabilities.yaml + tools.yaml to load (see config_paths.py) and which
    # agent-card content / opencode agent defs apply (see a2a_server/
    # agent_card.py, tools/opencode_cli.py). "oncall" (the default) resolves
    # exactly as it always has, at the root of configs/ — this setting only
    # matters for a second (or later) agent kind sharing this same image and
    # codebase. Every other setting below (temporal_namespace, agent_name,
    # agent_public_url, ...) is already generic per-deployment config — a
    # Jenkins deployment just sets different values for those same fields,
    # not new ones.
    agent_kind: str = "oncall"

    # Temporal connection (hosted elsewhere; namespace-scoped to this agent).
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "oncall-ari-agent"
    temporal_task_queue: str = "oncall-ari-queue"
    temporal_tls_enabled: bool = False
    temporal_api_key: str | None = None
    # Base64-encoded 32-byte AES-256 key. When set, every workflow/activity
    # payload (which carries a request's LLM api_key and repo pat/ssh_key)
    # is encrypted before Temporal persists it, instead of sitting as
    # plaintext-in-base64 in Temporal's event history/Web UI — see
    # orchestration/payload_codec.py. Unset (the local-dev default) means
    # no encryption, matching this setting's role as a deployment-level
    # secret, not something request-scoped.
    temporal_payload_encryption_key: str | None = None

    # Inbound auth: API keys this agent accepts on `x-api-key`.
    agent_api_keys: str = "change-me-dev-key"

    agent_name: str = "ARI On-Call Agent"
    agent_public_url: str = "http://localhost:8000"

    sandbox_root: str = "/tmp/ari-agent-sandbox"

    # Per-worker local mirror cache of target repos (see tools/repo_cache.py)
    # — kept separate from sandbox_root deliberately: sandbox_root is wiped
    # per-request by cleanup_workspace, this must survive across requests.
    repo_cache_root: str = "/var/lib/ari-agent/repo-cache"

    opencode_bin: str = "opencode"

    github_mcp_server_url: str = "https://api.githubcopilot.com/mcp/"

    log_level: str = "info"

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.agent_api_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
