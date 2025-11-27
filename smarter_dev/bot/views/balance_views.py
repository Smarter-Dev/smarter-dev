"""Interactive balance views for Discord bot.

This module provides interactive UI components for balance display and sharing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import hikari

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class BalanceShareView:
    """View with share button for balance command."""

    def __init__(self, username: str, balance: int, streak_count: int,
                 last_daily: str, total_received: int, total_sent: int):
        """Initialize the balance share view.

        Args:
            username: Username for display
            balance: Current balance
            streak_count: Current streak count
            last_daily: Last daily claim date string
            total_received: Total bytes received
            total_sent: Total bytes sent
        """
        self.username = username
        self.balance = balance
        self.streak_count = streak_count
        self.last_daily = last_daily
        self.total_received = total_received
        self.total_sent = total_sent
        self._timeout = 300  # 5 minutes

    def build_components(self) -> list[hikari.api.ComponentBuilder]:
        """Build the action row components."""
        share_button = hikari.impl.InteractiveButtonBuilder(
            style=hikari.ButtonStyle.PRIMARY,
            custom_id="share_balance",
            emoji="📤",
            label="Share"
        )

        action_row = hikari.impl.MessageActionRowBuilder()
        action_row.add_component(share_button)
        return [action_row]

