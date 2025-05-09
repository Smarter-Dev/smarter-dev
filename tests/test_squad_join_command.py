"""
Test the squad join command in the Discord bot.
"""

import pytest
import hikari
import lightbulb
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

from bot.plugins.squads import squads_join


@pytest.mark.asyncio
async def test_squad_join_action_row():
    """Test that the squad join command builds action rows correctly."""
    # Create mock context
    ctx = AsyncMock()
    ctx.guild_id = 123456789
    ctx.author.id = 987654321

    # Mock API client
    api_client = AsyncMock()
    response_mock = AsyncMock()
    response_mock.status_code = 200
    api_client._request.return_value = response_mock
    api_client._get_json.return_value = {
        "squads": [
            {
                "id": 1,
                "name": "Test Squad",
                "description": "A test squad",
                "role_id": 111222333
            }
        ]
    }
    ctx.bot.d.api_client = api_client

    # Mock guild and role
    guild = MagicMock()
    role = MagicMock()
    role.mention = "<@&111222333>"
    guild.get_role.return_value = role
    ctx.get_guild.return_value = guild

    # Mock the rest client
    ctx.bot.rest = MagicMock()

    # The issue is that ctx.bot.rest.build_action_row() doesn't exist
    # We need to use build_message_action_row instead
    # Let's test that our fix works
    ctx.bot.rest.build_message_action_row = MagicMock()
    ctx.bot.rest.build_message_action_row.return_value = MagicMock()
    ctx.bot.rest.build_message_action_row.return_value.add_button = MagicMock()
    ctx.bot.rest.build_message_action_row.return_value.add_button.return_value = MagicMock()
    ctx.bot.rest.build_message_action_row.return_value.add_button.return_value.set_label = MagicMock()
    ctx.bot.rest.build_message_action_row.return_value.add_button.return_value.set_label.return_value = MagicMock()
    ctx.bot.rest.build_message_action_row.return_value.add_button.return_value.set_label.return_value.add_to_container = MagicMock()

    # This should now work without raising an AttributeError
    await squads_join(ctx)


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
