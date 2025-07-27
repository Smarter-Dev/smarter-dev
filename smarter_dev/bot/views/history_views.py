"""Interactive history views for Discord bot.

This module provides interactive UI components for transaction history display and sharing.
"""

from __future__ import annotations

import hikari
import logging
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from smarter_dev.bot.services.models import Transaction

logger = logging.getLogger(__name__)


class HistoryShareView:
    """View with share button for history command."""
    
    def __init__(self, transactions: List['Transaction'], user_id: str):
        """Initialize the history share view.
        
        Args:
            transactions: Transaction history
            user_id: User ID for display context
        """
        self.transactions = transactions
        self.user_id = user_id
        self._timeout = 300  # 5 minutes
    
    def build_components(self) -> list[hikari.api.ComponentBuilder]:
        """Build the action row components."""
        share_button = hikari.impl.InteractiveButtonBuilder(
            style=hikari.ButtonStyle.PRIMARY,
            custom_id="share_history",
            emoji="📤",
            label="Share"
        )
        
        action_row = hikari.impl.MessageActionRowBuilder()
        action_row.add_component(share_button)
        return [action_row]