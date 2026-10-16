import asyncio
import logging
import os
import sys

from .config import ConfigError


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        import aiohttp  # noqa: F401
        import slack_bolt  # noqa: F401
    except ImportError as exc:
        print(f"error: the Slack gateway needs the 'slack' extra ({exc.name} is missing)", file=sys.stderr)
        return 2
    from .slack import run

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
