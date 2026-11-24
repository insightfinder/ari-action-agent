import logging
import os
import sys

from chat_gateway.config import ConfigError

from .oauth import StateFile
from .settings import McpSettings

USAGE = "usage: python -m mcp_gateway [serve | revoke <github login>]"


def serve(settings: McpSettings) -> int:
    import uvicorn

    from .server import build_app

    settings.check()
    app, _ = build_app(settings)
    logging.getLogger("mcp_gateway").info(
        "ARI MCP gateway on %s (listening %s:%s), agent %s", settings.resource_url(), settings.mcp_host, settings.mcp_port, settings.ari_agent_url
    )
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port, proxy_headers=True, log_level=os.environ.get("LOG_LEVEL", "info").lower())
    return 0


def revoke(settings: McpSettings, login: str) -> int:
    if not settings.mcp_state_file:
        print("error: MCP_STATE_FILE is not set, so there is nothing stored to revoke", file=sys.stderr)
        return 2
    removed = StateFile(settings.mcp_state_file).revoke_subject(login)
    print(f"revoked {removed} token(s) of {login}; restart the gateway so it drops tokens it already loaded")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "serve"
    try:
        settings = McpSettings()
        if command == "serve" and len(args) <= 1:
            return serve(settings)
        if command == "revoke" and len(args) == 2:
            return revoke(settings, args[1])
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
