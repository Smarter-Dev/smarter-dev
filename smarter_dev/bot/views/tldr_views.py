"""Interactive TLDR views for Discord bot.

This module provides interactive UI components for TLDR summary sharing.
"""

from __future__ import annotations

import hikari
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class TLDRShareView:
    """View with share button for TLDR command."""
    
    def __init__(self, summary_content: str, user_id: str, message_count: int):
        """Initialize the TLDR share view.
        
        Args:
            summary_content: The generated summary text
            user_id: User ID who requested the summary
            message_count: Number of messages that were summarized
        """
        self.summary_content = summary_content
        self.user_id = user_id
        self.message_count = message_count
        self._timeout = 300  # 5 minutes
    
    def build_components(self) -> list[hikari.api.ComponentBuilder]:
        """Build the action row components."""
        share_button = hikari.impl.InteractiveButtonBuilder(
            style=hikari.ButtonStyle.PRIMARY,
            custom_id=f"share_tldr:{self.user_id}:{self.message_count}",
            emoji="📤",
            label="Share to Channel"
        )
        
        action_row = hikari.impl.MessageActionRowBuilder()
        action_row.add_component(share_button)
        return [action_row]
    
    def get_summary_content(self) -> str:
        """Get the summary content for sharing."""
        return self.summary_content