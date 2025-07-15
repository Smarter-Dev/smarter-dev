"""Event handlers for Discord bot interactions.

This module handles Discord component interactions like select menus,
buttons, and other interactive elements used by the bot commands.
"""

from __future__ import annotations

import hikari
import logging
from typing import Dict, Any

from smarter_dev.bot.views.squad_views import SquadSelectView

logger = logging.getLogger(__name__)

# Global storage for active views (in production, this could be Redis-backed)
active_views: Dict[str, Any] = {}


def register_view(interaction_id: str, view: Any) -> None:
    """Register an active view for interaction handling.
    
    Args:
        interaction_id: Unique identifier for the interaction
        view: The view instance to register
    """
    active_views[interaction_id] = view
    logger.debug(f"Registered view for interaction {interaction_id}")


def unregister_view(interaction_id: str) -> None:
    """Unregister an active view.
    
    Args:
        interaction_id: Unique identifier for the interaction
    """
    if interaction_id in active_views:
        del active_views[interaction_id]
        logger.debug(f"Unregistered view for interaction {interaction_id}")


async def handle_component_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle component interactions for the bot.
    
    Args:
        event: The interaction event from Discord
    """
    if not isinstance(event.interaction, hikari.ComponentInteraction):
        return
    
    custom_id = event.interaction.custom_id
    logger.debug(f"Handling component interaction: {custom_id}")
    
    try:
        # Handle squad selection interactions
        if custom_id == "squad_select":
            await handle_squad_select_interaction(event)
        elif custom_id in ["squad_confirm", "squad_cancel"]:
            await handle_squad_confirm_interaction(event)
        else:
            logger.warning(f"Unhandled component interaction: {custom_id}")
    
    except Exception as e:
        logger.exception(f"Error handling component interaction {custom_id}: {e}")
        
        # Try to respond with error message
        try:
            if not event.interaction.is_responded():
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_UPDATE,
                    embed=hikari.Embed(
                        title="❌ Error",
                        description="An error occurred while processing your interaction.",
                        color=hikari.Color(0xef4444)
                    ),
                    components=[]
                )
        except Exception as e2:
            logger.error(f"Failed to send error response: {e2}")


async def handle_squad_select_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle squad selection menu interactions.
    
    Args:
        event: The interaction event
    """
    # In a production environment, you'd want to properly track views
    # For now, we'll handle this directly in the view
    
    # Get user and guild IDs to find the appropriate view
    user_id = str(event.interaction.user.id)
    guild_id = str(event.interaction.guild_id) if event.interaction.guild_id else None
    
    if not guild_id:
        logger.error("Squad select interaction without guild context")
        return
    
    # For this implementation, we'll handle the interaction within the view itself
    # In production, you might want to store active views in Redis or a database
    logger.info(f"Squad select interaction from user {user_id} in guild {guild_id}")
    
    # The SquadSelectView should handle its own interactions
    # This is a placeholder for the production implementation


async def handle_squad_confirm_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle squad confirmation button interactions.
    
    Args:
        event: The interaction event
    """
    user_id = str(event.interaction.user.id)
    guild_id = str(event.interaction.guild_id) if event.interaction.guild_id else None
    
    if not guild_id:
        logger.error("Squad confirm interaction without guild context")
        return
    
    logger.info(f"Squad confirm interaction from user {user_id} in guild {guild_id}")
    
    # Handle confirmation logic here
    # This would typically involve updating the squad membership


def setup_interaction_handlers(bot: hikari.GatewayBot) -> None:
    """Set up interaction event handlers for the bot.
    
    Args:
        bot: The Discord bot instance
    """
    @bot.listen(hikari.InteractionCreateEvent)
    async def on_interaction_create(event: hikari.InteractionCreateEvent) -> None:
        """Handle all interaction events."""
        await handle_component_interaction(event)
    
    logger.info("Interaction handlers set up")


def load(bot: hikari.GatewayBot) -> None:
    """Load the events plugin."""
    setup_interaction_handlers(bot)
    logger.info("Events plugin loaded")


def unload(bot: hikari.GatewayBot) -> None:
    """Unload the events plugin."""
    # Clear active views
    active_views.clear()
    logger.info("Events plugin unloaded")