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

    # Temporal connection (hosted elsewhere; namespace-scoped to this agent).
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "oncall-ari-agent"
    temporal_task_queue: str = "oncall-ari-fix-queue"
    temporal_tls_enabled: bool = False
    temporal_api_key: str | None = None

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
