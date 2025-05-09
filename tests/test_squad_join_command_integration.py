"""
Integration test for the squad join command in the Discord bot.
"""

import pytest
import hikari
import lightbulb
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from bot.plugins.squads import squads_join


@pytest.mark.asyncio
async def test_squad_join_integration():
    """Test the entire squad join command flow."""
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
    label_mock = MagicMock()
    container_mock = MagicMock()
    
    label_mock.add_to_container.return_value = container_mock
    button_mock.set_label.return_value = label_mock
    action_row_mock.add_button.return_value = button_mock
    ctx.bot.rest.build_message_action_row.return_value = action_row_mock
    
    # Mock the stream method
    stream_mock = AsyncMock()
    ctx.bot.stream.return_value.__aenter__.return_value = stream_mock
    # Make the stream_mock raise StopAsyncIteration when used in an async for loop
    stream_mock.__aiter__.return_value = AsyncMock()
    stream_mock.__aiter__.return_value.__anext__.side_effect = StopAsyncIteration()
    
    # Run the command
    await squads_join(ctx)
    
    # Verify that build_message_action_row was called
    ctx.bot.rest.build_message_action_row.assert_called_once()
    
    # Verify that add_button was called with the correct arguments
    action_row_mock.add_button.assert_called_once_with(
        hikari.ButtonStyle.PRIMARY,
        "join_squad_1"
    )
    
    # Verify that set_label was called with the correct arguments
    button_mock.set_label.assert_called_once_with("Test Squad")
    
    # Verify that add_to_container was called
    label_mock.add_to_container.assert_called_once()
    
    # Verify that respond was called with the correct arguments
    ctx.respond.assert_called_once()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
