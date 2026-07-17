"""ASGI entry point for the smoke harness.

Identical to ``main:app`` except the legacy admin's Discord REST client is
pointed at the harness's local mock (HARNESS_DISCORD_API_BASE_URL) before the
app imports, so /bot-admin pages render fully offline. No app code changes.
"""

import os

from smarter_dev.web import discord_admin_client
from smarter_dev.web.admin import discord as legacy_admin_discord

_mock_discord_base_url = os.environ["HARNESS_DISCORD_API_BASE_URL"]

# Legacy /bot-admin pages (still mounted this phase) use the old DiscordClient.
_original_discord_client_init = legacy_admin_discord.DiscordClient.__init__


def _mock_redirecting_init(self, bot_token: str) -> None:
    _original_discord_client_init(self, bot_token)
    self.base_url = _mock_discord_base_url


legacy_admin_discord.DiscordClient.__init__ = _mock_redirecting_init

# Skrift /admin/bot guild pages use the new DiscordAdminClient; point its
# base URL at the same mock so those pages also render fully offline.
_original_admin_client_factory = discord_admin_client.get_admin_discord_client


def _mock_admin_client() -> discord_admin_client.DiscordAdminClient:
    client = _original_admin_client_factory()
    client.api_base = _mock_discord_base_url
    return client


discord_admin_client.get_admin_discord_client = _mock_admin_client

from main import app  # noqa: E402,F401  (must import after the patch above)
