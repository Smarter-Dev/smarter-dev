"""Squad management commands for the Discord bot.

This module implements all squad-related slash commands using the service layer
for business logic. Commands include listing squads, joining, leaving, and
getting squad information.
"""

from __future__ import annotations

import hikari
import lightbulb
import logging
from datetime import datetime, timezone
from typing import List, TYPE_CHECKING

from smarter_dev.bot.utils.embeds import (
    create_error_embed, 
    create_success_embed,
    create_squad_list_embed
)
from smarter_dev.bot.utils.image_embeds import get_generator
from smarter_dev.bot.views.squad_views import SquadSelectView, SquadListShareView
from smarter_dev.bot.services.exceptions import (
    ServiceError,
    ValidationError
)

if TYPE_CHECKING:
    from smarter_dev.bot.services.squads_service import SquadsService
    from smarter_dev.bot.services.bytes_service import BytesService

logger = logging.getLogger(__name__)

# Create plugin
plugin = lightbulb.Plugin("squads")


@plugin.command
@lightbulb.command("squads", "Squad management commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def squads_group(ctx: lightbulb.Context) -> None:
    """Base squads command group."""
    pass


@squads_group.child
@lightbulb.command("list", "View available squads in this server")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def list_command(ctx: lightbulb.Context) -> None:
    """Handle squad list command - show available squads."""
    service: SquadsService = getattr(ctx.bot, 'd', {}).get('squads_service')
    if not service:
        # Fallback to _services dict
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('squads_service')
    
    if not service:
        generator = get_generator()
        image_file = generator.create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    try:
        squads = await service.list_squads(str(ctx.guild_id))
        
        if not squads:
            generator = get_generator()
            image_file = generator.create_error_embed("No squads have been created yet!")
            await ctx.respond(attachment=image_file)
            return
        
        # Get user's current squad
        user_squad_response = await service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
        current_squad_id = user_squad_response.squad.id if user_squad_response.squad else None
        
        # Get guild roles for color information
        guild_roles = {}
        guild = ctx.get_guild()
        if guild:
            for role in guild.get_roles().values():
                guild_roles[str(role.id)] = role.color
        
        # Create image embed for squad list
        generator = get_generator()
        image_file = generator.create_squad_list_embed(
            squads, 
            ctx.get_guild().name, 
            str(current_squad_id) if current_squad_id else None,
            guild_roles
        )
        
        # Create share view
        share_view = SquadListShareView(
            squads, 
            ctx.get_guild().name, 
            str(current_squad_id) if current_squad_id else None,
            guild_roles
        )
        
        # Send as ephemeral message with share button
        await ctx.respond(
            attachment=image_file,
            components=share_view.build_components(),
            flags=hikari.MessageFlag.EPHEMERAL
        )
        return
            
    except ServiceError as e:
        logger.error(f"Service error in squad list command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("Failed to get squads. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.exception(f"Unexpected error in squad list command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)


@squads_group.child
@lightbulb.command("join", "Join a squad using an interactive menu")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def join_command(ctx: lightbulb.Context) -> None:
    """Handle squad join command - interactive squad selection."""
    # Defer the interaction immediately to prevent timeout
    await ctx.respond(hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=hikari.MessageFlag.EPHEMERAL)
    
    squads_service: SquadsService = getattr(ctx.bot, 'd', {}).get('squads_service')
    bytes_service: BytesService = getattr(ctx.bot, 'd', {}).get('bytes_service')
    
    if not squads_service:
        # Fallback to _services dict
        squads_service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('squads_service')
    if not bytes_service:
        # Fallback to _services dict
        bytes_service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('bytes_service')
    
    if not squads_service or not bytes_service:
        generator = get_generator()
        image_file = generator.create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.edit_last_response(attachment=image_file)
        return
    
    try:
        # Get available squads
        squads = await squads_service.list_squads(str(ctx.guild_id))
        
        if not squads:
            generator = get_generator()
            image_file = generator.create_error_embed("No squads available to join!")
            await ctx.edit_last_response(attachment=image_file)
            return
        
        # Filter out inactive squads and default squads for joining
        active_squads = [squad for squad in squads if squad.is_active and not getattr(squad, 'is_default', False)]
        
        if not active_squads:
            generator = get_generator()
            image_file = generator.create_error_embed("No squads available to join!")
            await ctx.edit_last_response(attachment=image_file)
            return
        
        # Get user's balance and current squad (defaults to fresh balance data)
        balance = await bytes_service.get_balance(str(ctx.guild_id), str(ctx.user.id))
        user_squad_response = await squads_service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
        current_squad = user_squad_response.squad
        
        # Create interactive view
        view = SquadSelectView(
            squads=active_squads,
            current_squad=current_squad,
            user_balance=balance.balance,
            user_id=str(ctx.user.id),
            guild_id=str(ctx.guild_id),
            squads_service=squads_service,
            timeout=60
        )
        
        # Create image embed for squad selection
        generator = get_generator()
        image_file = generator.create_squad_join_selector_embed(
            user_balance=balance.balance
        )
        
        response = await ctx.edit_last_response(
            attachment=image_file, 
            components=view.build()
        )
        view.start(response, ctx.bot)
        
    except ServiceError as e:
        logger.error(f"Service error in squad join command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("Failed to load squad selection. Please try again later.")
        await ctx.edit_last_response(attachment=image_file)
    except Exception as e:
        logger.exception(f"Unexpected error in squad join command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.edit_last_response(attachment=image_file)



@squads_group.child
@lightbulb.command("info", "Get detailed information about your current squad")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def info_command(ctx: lightbulb.Context) -> None:
    """Handle squad info command - show current squad details."""
    logger.info(f"Squad info command called by user {ctx.user.id} in guild {ctx.guild_id}")
    
    service: SquadsService = getattr(ctx.bot, 'd', {}).get('squads_service')
    if not service:
        # Fallback to _services dict
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('squads_service')
    
    if not service:
        logger.error("Squad service not found in bot services")
        generator = get_generator()
        image_file = generator.create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    logger.info(f"Squad service found, calling get_user_squad for user {ctx.user.id}")
    
    try:
        user_squad_response = await service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
        logger.info(f"Got user squad response: is_in_squad={user_squad_response.is_in_squad}")
        
        if not user_squad_response.is_in_squad:
            logger.info("User is not in any squad")
            generator = get_generator()
            image_file = generator.create_error_embed("You are not currently in any squad!")
            await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        logger.info("User is in a squad, continuing with squad info")
        
        squad = user_squad_response.squad
        
        # Get squad members
        members = await service.get_squad_members(str(ctx.guild_id), squad.id)
        
        # Enhance member data with Discord names
        enhanced_members = []
        for member in members:
            if not member.username:
                try:
                    discord_member = ctx.get_guild().get_member(int(member.user_id))
                    if discord_member:
                        # Create enhanced member with Discord name
                        from dataclasses import replace
                        member = replace(member, username=discord_member.display_name)
                except:
                    pass
            enhanced_members.append(member)
        
        # Create image embed for squad info
        generator = get_generator()
        image_file = generator.create_squad_info_embed(squad, enhanced_members, user_squad_response)
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        
    except ServiceError as e:
        logger.error(f"Service error in squad info command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("Failed to get squad information. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.exception(f"Unexpected error in squad info command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)


async def squad_autocomplete(
    option: hikari.AutocompleteInteractionOption, 
    interaction: hikari.AutocompleteInteraction
) -> List[str]:
    """Autocomplete function for squad names."""
    try:
        # Get the service
        service: SquadsService = getattr(interaction.app.d, 'squads_service')
        if not service:
            service = getattr(interaction.app.d, '_services', {}).get('squads_service')
        
        if not service:
            return []
        
        # Get all squads
        squads = await service.list_squads(str(interaction.guild_id))
        
        # Filter squads based on current input
        current_input = option.value.lower() if option.value else ""
        matching_squads = [
            squad.name for squad in squads 
            if current_input in squad.name.lower()
        ]
        
        # Return up to 25 suggestions (Discord limit)
        return matching_squads[:25]
        
    except Exception:
        # If autocomplete fails, just return empty list
        return []


@squads_group.child
@lightbulb.option("squad", "Name of the squad to view (leave empty for your squad)", required=False, autocomplete=squad_autocomplete)
@lightbulb.command("members", "View members of a specific squad or your current squad")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def members_command(ctx: lightbulb.Context) -> None:
    """Handle squad members command - show squad member list."""
    service: SquadsService = getattr(ctx.bot, 'd', {}).get('squads_service')
    if not service:
        # Fallback to _services dict
        service = getattr(ctx.bot, 'd', {}).get('_services', {}).get('squads_service')
    
    if not service:
        generator = get_generator()
        image_file = generator.create_error_embed("Bot services are not initialized. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        return
    
    try:
        target_squad = None
        squad_name = ctx.options.squad
        
        if squad_name:
            # Find squad by name
            squads = await service.list_squads(str(ctx.guild_id))
            target_squad = next(
                (s for s in squads if s.name.lower() == squad_name.lower()), 
                None
            )
            
            if not target_squad:
                generator = get_generator()
                image_file = generator.create_error_embed(f"Squad '{squad_name}' not found!")
                await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
                return
        else:
            # Use user's current squad
            user_squad_response = await service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
            
            if not user_squad_response.is_in_squad:
                generator = get_generator()
                image_file = generator.create_error_embed("You are not in any squad! Specify a squad name to view its members.")
                await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
                return
            
            target_squad = user_squad_response.squad
        
        # Get squad members
        members = await service.get_squad_members(str(ctx.guild_id), target_squad.id)
        
        # Enhance member data with Discord names
        enhanced_members = []
        for member in members:
            enhanced_member = member
            if not member.username:
                try:
                    # Try to get guild member first
                    discord_member = ctx.get_guild().get_member(int(member.user_id))
                    if discord_member:
                        from dataclasses import replace
                        enhanced_member = replace(member, username=discord_member.display_name)
                    else:
                        # Try to fetch user from Discord
                        user = await ctx.bot.rest.fetch_user(int(member.user_id))
                        enhanced_member = replace(member, username=user.username)
                except:
                    pass
            enhanced_members.append(enhanced_member)
        
        # Create image embed for squad members
        generator = get_generator()
        image_file = generator.create_squad_members_embed(target_squad, enhanced_members)
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
        
    except ServiceError as e:
        logger.error(f"Service error in squad members command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("Failed to get squad members. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.exception(f"Unexpected error in squad members command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("An unexpected error occurred. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)


def load(bot: lightbulb.BotApp) -> None:
    """Load the squads plugin."""
    bot.add_plugin(plugin)
    logger.info("Squads plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the squads plugin."""
    bot.remove_plugin(plugin)
    logger.info("Squads plugin unloaded")