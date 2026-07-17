"""ASGI entry point for the smoke harness.

Identical to ``main:app`` except the admin Discord REST client is pointed at
the harness's local mock (HARNESS_DISCORD_API_BASE_URL) before the app imports,
so the ``/admin/bot`` guild pages render fully offline. No app code changes.
"""

import os

from smarter_dev.web import discord_admin_client

_mock_discord_base_url = os.environ["HARNESS_DISCORD_API_BASE_URL"]

# Skrift /admin/bot guild pages use DiscordAdminClient; point its base URL at
# the mock so those pages render fully offline.
_original_admin_client_factory = discord_admin_client.get_admin_discord_client


def _mock_admin_client() -> discord_admin_client.DiscordAdminClient:
    client = _original_admin_client_factory()
    client.api_base = _mock_discord_base_url
    return client


discord_admin_client.get_admin_discord_client = _mock_admin_client

from main import app  # noqa: E402,F401  (must import after the patch above)
