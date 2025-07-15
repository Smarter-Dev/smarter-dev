"""Squad management commands for the Discord bot.

This module implements all squad-related slash commands using the service layer
for business logic. Commands include listing squads, joining, leaving, and
getting squad information.
"""

from __future__ import annotations

import hikari
import lightbulb
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from smarter_dev.bot.utils.embeds import (
    create_error_embed, 
    create_success_embed,
    create_squad_list_embed
)
from smarter_dev.bot.views.squad_views import SquadSelectView
from smarter_dev.bot.services.exceptions import (
    NotInSquadError,
    ServiceError,
    ValidationError
)

if TYPE_CHECKING:
    from smarter_dev.bot.services.squads_service import SquadsService
    from smarter_dev.bot.services.bytes_service import BytesService

logger = logging.getLogger(__name__)

# Create squads command group
squads_group = lightbulb.Group("squads", "Squad management commands")


@squads_group.register
class ListCommand(
    lightbulb.SlashCommand,
    name="list",
    description="View available squads in this server"
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        """Handle squad list command - show available squads."""
        service: SquadsService = ctx.app.d.squads_service
        
        if not service:
            embed = create_error_embed("Bot services are not initialized. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        try:
            squads = await service.list_squads(str(ctx.guild_id))
            
            if not squads:
                embed = create_error_embed("No squads have been created yet!")
                await ctx.respond(embed=embed)
                return
            
            # Get user's current squad
            user_squad_response = await service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
            current_squad_id = user_squad_response.squad.id if user_squad_response.squad else None
            
            embed = create_squad_list_embed(squads, current_squad_id)
            
            # Add footer with user's current status
            if user_squad_response.squad:
                embed.set_footer(f"You are currently in: {user_squad_response.squad.name}")
            else:
                embed.set_footer("You are not in any squad")
                
        except ServiceError as e:
            logger.error(f"Service error in squad list command: {e}")
            embed = create_error_embed("Failed to get squads. Please try again later.")
        except Exception as e:
            logger.exception(f"Unexpected error in squad list command: {e}")
            embed = create_error_embed("An unexpected error occurred. Please try again later.")
            
        await ctx.respond(embed=embed)


@squads_group.register
class JoinCommand(
    lightbulb.SlashCommand,
    name="join",
    description="Join a squad using an interactive menu"
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        """Handle squad join command - interactive squad selection."""
        squads_service: SquadsService = ctx.app.d.squads_service
        bytes_service: BytesService = ctx.app.d.bytes_service
        
        if not squads_service or not bytes_service:
            embed = create_error_embed("Bot services are not initialized. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        try:
            # Get available squads
            squads = await squads_service.list_squads(str(ctx.guild_id))
            
            if not squads:
                embed = create_error_embed("No squads available to join!")
                await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
                return
            
            # Filter out inactive squads for joining
            active_squads = [squad for squad in squads if squad.is_active]
            
            if not active_squads:
                embed = create_error_embed("No active squads available to join!")
                await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
                return
            
            # Get user's balance and current squad
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
            
            embed = hikari.Embed(
                title="🏆 Select a Squad to Join",
                description="Choose a squad from the menu below. You have 60 seconds to decide.",
                color=hikari.Color(0x3b82f6)
            )
            
            embed.add_field("Your Balance", f"**{balance.balance:,}** bytes", inline=True)
            
            if current_squad:
                embed.add_field("Current Squad", current_squad.name, inline=True)
            else:
                embed.add_field("Current Squad", "None", inline=True)
            
            # Show available squads count
            embed.add_field("Available Squads", f"{len(active_squads)} active squads", inline=True)
            
            response = await ctx.respond(
                embed=embed, 
                components=view.build(), 
                flags=hikari.MessageFlag.EPHEMERAL
            )
            view.start(response)
            
        except ServiceError as e:
            logger.error(f"Service error in squad join command: {e}")
            embed = create_error_embed("Failed to load squad selection. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
        except Exception as e:
            logger.exception(f"Unexpected error in squad join command: {e}")
            embed = create_error_embed("An unexpected error occurred. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@squads_group.register
class LeaveCommand(
    lightbulb.SlashCommand,
    name="leave",
    description="Leave your current squad"
):
    @lightbulb.invoke 
    async def invoke(self, ctx: lightbulb.Context) -> None:
        """Handle squad leave command - leave current squad."""
        service: SquadsService = ctx.app.d.squads_service
        
        if not service:
            embed = create_error_embed("Bot services are not initialized. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        try:
            # Check current squad status
            user_squad_response = await service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
            
            if not user_squad_response.is_in_squad:
                embed = create_error_embed("You are not currently in any squad!")
                await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
                return
            
            current_squad = user_squad_response.squad
            
            # Leave the squad
            await service.leave_squad(str(ctx.guild_id), str(ctx.user.id))
            
            embed = create_success_embed(
                title="✅ Squad Left",
                description=f"You have successfully left **{current_squad.name}**!"
            )
            
            embed.add_field(
                "Previous Squad", 
                current_squad.name, 
                inline=True
            )
            
            if user_squad_response.member_since:
                # Calculate membership duration
                membership_duration = datetime.now() - user_squad_response.member_since
                days = membership_duration.days
                if days > 0:
                    embed.add_field(
                        "Membership Duration",
                        f"{days} day{'s' if days != 1 else ''}",
                        inline=True
                    )
            
        except NotInSquadError:
            embed = create_error_embed("You are not currently in any squad!")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        except ServiceError as e:
            logger.error(f"Service error in squad leave command: {e}")
            embed = create_error_embed("Failed to leave squad. Please try again later.")
        except Exception as e:
            logger.exception(f"Unexpected error in squad leave command: {e}")
            embed = create_error_embed("An unexpected error occurred. Please try again later.")
            
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@squads_group.register
class InfoCommand(
    lightbulb.SlashCommand,
    name="info",
    description="Get detailed information about your current squad"
):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        """Handle squad info command - show current squad details."""
        service: SquadsService = ctx.app.d.squads_service
        
        if not service:
            embed = create_error_embed("Bot services are not initialized. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        try:
            user_squad_response = await service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
            
            if not user_squad_response.is_in_squad:
                embed = create_error_embed("You are not currently in any squad!")
                await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
                return
            
            squad = user_squad_response.squad
            
            # Get squad members
            members = await service.get_squad_members(str(ctx.guild_id), squad.id)
            
            embed = hikari.Embed(
                title=f"🏆 {squad.name}",
                color=hikari.Color(0x3b82f6),
                timestamp=datetime.now()
            )
            
            if squad.description:
                embed.description = squad.description
            
            # Squad statistics
            member_count = len(members)
            embed.add_field(
                "Members", 
                f"{member_count}" + (f"/{squad.max_members}" if squad.max_members else ""), 
                inline=True
            )
            embed.add_field("Switch Cost", f"{squad.switch_cost:,} bytes", inline=True)
            embed.add_field(
                "Status", 
                "🟢 Active" if squad.is_active else "🔴 Inactive", 
                inline=True
            )
            
            # User membership info
            if user_squad_response.member_since:
                embed.add_field(
                    "Member Since", 
                    user_squad_response.member_since.strftime("%B %d, %Y"), 
                    inline=True
                )
                
                # Calculate membership duration
                membership_duration = datetime.now() - user_squad_response.member_since
                days = membership_duration.days
                if days > 0:
                    embed.add_field(
                        "Membership Duration",
                        f"{days} day{'s' if days != 1 else ''}",
                        inline=True
                    )
            
            # Show recent members (up to 10)
            if members:
                member_names = []
                for member in members[:10]:
                    if member.username:
                        member_names.append(member.username)
                    else:
                        try:
                            discord_member = ctx.get_guild().get_member(int(member.user_id))
                            if discord_member:
                                member_names.append(discord_member.display_name)
                            else:
                                member_names.append(f"User {member.user_id[:8]}")
                        except:
                            member_names.append(f"User {member.user_id[:8]}")
                
                if member_names:
                    members_text = "\n".join(member_names)
                    if len(members_text) > 1024:  # Discord field limit
                        members_text = members_text[:1000] + "\n... (truncated)"
                    embed.add_field("Squad Members", members_text, inline=False)
            
            # Squad creation info
            if squad.created_at:
                embed.set_footer(f"Squad created on {squad.created_at.strftime('%B %d, %Y')}")
            
        except ServiceError as e:
            logger.error(f"Service error in squad info command: {e}")
            embed = create_error_embed("Failed to get squad information. Please try again later.")
        except Exception as e:
            logger.exception(f"Unexpected error in squad info command: {e}")
            embed = create_error_embed("An unexpected error occurred. Please try again later.")
            
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


@squads_group.register
class MembersCommand(
    lightbulb.SlashCommand,
    name="members",
    description="View members of a specific squad or your current squad"
):
    squad_name = lightbulb.string("squad", "Name of the squad to view (leave empty for your squad)", required=False)
    
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        """Handle squad members command - show squad member list."""
        service: SquadsService = ctx.app.d.squads_service
        
        if not service:
            embed = create_error_embed("Bot services are not initialized. Please try again later.")
            await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        
        try:
            target_squad = None
            
            if self.squad_name:
                # Find squad by name
                squads = await service.list_squads(str(ctx.guild_id))
                target_squad = next(
                    (s for s in squads if s.name.lower() == self.squad_name.lower()), 
                    None
                )
                
                if not target_squad:
                    embed = create_error_embed(f"Squad '{self.squad_name}' not found!")
                    await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
                    return
            else:
                # Use user's current squad
                user_squad_response = await service.get_user_squad(str(ctx.guild_id), str(ctx.user.id))
                
                if not user_squad_response.is_in_squad:
                    embed = create_error_embed("You are not in any squad! Specify a squad name to view its members.")
                    await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
                    return
                
                target_squad = user_squad_response.squad
            
            # Get squad members
            members = await service.get_squad_members(str(ctx.guild_id), target_squad.id)
            
            embed = hikari.Embed(
                title=f"👥 {target_squad.name} Members",
                color=hikari.Color(0x3b82f6),
                timestamp=datetime.now()
            )
            
            if not members:
                embed.description = "This squad has no members."
            else:
                embed.description = f"**{len(members)}** member{'s' if len(members) != 1 else ''}"
                
                # Build member list
                member_lines = []
                for i, member in enumerate(members, 1):
                    # Get Discord member info
                    try:
                        discord_member = ctx.get_guild().get_member(int(member.user_id))
                        if discord_member:
                            name = discord_member.display_name
                            status_emoji = "🟢" if discord_member.presence and discord_member.presence.visible_status == hikari.Status.ONLINE else "⚫"
                        else:
                            name = member.username if member.username else f"User {member.user_id[:8]}"
                            status_emoji = "⚫"
                    except:
                        name = member.username if member.username else f"User {member.user_id[:8]}"
                        status_emoji = "⚫"
                    
                    # Format join date
                    joined_text = ""
                    if member.joined_at:
                        joined_text = f" (joined {member.joined_at.strftime('%m/%d/%y')})"
                    
                    member_lines.append(f"{status_emoji} **{i}.** {name}{joined_text}")
                
                # Split into multiple fields if needed
                members_text = "\n".join(member_lines)
                if len(members_text) > 1024:  # Discord field limit
                    # Split into multiple fields
                    current_field = ""
                    field_count = 1
                    
                    for line in member_lines:
                        if len(current_field + line + "\n") > 1000:
                            embed.add_field(
                                f"Members ({field_count})",
                                current_field,
                                inline=False
                            )
                            current_field = line + "\n"
                            field_count += 1
                        else:
                            current_field += line + "\n"
                    
                    if current_field:
                        embed.add_field(
                            f"Members ({field_count})",
                            current_field,
                            inline=False
                        )
                else:
                    embed.add_field("Members", members_text, inline=False)
            
            # Squad info
            embed.add_field("Switch Cost", f"{target_squad.switch_cost:,} bytes", inline=True)
            embed.add_field(
                "Status", 
                "🟢 Active" if target_squad.is_active else "🔴 Inactive", 
                inline=True
            )
            if target_squad.max_members:
                embed.add_field("Max Members", str(target_squad.max_members), inline=True)
            
        except ServiceError as e:
            logger.error(f"Service error in squad members command: {e}")
            embed = create_error_embed("Failed to get squad members. Please try again later.")
        except Exception as e:
            logger.exception(f"Unexpected error in squad members command: {e}")
            embed = create_error_embed("An unexpected error occurred. Please try again later.")
            
        await ctx.respond(embed=embed, flags=hikari.MessageFlag.EPHEMERAL)


def load(bot: lightbulb.BotApp) -> None:
    """Load the squads plugin."""
    # Get client from bot (v3 syntax)
    client = lightbulb.client_from_app(bot)
    client.register(squads_group)
    logger.info("Squads plugin loaded")


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the squads plugin."""
    client = lightbulb.client_from_app(bot)
    client.unregister(squads_group)
    logger.info("Squads plugin unloaded")