from urllib.parse import urlsplit

from pydantic import field_validator

from chat_gateway.config import ConfigError, GatewaySettings

MCP_SECRET_FIELDS = frozenset({"github_oauth_client_secret"})

APPROVAL_CONFIRMATIONS = ("dialog", "browser", "app")


class McpSettings(GatewaySettings):
    mcp_public_url: str = ""
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8080
    mcp_path: str = "/mcp"
    mcp_state_file: str = ""
    mcp_session_file: str = ""
    mcp_approvers: str = ""
    mcp_allowed_users: str = ""
    mcp_allowed_github_orgs: str = ""
    mcp_tool_wait_seconds: float = 25.0
    mcp_poll_seconds: float = 3.0
    mcp_access_token_seconds: int = 3600
    mcp_refresh_token_seconds: int = 30 * 86400
    mcp_approval_confirmation: str = "dialog"
    mcp_max_clients: int = 5000
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    github_api_url: str = "https://api.github.com"
    github_web_url: str = "https://github.com"

    @field_validator("github_oauth_client_id", "github_oauth_client_secret", mode="before")
    @classmethod
    def strip_oauth(cls, value):
        return value.strip() if isinstance(value, str) else value

    def public_url(self) -> str:
        return self.mcp_public_url.rstrip("/")

    def public_host(self) -> str:
        return urlsplit(self.public_url()).netloc

    def resource_url(self) -> str:
        return self.public_url() + self.mcp_path

    def approval_confirmation(self) -> str:
        return self.mcp_approval_confirmation.strip().lower()

    def approver_set(self) -> set[str]:
        return {a.strip().lower() for a in self.mcp_approvers.split(",") if a.strip()}

    def allowed_user_set(self) -> set[str]:
        return {u.strip().lower() for u in self.mcp_allowed_users.split(",") if u.strip()}

    def allowed_org_list(self) -> list[str]:
        return [o.strip() for o in self.mcp_allowed_github_orgs.split(",") if o.strip()]

    def check(self) -> None:
        parts = urlsplit(self.public_url())
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise ConfigError("MCP_PUBLIC_URL must be the gateway's public address, e.g. https://ari-mcp.example.com")
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1"):
            raise ConfigError("MCP_PUBLIC_URL must use https unless it is localhost")
        if parts.path not in ("", "/"):
            raise ConfigError("MCP_PUBLIC_URL must not contain a path; set MCP_PATH for the endpoint")
        if not self.mcp_path.startswith("/"):
            raise ConfigError("MCP_PATH must start with /")
        if not self.github_oauth_client_id or not self.github_oauth_client_secret:
            raise ConfigError("GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET are required: people sign in with GitHub")
        if not self.allowed_user_set() and not self.allowed_org_list():
            raise ConfigError(
                "set MCP_ALLOWED_USERS or MCP_ALLOWED_GITHUB_ORGS; without either, anyone with a GitHub account could use the agent"
            )
        if self.mcp_tool_wait_seconds <= 0 or self.mcp_tool_wait_seconds > 50:
            raise ConfigError("MCP_TOOL_WAIT_SECONDS must be between 0 and 50; clients give up on longer tool calls")
        if self.approval_confirmation() not in APPROVAL_CONFIRMATIONS:
            raise ConfigError(f"MCP_APPROVAL_CONFIRMATION must be one of: {', '.join(APPROVAL_CONFIRMATIONS)}")
        if self.mcp_access_token_seconds < 60:
            raise ConfigError("MCP_ACCESS_TOKEN_SECONDS must be at least 60")
        self.llm_config()
        self.options()
        self.jenkins_config()
        self.check_approval_policy()

    def masked(self) -> dict:
        shown = super().masked()
        for name in MCP_SECRET_FIELDS:
            value = shown.get(name)
            if value:
                shown[name] = value[:4] + "…" if len(value) > 4 else "…"
        return shown
