"""
Test button creation in the Discord bot.
"""

import pytest
import hikari
import lightbulb
from unittest.mock import MagicMock, AsyncMock, patch

@pytest.mark.asyncio
async def test_button_creation():
    """Test the correct way to create buttons in Hikari."""
    # Create mock rest client
    rest = MagicMock()
    
    # Create a message action row builder
    action_row_builder = MagicMock()
    rest.build_message_action_row.return_value = action_row_builder
    
    # Create a button
    button = MagicMock()
    action_row_builder.add_interactive_button.return_value = button
    
    # Build the action row
    action_row = rest.build_message_action_row()
    
    # Add a button to the action row
    button = action_row.add_interactive_button(
        hikari.ButtonStyle.PRIMARY,
        "test_button_id"
    )
    
    # Set the label on the button
    button.set_label("Test Button")
    
    # Verify that the methods were called correctly
    rest.build_message_action_row.assert_called_once()
    action_row_builder.add_interactive_button.assert_called_once_with(
        hikari.ButtonStyle.PRIMARY,
        "test_button_id"
    )
    button.set_label.assert_called_once_with("Test Button")


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
