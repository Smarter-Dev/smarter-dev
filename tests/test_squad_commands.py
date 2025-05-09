"""
Test the squad commands in the Discord bot.
"""

import pytest
import hikari
import lightbulb
from unittest.mock import MagicMock, AsyncMock, patch

from bot.plugins.squads import squads_join, squads_leave


@pytest.mark.asyncio
async def test_squads_join_command():
    """Test the squads join command."""
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

    # Mock build_message_action_row
    action_row_mock = MagicMock()
    button_mock = MagicMock()
    ctx.bot.rest.build_message_action_row.return_value = action_row_mock
    action_row_mock.add_interactive_button.return_value = button_mock
    button_mock.set_label.return_value = button_mock

    # Run the command
    await squads_join(ctx)

    # Verify that build_message_action_row was called
    ctx.bot.rest.build_message_action_row.assert_called_once()

    # Verify that add_interactive_button was called with the correct arguments
    action_row_mock.add_interactive_button.assert_called_once_with(
        hikari.ButtonStyle.PRIMARY,
        "join_squad_1",
        label="Test Squad"
    )

    # Verify that respond was called
    ctx.respond.assert_called_once()


@pytest.mark.asyncio
async def test_squads_leave_command():
    """Test the squads leave command."""
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
                "role_id": 111222333,
                "guild_id": 123456789  # Match the guild_id we set above
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

    # Mock build_message_action_row
    action_row_mock = MagicMock()
    button_mock = MagicMock()
    ctx.bot.rest.build_message_action_row.return_value = action_row_mock
    action_row_mock.add_interactive_button.return_value = button_mock
    button_mock.set_label.return_value = button_mock

    # Run the command
    await squads_leave(ctx)

    # Verify that build_message_action_row was called
    ctx.bot.rest.build_message_action_row.assert_called_once()

    # Verify that add_interactive_button was called with the correct arguments
    action_row_mock.add_interactive_button.assert_called_once_with(
        hikari.ButtonStyle.DANGER,
        "leave_squad_1",
        label="Test Squad"
    )

    # Verify that respond was called
    ctx.respond.assert_called_once()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
