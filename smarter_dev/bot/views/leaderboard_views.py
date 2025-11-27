"""Interactive leaderboard views for Discord bot.

This module provides interactive UI components for leaderboard display and sharing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import hikari

if TYPE_CHECKING:
    from smarter_dev.bot.services.models import LeaderboardEntry

logger = logging.getLogger(__name__)


class LeaderboardShareView:
    """View with share button for leaderboard command."""

    def __init__(self, entries: list[LeaderboardEntry], guild_name: str, user_display_names: dict):
        """Initialize the leaderboard share view.

        Args:
            entries: Leaderboard entries
            guild_name: Guild name for display
            user_display_names: Mapping of user IDs to display names
        """
        self.entries = entries
        self.guild_name = guild_name
        self.user_display_names = user_display_names
        self._timeout = 300  # 5 minutes

    def build_components(self) -> list[hikari.api.ComponentBuilder]:
        """Build the action row components."""
        share_button = hikari.impl.InteractiveButtonBuilder(
            style=hikari.ButtonStyle.PRIMARY,
            custom_id="share_leaderboard",
            emoji="📤",
            label="Share"
        )

        action_row = hikari.impl.MessageActionRowBuilder()
        action_row.add_component(share_button)
        return [action_row]
