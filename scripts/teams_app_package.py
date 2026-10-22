import argparse
import json
import re
import struct
import sys
import zipfile
import zlib
from pathlib import Path

GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ACCENT = (0x4F, 0x6B, 0xED)
SCOPES = ["personal", "team", "groupChat"]


def png(size: int, background: tuple[int, int, int, int], foreground: tuple[int, int, int, int]) -> bytes:
    center = (size - 1) / 2
    outer, inner = size * 0.36, size * 0.2
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            distance = ((x - center) ** 2 + (y - center) ** 2) ** 0.5
            row.extend(foreground if inner <= distance <= outer else background)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")


def manifest(bot_id: str, name: str, developer: str, website: str, version: str) -> dict:
    commands = [
        {"title": "help", "description": "What I can do and how to ask"},
        {"title": "status", "description": "Where the current task is"},
    ]
    return {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
        "manifestVersion": "1.17",
        "version": version,
        "id": bot_id,
        "developer": {"name": developer, "websiteUrl": website, "privacyUrl": website, "termsOfUseUrl": website},
        "name": {"short": name[:30], "full": f"{name} - on-call engineering agent"[:100]},
        "description": {
            "short": "Ask the ARI agent questions or have it prepare pull requests."[:80],
            "full": (
                "Mention the agent with a question or a change request. It answers in the conversation, and when it "
                "prepares a pull request it shows the proposal with Approve and Decline buttons. Nothing is opened "
                "on GitHub until someone approves."
            ),
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#{:02X}{:02X}{:02X}".format(*ACCENT),
        "bots": [
            {
                "botId": bot_id,
                "scopes": SCOPES,
                "supportsFiles": False,
                "isNotificationOnly": False,
                "commandLists": [{"scopes": SCOPES, "commands": commands}],
            }
        ],
        "permissions": ["identity", "messageTeamMembers"],
        "validDomains": [],
    }


def build(bot_id: str, out: Path, *, name: str, developer: str, website: str, version: str) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest(bot_id, name, developer, website, version), indent=2))
        archive.writestr("color.png", png(192, (*ACCENT, 255), (255, 255, 255, 255)))
        archive.writestr("outline.png", png(32, (0, 0, 0, 0), (255, 255, 255, 255)))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Teams app package (manifest.json and icons) for the chat gateway's bot.")
    parser.add_argument("--bot-id", required=True, help="the bot's id (TEAMS_APP_ID)")
    parser.add_argument("--out", default="ari-agent-teams.zip")
    parser.add_argument("--name", default="ARI Agent")
    parser.add_argument("--developer", default="ARI agent")
    parser.add_argument("--website", default="https://github.com/insightfinder/ari-action-agent")
    parser.add_argument("--version", default="1.0.0")
    args = parser.parse_args(argv)
    if not GUID.match(args.bot_id):
        print("error: --bot-id must be the bot's GUID from the Teams Developer Portal", file=sys.stderr)
        return 2
    if not args.website.startswith("https://"):
        print("error: --website must be an https:// URL", file=sys.stderr)
        return 2
    path = build(args.bot_id, Path(args.out), name=args.name, developer=args.developer, website=args.website, version=args.version)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
