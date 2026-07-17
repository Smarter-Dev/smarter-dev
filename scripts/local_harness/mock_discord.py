"""Tiny in-process mock of the Discord REST API for the admin pages.

The Skrift ``/admin/bot`` guild pages fetch guilds, roles, and channels from
Discord. ``harness_app`` points ``DiscordAdminClient.api_base`` here so every
page can render fully offline. Only the read endpoints the admin uses are
implemented.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.local_harness import config

_GUILD_SUMMARY = {
    "id": config.GUILD_ID,
    "name": "Harness Guild",
    "icon": None,
    "owner": False,
}

_GUILD_DETAIL = {
    "id": config.GUILD_ID,
    "name": "Harness Guild",
    "icon": None,
    "owner_id": config.USER_ID,
    "approximate_member_count": 42,
    "description": "Local smoke-harness guild",
}

_ROLES = [
    {
        "id": config.SQUAD_ROLE_ID,
        "name": "Harness Squad Role",
        "color": 0x00FF00,
        "position": 2,
        "permissions": "0",
        "managed": False,
        "mentionable": True,
    },
    {
        "id": config.OTHER_ROLE_ID,
        "name": "Harness Other Role",
        "color": 0,
        "position": 1,
        "permissions": "0",
        "managed": False,
        "mentionable": False,
    },
]

_CHANNELS = [
    {
        "id": config.TEXT_CHANNEL_ID,
        "name": "harness-general",
        "type": 0,
        "position": 0,
        "parent_id": None,
        "topic": "general",
    },
    {
        "id": config.FORUM_CHANNEL_ID,
        "name": "harness-forum",
        "type": 15,
        "position": 1,
        "parent_id": None,
        "topic": "forum",
    },
]

_GUILD_PATH = re.compile(r"^/guilds/(?P<guild_id>\d+)(?P<rest>/roles|/channels)?$")


class _MockDiscordHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        path = self.path.split("?", 1)[0]
        if path == "/users/@me/guilds":
            self._send_json([_GUILD_SUMMARY])
            return
        match = _GUILD_PATH.match(path)
        if match is None or match.group("guild_id") != config.GUILD_ID:
            self._send_json({"message": "Unknown Guild", "code": 10004}, status=404)
            return
        if match.group("rest") == "/roles":
            self._send_json(_ROLES)
        elif match.group("rest") == "/channels":
            self._send_json(_CHANNELS)
        else:
            self._send_json(_GUILD_DETAIL)

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # keep harness output clean


def start_mock_discord_server(port: int) -> ThreadingHTTPServer:
    """Start the mock server on a daemon thread and return it."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _MockDiscordHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    started = start_mock_discord_server(config.MOCK_DISCORD_PORT)
    print(f"mock discord API listening on {config.MOCK_DISCORD_BASE_URL}")
    threading.Event().wait()
