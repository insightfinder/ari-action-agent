import json
from typing import Any
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5"}

SECRET_FIELDS = frozenset({"ari_api_key", "llm_api_key", "github_pat"})


class ConfigError(ValueError):
    pass


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    ari_agent_url: str = "http://localhost:8000"
    ari_api_key: str = ""
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = ""
    github_pat: str = ""
    default_repo_url: str = ""
    default_repo_ref: str = ""
    repo_map: str = ""
    allowed_repo_hosts: str = "github.com"
    approvers: str = ""
    requester_can_approve: bool = True
    options_json: str = "{}"
    poll_seconds: float = 5.0
    wait_seconds: float = 3600.0
    agent_outage_seconds: float = 600.0
    request_timeout_seconds: float = 60.0

    @field_validator("ari_api_key", "llm_api_key", "github_pat", mode="before")
    @classmethod
    def strip_secret(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    def llm_config(self) -> dict[str, Any]:
        if not self.llm_api_key:
            raise ConfigError("LLM_API_KEY is not set")
        model = self.llm_model or DEFAULT_MODELS.get(self.llm_provider, "")
        if not model:
            raise ConfigError(f"LLM_MODEL is not set and no default model is known for provider {self.llm_provider!r}")
        llm: dict[str, Any] = {"provider": self.llm_provider, "api_key": self.llm_api_key, "model": model}
        if self.llm_base_url:
            llm["base_url"] = self.llm_base_url
        return llm

    def repo_map_entries(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        for entry in self.repo_map.split(","):
            if "=" not in entry:
                continue
            key, url = entry.split("=", 1)
            if key.strip() and url.strip():
                entries[key.strip()] = url.strip()
        return entries

    def allowed_hosts(self) -> set[str]:
        hosts = {h.strip().lower() for h in self.allowed_repo_hosts.split(",") if h.strip()}
        for url in [self.default_repo_url, *self.repo_map_entries().values()]:
            host = (urlsplit(url).hostname or "").lower()
            if host:
                hosts.add(host)
        return hosts

    def check_repo_url(self, repo_url: str) -> str:
        parts = urlsplit(repo_url)
        if parts.username or parts.password:
            raise ConfigError("repository URL must not embed credentials")
        if parts.scheme != "https":
            raise ConfigError(f"repository URL must use https, not {parts.scheme or 'no scheme'}")
        host = (parts.hostname or "").lower()
        if host not in self.allowed_hosts():
            raise ConfigError(f"repository host {host!r} is not allowed; set ALLOWED_REPO_HOSTS to permit it")
        return repo_url

    def repo_config(self, url: str = "", ref: str = "", *, require_pat: bool = False) -> dict[str, Any]:
        repo_url = url or self.default_repo_url
        if not repo_url:
            raise ConfigError("no repository: pass repo=<url> in the message or set DEFAULT_REPO_URL")
        self.check_repo_url(repo_url)
        if require_pat and not self.github_pat:
            raise ConfigError("GITHUB_PAT is not set; the agent cannot push a branch or open a pull request without it")
        repo: dict[str, Any] = {"url": repo_url}
        repo_ref = ref or self.default_repo_ref
        if repo_ref:
            repo["ref"] = repo_ref
        if self.github_pat:
            repo["pat"] = self.github_pat
        return repo

    def repo_for(self, thread_key: str) -> str:
        entries = self.repo_map_entries()
        if thread_key in entries:
            return entries[thread_key]
        channel = thread_key.split(":", 1)[0]
        return entries.get(channel, "")

    def options(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.options_json or "{}")
        except json.JSONDecodeError as exc:
            raise ConfigError(f"OPTIONS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigError("OPTIONS_JSON must be a JSON object")
        return parsed

    def approver_set(self) -> set[str]:
        return {a.strip() for a in self.approvers.split(",") if a.strip()}

    def check_approval_policy(self) -> None:
        if not self.requester_can_approve and not self.approver_set():
            raise ConfigError("REQUESTER_CAN_APPROVE is false but APPROVERS is empty, so nobody could approve a proposal")

    def masked(self) -> dict[str, Any]:
        shown: dict[str, Any] = {}
        for name, value in self.model_dump().items():
            if name in SECRET_FIELDS and value:
                shown[name] = value[:4] + "…" if len(value) > 4 else "…"
            else:
                shown[name] = value
        return shown
