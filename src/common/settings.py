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

    commit_author_name: str = "ARI Agent"
    commit_author_email: str = "support@insightfinder.com"
    agent_public_url: str = "http://localhost:8000"

    sandbox_root: str = "/tmp/ari-agent-sandbox"

    # Per-worker local mirror cache of target repos (see tools/repo_cache.py)
    # — kept separate from sandbox_root deliberately: sandbox_root is wiped
    # per-request by cleanup_workspace, this must survive across requests.
    repo_cache_root: str = "/var/lib/ari-agent/repo-cache"

    # Caps how many activities (clone/analyze/verify/... steps, across all
    # in-flight requests) this worker process runs at once. Temporal's SDK
    # default is 100 — fine for cheap activities, but each opencode-backed
    # step here can spawn a Node process plus, for `verify`, a real
    # go/maven/npm build inside a container sized at 2 CPU / 2Gi (see the
    # Helm chart's ariOncallAgent.worker.resources). Left uncapped, enough
    # concurrent requests landing on one worker pile up enough simultaneous
    # subprocesses to blow that limit — confirmed live via repeated
    # OOMKilled restarts. This default is deliberately conservative for
    # that container size; raise it only alongside more CPU/memory.
    max_concurrent_activities: int = 3
    interactive_max_concurrent_activities: int = 1
    temporal_interactive_task_queue: str = ""

    # Shared, persistent HOME for `verify`'s build/test subprocesses only
    # (mvn/go/npm, invoked via opencode's bash tool) — separate from
    # opencode's own per-request XDG_* isolation (see opencode_runtime.py),
    # so build-tool dependency caches (~/.m2, Go's module+build cache,
    # ~/.npm) stay warm across requests instead of a cold download every
    # single verify run. Confirmed live this was previously unbounded: no
    # cache anywhere meant every verify paid full dependency-resolution
    # cost every time, which was the dominant cost behind verify routinely
    # exceeding callers' patience timeouts (see capabilities.yaml's
    # verify.timeout_seconds). These tools are themselves designed for
    # concurrent access to one shared cache (content/coordinate-addressed
    # storage) — see max_concurrent_activities above for why concurrency
    # here is bounded anyway.
    build_cache_root: str = "/var/lib/ari-agent/build-cache"
    task_store_root: str = ""

    # Shared LRU-eviction thresholds for both repo_cache_root and
    # build_cache_root (see common/cache_gc.py) — both PVCs are small
    # (5Gi) relative to what they cache, so eviction isn't an edge case
    # here, it's expected to run regularly. Below `low_water` fraction
    # free, evict least-recently-used entries until back above
    # `high_water`. Same pair for both caches on purpose: no reason for
    # ops to reason about two independent knobs for what's the same
    # policy on two differently-shaped caches.
    cache_evict_low_water: float = 0.15
    cache_evict_high_water: float = 0.30

    opencode_bin: str = "opencode"

    skills_enabled: bool = True
    skills_dir: str = "/var/lib/ari-agent/skills"
    bundled_skills_dir: str = ""
    trust_repo_skills: bool = False

    # publish_pr's primary path is deterministic (git + a direct GitHub
    # REST API call, see capabilities/publish_pr.py) — this MCP server is
    # kept only as its fallback, still routed through a dedicated opencode
    # agent (`ari-publish`, tools/opencode_cli.py) when the primary path
    # fails.
    github_mcp_server_url: str = "https://api.githubcopilot.com/mcp/"

    log_level: str = "info"

    # OTLP/gRPC address of the InsightFinder trace server (TRACE_SERVER_URL,
    # the same variable mcp-service reads). Empty turns tracing off. Even
    # when set, a request is traced only if it carries a `trace` block —
    # which trace project its spans belong to is the caller's to say, not
    # this deployment's (see common/tracing.py).
    trace_server_url: str = ""

    def task_queue_for(self, config: dict | None) -> str:
        lane = ((config or {}).get("options") or {}).get("lane")
        if lane == "interactive" and self.interactive_task_queue():
            return self.interactive_task_queue()
        return self.temporal_task_queue

    def interactive_task_queue(self) -> str:
        if self.interactive_max_concurrent_activities <= 0:
            return ""
        return self.temporal_interactive_task_queue or f"{self.temporal_task_queue}-interactive"

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.agent_api_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
