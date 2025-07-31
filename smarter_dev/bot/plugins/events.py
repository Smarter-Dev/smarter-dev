"""Event handlers for Discord bot interactions.

This module handles Discord component interactions like select menus,
buttons, and other interactive elements used by the bot commands.
"""

from __future__ import annotations

import hikari
import logging
from typing import Dict, Any

from smarter_dev.bot.views.squad_views import SquadSelectView
from smarter_dev.bot.views.balance_views import BalanceShareView
from smarter_dev.bot.views.leaderboard_views import LeaderboardShareView
from smarter_dev.bot.views.history_views import HistoryShareView

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


async def handle_modal_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle modal interactions for the bot.
    
    Args:
        event: The interaction event from Discord
    """
    if not isinstance(event.interaction, hikari.ModalInteraction):
        return
    
    custom_id = event.interaction.custom_id
    logger.debug(f"Handling modal interaction: {custom_id}")
    
    try:
        # Handle bytes transfer modal
        if custom_id.startswith("send_bytes_modal:"):
            await handle_send_bytes_modal(event)
        else:
            logger.warning(f"Unhandled modal interaction: {custom_id}")
    
    except Exception as e:
        logger.exception(f"Error handling modal interaction {custom_id}: {e}")
        
        # Try to respond with error message
        try:
            if not event.interaction.is_responded():
                from smarter_dev.bot.utils.image_embeds import get_generator
                generator = get_generator()
                image_file = generator.create_error_embed("An error occurred while processing your request.")
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    attachment=image_file,
                    flags=hikari.MessageFlag.EPHEMERAL
                )
        except Exception as e2:
            logger.error(f"Failed to send modal error response: {e2}")


async def handle_send_bytes_modal(event: hikari.InteractionCreateEvent) -> None:
    """Handle send bytes modal submission.
    
    Args:
        event: The modal interaction event
    """
    if not isinstance(event.interaction, hikari.ModalInteraction):
        return
        
    # Parse custom_id to get recipient ID
    custom_id_parts = event.interaction.custom_id.split(":")
    if len(custom_id_parts) != 2:
        logger.error(f"Invalid send_bytes_modal custom_id format: {event.interaction.custom_id}")
        return
    
    recipient_id = custom_id_parts[1]
    user_id = str(event.interaction.user.id)
    
    # Find the handler
    handler_key = f"send_bytes_modal:{recipient_id}:{user_id}"
    
    # Get bot instance from event
    bot = event.app
    if not hasattr(bot, 'd') or 'modal_handlers' not in bot.d:
        logger.error("No modal handlers found in bot data")
        return
    
    handler = bot.d['modal_handlers'].get(handler_key)
    if not handler:
        logger.error(f"No handler found for key: {handler_key}")
        return
    
    try:
        # Call the handler's submit method
        await handler.handle_submit(event.interaction)
        
        # Clean up the handler after use
        del bot.d['modal_handlers'][handler_key]
        
    except Exception as e:
        logger.exception(f"Error in send bytes modal handler: {e}")
        # Clean up handler even on error
        if handler_key in bot.d['modal_handlers']:
            del bot.d['modal_handlers'][handler_key]
        raise


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
        elif custom_id == "share_balance":
            await handle_balance_share_interaction(event)
        elif custom_id == "share_leaderboard":
            await handle_leaderboard_share_interaction(event)
        elif custom_id == "share_history":
            await handle_history_share_interaction(event)
        elif custom_id == "share_squad_list":
            await handle_squad_list_share_interaction(event)
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


async def handle_balance_share_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle balance share button interactions.
    
    Args:
        event: The interaction event
    """
    if not isinstance(event.interaction, hikari.ComponentInteraction):
        return
        
    user_id = str(event.interaction.user.id)
    guild_id = str(event.interaction.guild_id) if event.interaction.guild_id else None
    
    if not guild_id:
        logger.error("Balance share interaction without guild context")
        return
    
    logger.info(f"Balance share interaction from user {user_id} in guild {guild_id}")
    
    try:
        # Get the user's current balance data to regenerate the image
        from smarter_dev.bot.services.bytes_service import BytesService
        from smarter_dev.bot.utils.image_embeds import get_generator
        
        # Get the bytes service from the bot
        service = None
        
        # Try multiple ways to access the service
        if hasattr(event.app, 'd') and hasattr(event.app.d, '_services'):
            service = event.app.d._services.get('bytes_service')
        elif hasattr(event.app, 'bytes_service'):
            service = event.app.bytes_service
        elif hasattr(event.app, 'd') and isinstance(event.app.d, dict):
            services = event.app.d.get('_services', {})
            service = services.get('bytes_service')
        
        logger.debug(f"Service access result: {service is not None}")
        
        if not service:
            await event.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                content="❌ Service not available. Please try again later.",
                flags=hikari.MessageFlag.EPHEMERAL
            )
            return
        
        # Get current balance
        balance = await service.get_balance(guild_id, user_id)
        
        # Format last daily as readable string
        last_daily_str = None
        if balance.last_daily:
            last_daily_str = balance.last_daily.strftime('%B %d, %Y')
        
        # Get username for display
        username = event.interaction.user.display_name or event.interaction.user.username
        
        # Generate the balance image
        generator = get_generator()
        image_file = generator.create_balance_embed(
            username=username,
            balance=balance.balance,
            streak_count=balance.streak_count,
            last_daily=last_daily_str,
            total_received=balance.total_received,
            total_sent=balance.total_sent
        )
        
        # Send as public message
        await event.interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            attachment=image_file,
            flags=hikari.MessageFlag.NONE  # Public message
        )
        
    except Exception as e:
        logger.exception(f"Error in balance share interaction: {e}")
        
        # Send error response
        try:
            if not event.interaction.is_responded():
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content="❌ Failed to share balance. Please try again later.",
                    flags=hikari.MessageFlag.EPHEMERAL
                )
        except Exception as e2:
            logger.error(f"Failed to send balance share error response: {e2}")


async def handle_leaderboard_share_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle leaderboard share button interactions.
    
    Args:
        event: The interaction event
    """
    if not isinstance(event.interaction, hikari.ComponentInteraction):
        return
        
    user_id = str(event.interaction.user.id)
    guild_id = str(event.interaction.guild_id) if event.interaction.guild_id else None
    
    if not guild_id:
        logger.error("Leaderboard share interaction without guild context")
        return
    
    logger.info(f"Leaderboard share interaction from user {user_id} in guild {guild_id}")
    
    try:
        # Get the bytes service from the bot
        service = None
        
        # Try multiple ways to access the service
        if hasattr(event.app, 'd') and hasattr(event.app.d, '_services'):
            service = event.app.d._services.get('bytes_service')
        elif hasattr(event.app, 'bytes_service'):
            service = event.app.bytes_service
        elif hasattr(event.app, 'd') and isinstance(event.app.d, dict):
            services = event.app.d.get('_services', {})
            service = services.get('bytes_service')
        
        logger.debug(f"Service access result: {service is not None}")
        
        if not service:
            await event.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                content="❌ Service not available. Please try again later.",
                flags=hikari.MessageFlag.EPHEMERAL
            )
            return
        
        # Get leaderboard data (default to 10 entries like the command)
        entries = await service.get_leaderboard(guild_id, 10)
        
        # Create user display names mapping
        user_display_names = {}
        for entry in entries:
            try:
                member = event.interaction.get_guild().get_member(int(entry.user_id))
                user_display_names[entry.user_id] = member.display_name if member else f"User {entry.user_id[:8]}"
            except:
                user_display_names[entry.user_id] = f"User {entry.user_id[:8]}"
        
        # Generate the leaderboard image
        from smarter_dev.bot.utils.image_embeds import get_generator
        generator = get_generator()
        image_file = generator.create_leaderboard_embed(
            entries, 
            event.interaction.get_guild().name, 
            user_display_names
        )
        
        # Send as public message
        await event.interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            attachment=image_file,
            flags=hikari.MessageFlag.NONE  # Public message
        )
        
    except Exception as e:
        logger.exception(f"Error in leaderboard share interaction: {e}")
        
        # Send error response
        try:
            if not event.interaction.is_responded():
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content="❌ Failed to share leaderboard. Please try again later.",
                    flags=hikari.MessageFlag.EPHEMERAL
                )
        except Exception as e2:
            logger.error(f"Failed to send leaderboard share error response: {e2}")


async def handle_history_share_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle history share button interactions.
    
    Args:
        event: The interaction event
    """
    if not isinstance(event.interaction, hikari.ComponentInteraction):
        return
        
    user_id = str(event.interaction.user.id)
    guild_id = str(event.interaction.guild_id) if event.interaction.guild_id else None
    
    if not guild_id:
        logger.error("History share interaction without guild context")
        return
    
    logger.info(f"History share interaction from user {user_id} in guild {guild_id}")
    
    try:
        # Get the bytes service from the bot
        service = None
        
        # Try multiple ways to access the service
        if hasattr(event.app, 'd') and hasattr(event.app.d, '_services'):
            service = event.app.d._services.get('bytes_service')
        elif hasattr(event.app, 'bytes_service'):
            service = event.app.bytes_service
        elif hasattr(event.app, 'd') and isinstance(event.app.d, dict):
            services = event.app.d.get('_services', {})
            service = services.get('bytes_service')
        
        logger.debug(f"Service access result: {service is not None}")
        
        if not service:
            await event.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                content="❌ Service not available. Please try again later.",
                flags=hikari.MessageFlag.EPHEMERAL
            )
            return
        
        # Get transaction history (default to 10 entries like the command)
        transactions = await service.get_transaction_history(
            guild_id,
            user_id=user_id,
            limit=10
        )
        
        # Generate the history image
        from smarter_dev.bot.utils.image_embeds import get_generator
        generator = get_generator()
        image_file = generator.create_history_embed(transactions, user_id)
        
        # Send as public message
        await event.interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            attachment=image_file,
            flags=hikari.MessageFlag.NONE  # Public message
        )
        
    except Exception as e:
        logger.exception(f"Error in history share interaction: {e}")
        
        # Send error response
        try:
            if not event.interaction.is_responded():
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content="❌ Failed to share transaction history. Please try again later.",
                    flags=hikari.MessageFlag.EPHEMERAL
                )
        except Exception as e2:
            logger.error(f"Failed to send history share error response: {e2}")


async def handle_squad_list_share_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle squad list share button interactions.
    
    Args:
        event: The interaction event
    """
    if not isinstance(event.interaction, hikari.ComponentInteraction):
        return
        
    user_id = str(event.interaction.user.id)
    guild_id = str(event.interaction.guild_id) if event.interaction.guild_id else None
    
    if not guild_id:
        logger.error("Squad list share interaction without guild context")
        return
    
    logger.info(f"Squad list share interaction from user {user_id} in guild {guild_id}")
    
    try:
        # Get the squads service from the bot
        service = None
        
        # Try multiple ways to access the service
        if hasattr(event.app, 'd') and hasattr(event.app.d, '_services'):
            service = event.app.d._services.get('squads_service')
        elif hasattr(event.app, 'squads_service'):
            service = event.app.squads_service
        elif hasattr(event.app, 'd') and isinstance(event.app.d, dict):
            services = event.app.d.get('_services', {})
            service = services.get('squads_service')
        
        logger.debug(f"Service access result: {service is not None}")
        
        if not service:
            await event.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                content="❌ Service not available. Please try again later.",
                flags=hikari.MessageFlag.EPHEMERAL
            )
            return
        
        # Get squads data
        squads = await service.list_squads(guild_id)
        
        if not squads:
            await event.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                content="❌ No squads available.",
                flags=hikari.MessageFlag.EPHEMERAL
            )
            return
        
        # Get user's current squad
        user_squad_response = await service.get_user_squad(guild_id, user_id)
        current_squad_id = user_squad_response.squad.id if user_squad_response.squad else None
        
        # Get guild roles for color information
        guild_roles = {}
        guild = event.interaction.get_guild()
        if guild:
            for role in guild.get_roles().values():
                guild_roles[str(role.id)] = role.color
        
        # Generate the squad list image
        from smarter_dev.bot.utils.image_embeds import get_generator
        generator = get_generator()
        image_file = generator.create_squad_list_embed(
            squads, 
            guild.name, 
            str(current_squad_id) if current_squad_id else None,
            guild_roles
        )
        
        # Send as public message
        await event.interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE,
            attachment=image_file,
            flags=hikari.MessageFlag.NONE  # Public message
        )
        
    except Exception as e:
        logger.exception(f"Error in squad list share interaction: {e}")
        
        # Send error response
        try:
            if not event.interaction.is_responded():
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    content="❌ Failed to share squad list. Please try again later.",
                    flags=hikari.MessageFlag.EPHEMERAL
                )
        except Exception as e2:
            logger.error(f"Failed to send squad list share error response: {e2}")


def setup_interaction_handlers(bot: hikari.GatewayBot) -> None:
    """Set up interaction event handlers for the bot.
    
    Args:
        bot: The Discord bot instance
    """
    @bot.listen(hikari.InteractionCreateEvent)
    async def on_interaction_create(event: hikari.InteractionCreateEvent) -> None:
        """Handle all interaction events."""
        # Handle both component and modal interactions
        await handle_component_interaction(event)
        await handle_modal_interaction(event)
    
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