import asyncio
import importlib
import logging
import os
import sys

from .config import ConfigError

PLATFORMS = {
    "slack": ("chat_gateway.slack", ("aiohttp", "slack_bolt"), "slack"),
    "teams": ("chat_gateway.teams", ("aiohttp", "microsoft_agents.hosting.aiohttp", "microsoft_agents.authentication.msal"), "teams"),
}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = sys.argv[1:] if argv is None else argv
    platform = (args[0] if args else os.environ.get("CHAT_PLATFORM", "slack")).strip().lower()
    if platform not in PLATFORMS:
        print(f"error: unknown chat platform {platform!r}; use one of: {', '.join(PLATFORMS)}", file=sys.stderr)
        return 2
    module_name, requirements, extra = PLATFORMS[platform]
    for requirement in requirements:
        try:
            importlib.import_module(requirement)
        except ImportError:
            print(f"error: the {platform} gateway needs the '{extra}' extra ({requirement} is missing)", file=sys.stderr)
            return 2
    run = importlib.import_module(module_name).run
    try:
        asyncio.run(run())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
