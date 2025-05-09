"""
Squads plugin for the Smarter Dev Discord bot.

This plugin provides commands for the Squads system, which allows users to join
squads (roles) based on their bytes balance.
"""

import logging
import asyncio
from typing import Optional, List, Dict, Any

import hikari
import lightbulb
from lightbulb import commands, context

from bot.api_client import APIClient
from bot.api_models import Squad, SquadMember
from bot.plugins.bytes import format_bytes

# Create plugin
squads_plugin = lightbulb.Plugin("Squads")
logger = logging.getLogger("bot.plugins.squads")

# Create a squads command group
@squads_plugin.command
@lightbulb.command("squads", "Commands for managing squads")
@lightbulb.implements(commands.SlashCommandGroup)
async def squads_group(ctx: context.Context) -> None:
    # This is just a command group and doesn't do anything on its own
    pass


@squads_group.child
@lightbulb.command("list", "List all available squads")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def squads_list(ctx: context.SlashContext) -> None:
    """
    List all available squads in the server.
    """
    # Get API client
    client = ctx.bot.d.api_client

    # Get guild ID
    guild_id = ctx.guild_id
    if not guild_id:
        await ctx.respond("This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        # Get all squads for this guild
        response = await client._request("GET", f"/api/squads?guild_id={guild_id}&is_active=true")
        data = await client._get_json(response)

        if not data.get("squads"):
            await ctx.respond("No squads are available in this server.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Create embed
        embed = hikari.Embed(
            title="Available Squads",
            description="Here are the squads you can join in this server:",
            color=hikari.Color.from_rgb(114, 137, 218)  # Discord blurple
        )

        # Add squads to embed
        for squad in data["squads"]:
            role_id = squad["role_id"]
            role = ctx.get_guild().get_role(role_id)
            role_mention = role.mention if role else f"Role ID: {role_id}"

            # Squads no longer have bytes requirements

            # Add description if available
            description = squad.get("description", "No description")

            embed.add_field(
                name=squad['name'],
                value=f"{description}\nRole: {role_mention}",
                inline=False
            )

        await ctx.respond(embed=embed)

    except Exception as e:
        logger.error(f"Error listing squads: {e}")
        await ctx.respond("An error occurred while listing squads. Please try again later.", flags=hikari.MessageFlag.EPHEMERAL)


@squads_group.child
@lightbulb.command("join", "Join a squad")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def squads_join(ctx: context.SlashContext) -> None:
    """
    Show squads the user is eligible to join and let them select one.
    Users need a minimum number of total bytes received to use this command, which is set by server admins.
    """
    # Get API client
    client = ctx.bot.d.api_client

    # Get guild ID
    guild_id = ctx.guild_id
    if not guild_id:
        await ctx.respond("This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        # Get eligible squads for this user
        response = await client._request("GET", f"/api/users/{ctx.author.id}/eligible-squads?guild_id={guild_id}")
        data = await client._get_json(response)

        # Check for error message (not enough bytes to use the command)
        if "error" in data:
            await ctx.respond(data["error"], flags=hikari.MessageFlag.EPHEMERAL)
            return

        if not data.get("squads"):
            await ctx.respond("No squads are available in this server.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Create action row with buttons for each squad
        action_row = ctx.bot.rest.build_message_action_row()
        for i, squad in enumerate(data["squads"]):
            # Limit to 5 buttons per action row (Discord limit)
            if i >= 5:
                break

            # Create a button for each squad
            action_row.add_interactive_button(
                hikari.ButtonStyle.PRIMARY,
                f"join_squad_{squad['id']}",  # Custom ID for the button
                label=squad["name"]
            )

        # Create embed
        embed = hikari.Embed(
            title="Join a Squad",
            description="Select a squad to join:",
            color=hikari.Color.from_rgb(114, 137, 218)  # Discord blurple
        )

        # Add squads to embed
        for squad in data["squads"]:
            role_id = squad["role_id"]
            role = ctx.get_guild().get_role(role_id)
            role_mention = role.mention if role else f"Role ID: {role_id}"

            # Squads no longer have bytes requirements

            # Add description if available
            description = squad.get("description", "No description")

            embed.add_field(
                name=squad['name'],
                value=f"{description}\nRole: {role_mention}",
                inline=False
            )

        # Send message with buttons
        message = await ctx.respond(
            embed=embed,
            component=action_row,
            flags=hikari.MessageFlag.EPHEMERAL
        )

        # Set up a listener for button interactions
        try:
            with ctx.bot.stream(hikari.InteractionCreateEvent, timeout=60.0) as stream:
                async for event in stream:
                    # Make sure it's a button interaction from the same user
                    if not isinstance(event.interaction, hikari.ComponentInteraction):
                        continue
                    if event.interaction.user.id != ctx.author.id:
                        continue
                    if not event.interaction.custom_id.startswith("join_squad_"):
                        continue

                    # Get the squad ID from the button custom ID
                    squad_id = int(event.interaction.custom_id.replace("join_squad_", ""))

                    # Find the squad in the data
                    squad = next((s for s in data["squads"] if s["id"] == squad_id), None)
                    if not squad:
                        await event.interaction.create_initial_response(
                            hikari.ResponseType.MESSAGE_CREATE,
                            "Squad not found. Please try again.",
                            flags=hikari.MessageFlag.EPHEMERAL
                        )
                        continue

                    # Join the squad
                    join_response = await client._request(
                        "POST",
                        f"/api/squads/{squad_id}/members",
                        data={"user_id": ctx.author.id}
                    )

                    if join_response.status_code == 201:
                        # Add the role to the user
                        role_id = squad["role_id"]
                        try:
                            await ctx.bot.rest.add_role_to_member(
                                guild_id,
                                ctx.author.id,
                                role_id,
                                reason=f"Joined squad: {squad['name']}"
                            )
                        except Exception as e:
                            logger.error(f"Error adding role {role_id} to user {ctx.author.id}: {e}")

                        # Send success message
                        await event.interaction.create_initial_response(
                            hikari.ResponseType.MESSAGE_CREATE,
                            f"You have joined the {squad['name']} squad!",
                            flags=hikari.MessageFlag.EPHEMERAL
                        )

                        # Also send a public message
                        role = ctx.get_guild().get_role(role_id)
                        role_mention = role.mention if role else f"Role ID: {role_id}"

                        await ctx.get_channel().send(
                            f"{ctx.author.mention} has joined the {squad['name']} squad! {role_mention}"
                        )
                    else:
                        # Handle error
                        error_data = await client._get_json(join_response)
                        await event.interaction.create_initial_response(
                            hikari.ResponseType.MESSAGE_CREATE,
                            f"Error joining squad: {error_data.get('error', 'Unknown error')}",
                            flags=hikari.MessageFlag.EPHEMERAL
                        )

                    # Break out of the loop after handling the interaction
                    break
        except asyncio.TimeoutError:
            # Handle timeout
            pass

    except Exception as e:
        logger.error(f"Error joining squad: {e}")
        await ctx.respond("An error occurred while joining a squad. Please try again later.", flags=hikari.MessageFlag.EPHEMERAL)


@squads_group.child
@lightbulb.command("leave", "Leave a squad")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def squads_leave(ctx: context.SlashContext) -> None:
    """
    Show squads the user is a member of and let them select one to leave.
    """
    # Get API client
    client = ctx.bot.d.api_client

    # Get guild ID
    guild_id = ctx.guild_id
    if not guild_id:
        await ctx.respond("This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        # Get squads the user is a member of
        response = await client._request("GET", f"/api/users/{ctx.author.id}/squads")
        data = await client._get_json(response)

        if not data.get("squads"):
            await ctx.respond("You are not a member of any squads.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Filter squads to only include those in the current guild
        guild_squads = [s for s in data["squads"] if s["guild_id"] == guild_id]

        if not guild_squads:
            await ctx.respond("You are not a member of any squads in this server.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Create action row with buttons for each squad
        action_row = ctx.bot.rest.build_message_action_row()
        for i, squad in enumerate(guild_squads):
            # Limit to 5 buttons per action row (Discord limit)
            if i >= 5:
                break

            # Create a button for each squad
            action_row.add_interactive_button(
                hikari.ButtonStyle.DANGER,
                f"leave_squad_{squad['id']}",  # Custom ID for the button
                label=squad["name"]
            )

        # Create embed
        embed = hikari.Embed(
            title="Leave a Squad",
            description="Select a squad to leave:",
            color=hikari.Color.from_rgb(114, 137, 218)  # Discord blurple
        )

        # Add squads to embed
        for squad in guild_squads:
            role_id = squad["role_id"]
            role = ctx.get_guild().get_role(role_id)
            role_mention = role.mention if role else f"Role ID: {role_id}"

            # Add description if available
            description = squad.get("description", "No description")

            embed.add_field(
                name=squad["name"],
                value=f"{description}\nRole: {role_mention}",
                inline=False
            )

        # Send message with buttons
        message = await ctx.respond(
            embed=embed,
            component=action_row,
            flags=hikari.MessageFlag.EPHEMERAL
        )

        # Set up a listener for button interactions
        try:
            with ctx.bot.stream(hikari.InteractionCreateEvent, timeout=60.0) as stream:
                async for event in stream:
                    # Make sure it's a button interaction from the same user
                    if not isinstance(event.interaction, hikari.ComponentInteraction):
                        continue
                    if event.interaction.user.id != ctx.author.id:
                        continue
                    if not event.interaction.custom_id.startswith("leave_squad_"):
                        continue

                    # Get the squad ID from the button custom ID
                    squad_id = int(event.interaction.custom_id.replace("leave_squad_", ""))

                    # Find the squad in the data
                    squad = next((s for s in guild_squads if s["id"] == squad_id), None)
                    if not squad:
                        await event.interaction.create_initial_response(
                            hikari.ResponseType.MESSAGE_CREATE,
                            "Squad not found. Please try again.",
                            flags=hikari.MessageFlag.EPHEMERAL
                        )
                        continue

                    # Leave the squad
                    leave_response = await client._request(
                        "DELETE",
                        f"/api/squads/{squad_id}/members/{ctx.author.id}"
                    )

                    if leave_response.status_code == 200:
                        # Remove the role from the user
                        role_id = squad["role_id"]
                        try:
                            await ctx.bot.rest.remove_role_from_member(
                                guild_id,
                                ctx.author.id,
                                role_id,
                                reason=f"Left squad: {squad['name']}"
                            )
                        except Exception as e:
                            logger.error(f"Error removing role {role_id} from user {ctx.author.id}: {e}")

                        # Send success message
                        await event.interaction.create_initial_response(
                            hikari.ResponseType.MESSAGE_CREATE,
                            f"You have left the {squad['name']} squad.",
                            flags=hikari.MessageFlag.EPHEMERAL
                        )
                    else:
                        # Handle error
                        error_data = await client._get_json(leave_response)
                        await event.interaction.create_initial_response(
                            hikari.ResponseType.MESSAGE_CREATE,
                            f"Error leaving squad: {error_data.get('error', 'Unknown error')}",
                            flags=hikari.MessageFlag.EPHEMERAL
                        )

                    # Break out of the loop after handling the interaction
                    break
        except asyncio.TimeoutError:
            # Handle timeout
            pass

    except Exception as e:
        logger.error(f"Error leaving squad: {e}")
        await ctx.respond("An error occurred while leaving a squad. Please try again later.", flags=hikari.MessageFlag.EPHEMERAL)


@squads_group.child
@lightbulb.option("user", "User to check squads for", type=hikari.User, required=False)
@lightbulb.command("info", "Check which squads a user is in")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def squads_info(ctx: context.SlashContext) -> None:
    """
    Check which squads a user is in.
    """
    # Get API client
    client = ctx.bot.d.api_client

    # Get user to check
    target_user = ctx.options.user or ctx.author

    # Get guild ID
    guild_id = ctx.guild_id
    if not guild_id:
        await ctx.respond("This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        # Get squads the user is a member of
        response = await client._request("GET", f"/api/users/{target_user.id}/squads")
        data = await client._get_json(response)

        # Get user's bytes balance
        bytes_response = await client._request("GET", f"/api/bytes/balance/{target_user.id}?guild_id={guild_id}")
        bytes_data = await client._get_json(bytes_response)

        bytes_received = bytes_data.get("bytes_received", 0)
        bytes_balance = bytes_data.get("bytes_balance", 0)

        # Create embed
        embed = hikari.Embed(
            title=f"{target_user.username}'s Squads",
            description=f"Total bytes received: {format_bytes(bytes_received)} (Current balance: {format_bytes(bytes_balance)})",
            color=hikari.Color.from_rgb(114, 137, 218)  # Discord blurple
        )

        # Filter squads to only include those in the current guild
        guild_squads = [s for s in data.get("squads", []) if s["guild_id"] == guild_id]

        if guild_squads:
            squads_text = ""
            for squad in guild_squads:
                role_id = squad["role_id"]
                role = ctx.get_guild().get_role(role_id)
                role_mention = role.mention if role else f"Role ID: {role_id}"

                squads_text += f"• {squad['name']} - {role_mention}\n"

            embed.add_field(
                name="Current Squads",
                value=squads_text,
                inline=False
            )
        else:
            embed.add_field(
                name="Current Squads",
                value="Not a member of any squads in this server.",
                inline=False
            )

        # Get eligible squads
        if target_user.id == ctx.author.id:
            eligible_response = await client._request("GET", f"/api/users/{target_user.id}/eligible-squads?guild_id={guild_id}")
            eligible_data = await client._get_json(eligible_response)

            eligible_squads = eligible_data.get("squads", [])

            if eligible_squads:
                eligible_text = ""
                for squad in eligible_squads:
                    role_id = squad["role_id"]
                    role = ctx.get_guild().get_role(role_id)
                    role_mention = role.mention if role else f"Role ID: {role_id}"

                    eligible_text += f"• {squad['name']} - {role_mention}\n"

                embed.add_field(
                    name="Eligible to Join",
                    value=eligible_text,
                    inline=False
                )

                embed.set_footer(text="Use /squads join to join a squad")

        await ctx.respond(embed=embed)

    except Exception as e:
        logger.error(f"Error checking squads: {e}")
        await ctx.respond("An error occurred while checking squads. Please try again later.", flags=hikari.MessageFlag.EPHEMERAL)


@squads_group.child
@lightbulb.option("squad_name", "Name of the squad to check", type=str, required=True)
@lightbulb.command("members", "List members of a squad")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def squads_members(ctx: context.SlashContext) -> None:
    """
    List members of a squad.
    """
    # Get API client
    client = ctx.bot.d.api_client

    # Get guild ID
    guild_id = ctx.guild_id
    if not guild_id:
        await ctx.respond("This command can only be used in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        # Get all squads for this guild
        response = await client._request("GET", f"/api/squads?guild_id={guild_id}")
        data = await client._get_json(response)

        if not data.get("squads"):
            await ctx.respond("No squads are available in this server.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Find the squad by name
        squad_name = ctx.options.squad_name.lower()
        squad = next((s for s in data["squads"] if s["name"].lower() == squad_name), None)

        if not squad:
            # Try partial match
            squad = next((s for s in data["squads"] if squad_name in s["name"].lower()), None)

        if not squad:
            await ctx.respond(f"Squad '{ctx.options.squad_name}' not found.", flags=hikari.MessageFlag.EPHEMERAL)
            return

        # Get squad members
        members_response = await client._request("GET", f"/api/squads/{squad['id']}/members")
        members_data = await client._get_json(members_response)

        # Create embed
        embed = hikari.Embed(
            title=f"{squad['name']} Squad Members",
            description=squad.get('description', 'No description'),
            color=hikari.Color.from_rgb(114, 137, 218)  # Discord blurple
        )

        if squad.get("description"):
            embed.add_field(
                name="Description",
                value=squad["description"],
                inline=False
            )

        # Add role info
        role_id = squad["role_id"]
        role = ctx.get_guild().get_role(role_id)
        role_mention = role.mention if role else f"Role ID: {role_id}"

        embed.add_field(
            name="Role",
            value=role_mention,
            inline=False
        )

        # Add members
        if members_data.get("members"):
            members_text = ""
            for member in members_data["members"]:
                user = member["user"]
                discord_id = user["discord_id"]

                try:
                    discord_user = await ctx.bot.rest.fetch_user(discord_id)
                    members_text += f"• {discord_user.mention} ({discord_user.username})\n"
                except:
                    members_text += f"• <@{discord_id}> (ID: {discord_id})\n"

                # Limit to 20 members to avoid hitting Discord's character limit
                if len(members_text.split("\n")) > 20:
                    members_text += f"... and {len(members_data['members']) - 20} more"
                    break

            embed.add_field(
                name=f"Members ({len(members_data['members'])})",
                value=members_text or "No members",
                inline=False
            )
        else:
            embed.add_field(
                name="Members",
                value="No members",
                inline=False
            )

        await ctx.respond(embed=embed)

    except Exception as e:
        logger.error(f"Error listing squad members: {e}")
        await ctx.respond("An error occurred while listing squad members. Please try again later.", flags=hikari.MessageFlag.EPHEMERAL)


def load(bot: lightbulb.BotApp) -> None:
    """Load the squads plugin."""
    bot.add_plugin(squads_plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the squads plugin."""
    bot.remove_plugin(squads_plugin)
