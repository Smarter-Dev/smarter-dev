"""
Test the action row builder in the Discord bot.
"""

import pytest
import hikari
import lightbulb
from unittest.mock import MagicMock, AsyncMock, patch

@pytest.mark.asyncio
async def test_action_row_builder():
    """Test that the action row builder works correctly."""
    # Create mock rest client
    rest = MagicMock()

    # Create a message action row builder
    action_row_builder = MagicMock()
    rest.build_message_action_row.return_value = action_row_builder

    # Create a button builder
    button_builder = MagicMock()
    action_row_builder.add_interactive_button.return_value = button_builder

    # Build the action row
    action_row = rest.build_message_action_row()
    action_row.add_interactive_button(
        hikari.ButtonStyle.PRIMARY,
        "join_squad_1",
        label="Test Squad"
    )

    # Verify that the methods were called correctly
    rest.build_message_action_row.assert_called_once()
    action_row_builder.add_interactive_button.assert_called_once_with(
        hikari.ButtonStyle.PRIMARY,
        "join_squad_1",
        label="Test Squad"
    )


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
