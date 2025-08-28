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
from smarter_dev.bot.views.beacon_views import create_beacon_message_modal, handle_beacon_modal_submit, is_user_on_cooldown
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


async def _send_squad_join_announcement(
    bot: lightbulb.BotApp,
    guild_id: str,
    user: hikari.User,
    squad,
    announcement_channel_id: str
) -> None:
    """Send a squad join announcement to the squad's announcement channel.
    
    Args:
        bot: The Discord bot instance
        guild_id: Discord guild ID
        user: User who joined the squad
        squad: Squad object with squad information
        announcement_channel_id: Channel ID to send announcement to
    """
    try:
        # Only announce for non-default squads
        if getattr(squad, 'is_default', False):
            logger.debug(f"Skipping announcement for default squad {squad.name}")
            return
        
        # Get the announcement channel
        try:
            channel = bot.rest.fetch_channel(int(announcement_channel_id))
            if not channel:
                logger.warning(f"Could not find announcement channel {announcement_channel_id} for squad {squad.name}")
                return
        except Exception as e:
            logger.error(f"Error fetching announcement channel {announcement_channel_id}: {e}")
            return
        
        # Create green success embed announcement
        generator = get_generator()
        
        # Get user's display name for the announcement
        display_name = user.display_name or user.username
        
        # Create announcement message
        announcement_title = "New Squad Member!"
        announcement_description = f"{display_name} has joined {squad.name}!"
        
        # Create the green embed image
        image_file = generator.create_success_embed(announcement_title, announcement_description)
        
        # Send the announcement
        await bot.rest.create_message(
            channel=int(announcement_channel_id),
            attachment=image_file
        )
        
        logger.info(f"Successfully sent squad join announcement for {display_name} joining {squad.name}")
        
    except Exception as e:
        logger.error(f"Failed to send squad join announcement for user {user.id} joining squad {squad.name}: {e}")
        # Don't raise - we don't want squad joins to fail because of announcement issues


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
        
        # Sort squads to put default squad at the bottom
        squads.sort(key=lambda s: (getattr(s, 'is_default', False), s.name.lower()))
        
        # Get user's current squad
        user_squad_response = await service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
        current_squad_id = user_squad_response.squad.id if user_squad_response.squad else None
        
        # Check for active campaign
        has_active_campaign = await service._check_active_campaign(str(ctx.guild_id))
        
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
            guild_roles,
            has_active_campaign=has_active_campaign
        )
        
        # Create share view
        share_view = SquadListShareView(
            squads, 
            ctx.get_guild().name, 
            str(current_squad_id) if current_squad_id else None,
            guild_roles,
            has_active_campaign=has_active_campaign
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


async def joinable_squad_autocomplete(
    option: hikari.AutocompleteInteractionOption, 
    interaction: hikari.AutocompleteInteraction
) -> List[str]:
    """Autocomplete function for squad names that can be joined."""
    try:
        logger.info(f"Autocomplete called: option.value='{option.value}', guild_id={interaction.guild_id}")
        
        # Get the service
        service: SquadsService = getattr(interaction.app.d, 'squads_service')
        if not service:
            service = getattr(interaction.app.d, '_services', {}).get('squads_service')
        
        if not service:
            logger.warning("No squads service found for autocomplete")
            return []
        
        # Get all squads
        squads = await service.list_squads(str(interaction.guild_id))
        logger.info(f"Found {len(squads)} total squads")
        
        # Filter out inactive squads and default squads (same logic as join command)
        joinable_squads = [squad for squad in squads if squad.is_active and not getattr(squad, 'is_default', False)]
        logger.info(f"Found {len(joinable_squads)} joinable squads: {[s.name for s in joinable_squads]}")
        
        # Filter squads based on current input
        current_input = option.value.lower() if option.value else ""
        matching_squads = [
            squad.name for squad in joinable_squads 
            if current_input in squad.name.lower()
        ]
        
        logger.info(f"Returning {len(matching_squads)} matching squads: {matching_squads}")
        # Return up to 25 suggestions (Discord limit)
        return matching_squads[:25]
        
    except Exception as e:
        # If autocomplete fails, just return empty list
        logger.error(f"Autocomplete error: {e}")
        return []


async def _handle_direct_squad_join(
    ctx: lightbulb.Context,
    squad_name: str,
    squads_service: 'SquadsService',
    bytes_service: 'BytesService'
) -> None:
    """Handle direct squad join by name - mirrors dropdown selection behavior exactly."""
    try:
        # Get available squads (same filtering as interactive menu)
        squads = await squads_service.list_squads(str(ctx.guild_id))
        active_squads = [squad for squad in squads if squad.is_active and not getattr(squad, 'is_default', False)]
        
        # Find squad by name
        selected_squad = next(
            (s for s in active_squads if s.name.lower() == squad_name.lower()), 
            None
        )
        
        if not selected_squad:
            generator = get_generator()
            image_file = generator.create_error_embed(f"Squad '{squad_name}' not found or cannot be joined!")
            await ctx.edit_last_response(attachment=image_file)
            return
        
        # Get user's balance and current squad
        balance = await bytes_service.get_balance(str(ctx.guild_id), str(ctx.user.id))
        user_squad_response = await squads_service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
        current_squad = user_squad_response.squad
        
        # Check if user is already in this squad (same as dropdown logic)
        if current_squad and current_squad.id == selected_squad.id:
            generator = get_generator()
            image_file = generator.create_error_embed(f"You're already in the {selected_squad.name} squad!")
            await ctx.edit_last_response(attachment=image_file)
            return
        
        # Get username for transaction records (same as dropdown logic)
        username = None
        try:
            username = ctx.user.display_name or ctx.user.username
        except:
            pass  # Fall back to None
        
        # Process squad join (exact same call as dropdown)
        result = await squads_service.join_squad(
            str(ctx.guild_id),
            str(ctx.user.id),
            selected_squad.id,
            balance.balance,
            username
        )
        
        if not result.success:
            generator = get_generator()
            image_file = generator.create_error_embed(result.reason)
        else:
            # Assign Discord role for the new squad (same as dropdown logic)
            role_assignment_status = ""
            try:
                # Get the Discord guild and member
                guild = ctx.get_guild()
                if guild:
                    member = guild.get_member(int(ctx.user.id))
                    if member and result.squad.role_id:
                        # Remove previous squad role if switching squads
                        if result.previous_squad and result.previous_squad.role_id:
                            try:
                                await member.remove_role(int(result.previous_squad.role_id))
                                logger.info(f"Removed role {result.previous_squad.role_id} from user {ctx.user.id}")
                            except Exception as e:
                                logger.warning(f"Failed to remove previous squad role {result.previous_squad.role_id}: {e}")
                        
                        # Add new squad role
                        try:
                            await member.add_role(int(result.squad.role_id))
                            role_assignment_status = f"\n✅ Squad role assigned!"
                            logger.info(f"Assigned role {result.squad.role_id} to user {ctx.user.id}")
                        except Exception as e:
                            role_assignment_status = f"\n⚠️ Role assignment failed: {str(e)}"
                            logger.error(f"Failed to assign squad role {result.squad.role_id} to user {ctx.user.id}: {e}")
                    else:
                        logger.warning(f"Could not find member {ctx.user.id} in guild or squad has no role_id")
                else:
                    logger.warning("Could not get guild from context")
            except Exception as e:
                role_assignment_status = f"\n⚠️ Role assignment error: {str(e)}"
                logger.error(f"Error during role assignment for user {ctx.user.id}: {e}")
            
            # Build clean success description with custom welcome message (same as dropdown logic)
            if result.squad.welcome_message:
                description = result.squad.welcome_message
            else:
                description = f"Welcome to {result.squad.name}! We're glad to have you aboard."
            
            # Send announcement to squad's announcement channel if configured
            if hasattr(result.squad, 'announcement_channel') and result.squad.announcement_channel:
                try:
                    await _send_squad_join_announcement(
                        ctx.bot,
                        str(ctx.guild_id),
                        ctx.user,
                        result.squad,
                        result.squad.announcement_channel
                    )
                except Exception as e:
                    logger.warning(f"Failed to send squad join announcement: {e}")
            
            generator = get_generator()
            image_file = generator.create_success_embed("SQUAD JOINED", description)
    
        await ctx.edit_last_response(attachment=image_file)
        
    except Exception as e:
        logger.exception(f"Error in direct squad join: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed(f"Failed to join squad: {str(e)}")
        await ctx.edit_last_response(attachment=image_file)


@squads_group.child
@lightbulb.option("squad", "Name of the squad to join (leave empty for interactive menu)", required=False, autocomplete=joinable_squad_autocomplete)
@lightbulb.command("join", "Join a squad by name or using an interactive menu")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def join_command(ctx: lightbulb.Context) -> None:
    """Handle squad join command - direct join by name or interactive squad selection."""
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
    
    # Check if there's an active campaign that would prevent squad switching
    # First get user's current squad to determine if they're trying to switch
    try:
        user_squad_response = await squads_service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
        current_squad = user_squad_response.squad
        
        # Only check campaign if user is in a non-default squad (would be switching)
        if current_squad and not getattr(current_squad, 'is_default', False):
            has_active_campaign = await squads_service._check_active_campaign(str(ctx.guild_id))
            if has_active_campaign:
                generator = get_generator()
                image_file = generator.create_error_embed(
                    "Squad switching is disabled during active challenge campaigns to prevent spying on other squads."
                )
                await ctx.edit_last_response(attachment=image_file)
                return
    except Exception as e:
        logger.warning(f"Error checking campaign status: {e}")
        # Continue if check fails - don't block the command
    
    # Check if user provided a squad name
    squad_name = getattr(ctx.options, 'squad', None)
    
    if squad_name:
        # Handle direct squad join by name
        await _handle_direct_squad_join(ctx, squad_name, squads_service, bytes_service)
        return
    
    # Fall back to interactive menu (existing behavior)
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


@plugin.command
@lightbulb.command("beacon", "Send a beacon message to alert your squad")
@lightbulb.implements(lightbulb.SlashCommand)
async def beacon_command(ctx: lightbulb.Context) -> None:
    """Handle squad beacon command - send urgent message to squad with role ping."""
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
        # Check rate limiting first
        is_on_cooldown, seconds_remaining = is_user_on_cooldown(ctx.user.id)
        if is_on_cooldown:
            # Format time remaining in hours/minutes
            if seconds_remaining >= 3600:  # 1 hour or more
                hours_remaining = seconds_remaining // 3600
                minutes_part = (seconds_remaining % 3600) // 60
                if minutes_part > 0:
                    time_str = f"{hours_remaining} hour{'s' if hours_remaining != 1 else ''} and {minutes_part} minute{'s' if minutes_part != 1 else ''}"
                else:
                    time_str = f"{hours_remaining} hour{'s' if hours_remaining != 1 else ''}"
            else:
                minutes_remaining = max(1, seconds_remaining // 60)
                time_str = f"{minutes_remaining} minute{'s' if minutes_remaining != 1 else ''}"
            
            generator = get_generator()
            image_file = generator.create_error_embed(
                f"Please wait {time_str} before sending another beacon message."
            )
            await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        # Get user's current squad
        user_squad_response = await service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
        
        if not user_squad_response.is_in_squad:
            generator = get_generator()
            image_file = generator.create_error_embed("You must be in a squad to send beacon messages!")
            await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        squad = user_squad_response.squad
        
        # Check if we're in the correct channel
        if not squad.announcement_channel:
            generator = get_generator()
            image_file = generator.create_error_embed(
                f"Your squad ({squad.name}) doesn't have an announcement channel configured. "
                "Contact an administrator to set one up."
            )
            await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        if str(ctx.channel_id) != squad.announcement_channel:
            # Try to get the channel name for a better error message
            error_message = f"Beacon messages for {squad.name} can only be sent in your squad's announcement channel!"
            try:
                channel = await ctx.bot.rest.fetch_channel(int(squad.announcement_channel))
                # Filter to ASCII characters only and clean up the name
                clean_name = ''.join(c for c in channel.name if ord(c) < 128)
                error_message = f"Beacon messages for {squad.name} can only be sent in #{clean_name}"
            except Exception as e:
                logger.debug(f"Could not fetch channel name for {squad.announcement_channel}: {e}")
                # Use the generic message as fallback
                pass
            
            generator = get_generator()
            image_file = generator.create_error_embed(error_message)
            try:
                await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
            except (hikari.BadRequestError, hikari.NotFoundError):
                logger.warning("Failed to respond to beacon command - interaction may have expired")
            return
        
        # All checks passed, show the modal
        modal = create_beacon_message_modal()
        await ctx.respond_with_modal(
            modal.title,
            modal.custom_id,
            components=modal.components
        )
        
    except ServiceError as e:
        logger.error(f"Service error in beacon command: {e}")
        generator = get_generator()
        image_file = generator.create_error_embed("Failed to verify squad membership. Please try again later.")
        await ctx.respond(attachment=image_file, flags=hikari.MessageFlag.EPHEMERAL)
    except Exception as e:
        logger.exception(f"Unexpected error in beacon command: {e}")
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