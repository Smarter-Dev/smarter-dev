"""ASGI entry point for the smoke harness.

Identical to ``main:app`` except the legacy admin's Discord REST client is
pointed at the harness's local mock (HARNESS_DISCORD_API_BASE_URL) before the
app imports, so /bot-admin pages render fully offline. No app code changes.
"""

import os

from smarter_dev.web.admin import discord as legacy_admin_discord

_mock_discord_base_url = os.environ["HARNESS_DISCORD_API_BASE_URL"]
_original_discord_client_init = legacy_admin_discord.DiscordClient.__init__


def _mock_redirecting_init(self, bot_token: str) -> None:
    _original_discord_client_init(self, bot_token)
    self.base_url = _mock_discord_base_url


legacy_admin_discord.DiscordClient.__init__ = _mock_redirecting_init

from main import app  # noqa: E402,F401  (must import after the patch above)
