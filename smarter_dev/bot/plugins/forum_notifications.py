"""Forum notification subscription commands for user tagging system."""

from __future__ import annotations

import logging
import os

import aiohttp
import hikari
import lightbulb

from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)

# Create the plugin
plugin = lightbulb.Plugin("forum_notifications")


async def forum_autocomplete(
    option: hikari.AutocompleteInteractionOption, interaction: hikari.AutocompleteInteraction
) -> list[hikari.CommandChoice]:
    """Autocomplete function to show only forums with tagging-enabled agents."""
    try:
        guild_id = str(interaction.guild_id)

        # Get API client
        api_base_url = os.getenv("API_BASE_URL", "http://localhost:8000")
        api_key = os.getenv("BOT_API_KEY")

        if not api_key:
            logger.error("BOT_API_KEY not found in environment")
            return []

        async with aiohttp.ClientSession() as session:
            # Get agents for this guild
            async with session.get(
                f"{api_base_url}/guilds/{guild_id}/forum-agents",
                headers={"Authorization": f"Bearer {api_key}"}
            ) as response:
                if response.status >= 400:
                    logger.error(f"Failed to get forum agents: {response.status}")
                    return []

                agents = await response.json()

                # Find forums that have agents with user tagging enabled
                tagging_enabled_forums = set()
                for agent in agents:
                    if agent.get("enable_user_tagging", False) and agent.get("monitored_forums"):
                        for forum_id in agent["monitored_forums"]:
                            tagging_enabled_forums.add(forum_id)

                if not tagging_enabled_forums:
                    return []

                # Get guild channels and filter for forum channels with tagging enabled
                choices = []
                guild = interaction.get_guild()
                if guild:
                    for channel in guild.get_channels().values():
                        if (channel.type == hikari.ChannelType.GUILD_FORUM and
                            str(channel.id) in tagging_enabled_forums and
                            channel.name.lower().startswith(option.value.lower() if option.value else "")):
                            choices.append(hikari.CommandChoice(
                                name=f"#{channel.name}",
                                value=str(channel.id)
                            ))

                            # Discord limits to 25 choices
                            if len(choices) >= 25:
                                break

                return choices

    except Exception as e:
        logger.error(f"Error in forum autocomplete: {e}")
        return []


@plugin.command
@lightbulb.option(
    "forum",
    "Select the forum channel to configure notifications for",
    type=hikari.OptionType.STRING,
    autocomplete=forum_autocomplete,
    required=True
)
@lightbulb.option(
    "duration_hours",
    "How long to receive notifications (1, 2, 4, 8, 12, 18, 24, or -1 for forever)",
    type=hikari.OptionType.INTEGER,
    required=False,
    default=4
)
@lightbulb.command(
    "post-notifications",
    "Configure your notification preferences for forum posts by topic"
)
@lightbulb.implements(lightbulb.SlashCommand)
async def post_notifications(ctx: lightbulb.Context) -> None:
    """Configure user notification preferences for forum posts by topic."""

    # Get the forum channel and duration
    forum_channel_id = ctx.options.forum
    duration_hours = ctx.options.duration_hours
    guild_id = str(ctx.guild_id)
    user_id = str(ctx.author.id)

    if not forum_channel_id:
        await ctx.respond("❌ Please select a forum channel.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Get the actual channel object for display purposes
    try:
        forum_channel = ctx.bot.cache.get_guild_channel(int(forum_channel_id))
        if not forum_channel or forum_channel.type != hikari.ChannelType.GUILD_FORUM:
            await ctx.respond("❌ Invalid forum channel selected.", flags=hikari.MessageFlag.EPHEMERAL)
            return
    except (ValueError, AttributeError):
        await ctx.respond("❌ Invalid forum channel selected.", flags=hikari.MessageFlag.EPHEMERAL)
        return

    # Validate duration hours
    if duration_hours not in [1, 2, 4, 8, 12, 18, 24, -1]:
        await ctx.respond("❌ Invalid duration. Must be one of: 1, 2, 4, 8, 12, 18, 24, or -1 (forever)", flags=hikari.MessageFlag.EPHEMERAL)
        return

    try:
        # Get available topics for this forum from API
        settings = get_settings()
        async with APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key,
            default_timeout=30.0
        ) as client:

            # Get available notification topics for this forum
            response = await client.get(
                f"/guilds/{guild_id}/forum-channels/{forum_channel_id}/notification-topics"
            )

            if response.status_code == 404:
                await ctx.respond(
                    f"❌ No notification topics are configured for {forum_channel.mention}.\n"
                    "An administrator needs to set up topics in the admin panel first.",
                    flags=hikari.MessageFlag.EPHEMERAL
                )
                return
            elif response.status_code >= 400:
                error_data = response.json() if response.content else {}
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                await ctx.respond(f"❌ Error loading topics: {error_message}", flags=hikari.MessageFlag.EPHEMERAL)
                return

            topics_data = response.json()
            if not topics_data:
                await ctx.respond(
                    f"❌ No notification topics are available for {forum_channel.mention}.\n"
                    "An administrator needs to set up topics in the admin panel first.",
                    flags=hikari.MessageFlag.EPHEMERAL
                )
                return

            # Get current user subscriptions if any
            current_subscriptions = []

            try:
                subscription_response = await client.get(
                    f"/guilds/{guild_id}/users/{user_id}/forum-subscriptions/{forum_channel_id}"
                )

                if subscription_response.status_code == 200:
                    sub_data = subscription_response.json()
                    current_subscriptions = sub_data.get("subscribed_topics", [])

            except APIError as e:
                # Handle 404 (no existing subscription) gracefully - use defaults
                if e.status_code == 404:
                    logger.debug(f"No existing subscription found for user {user_id} in forum {forum_channel_id} - using defaults")
                else:
                    # Re-raise other API errors
                    raise

            # Create select menu for topic selection
            select_menu = create_topic_select_menu(
                topics_data,
                current_subscriptions,
                forum_channel_id,
                user_id,
                duration_hours
            )

            # Create action row with select menu
            action_row = hikari.impl.MessageActionRowBuilder()
            action_row.add_component(select_menu)

            # Create response with current settings info
            current_info = ""
            if current_subscriptions:
                topics_text = ", ".join(current_subscriptions)
                current_info = f"\n\n**Current Settings:**\n📋 Topics: {topics_text}"

            hours_text = f"{duration_hours} hours" if duration_hours != -1 else "forever"

            await ctx.respond(
                f"🔔 **Configure notifications for {forum_channel.name}**\n\n"
                f"⏰ **Duration**: {hours_text}\n\n"
                f"Select the topics you want to be notified about from the dropdown below. "
                f"You can choose multiple topics or none to disable notifications.{current_info}",
                components=[action_row],
                flags=hikari.MessageFlag.EPHEMERAL
            )

    except Exception as e:
        logger.error(f"Error in post_notifications command: {e}")
        await ctx.respond(f"❌ An error occurred: {e}", flags=hikari.MessageFlag.EPHEMERAL)


def create_topic_select_menu(
    topics_data: list[dict],
    current_subscriptions: list[str],
    forum_channel_id: str,
    user_id: str,
    duration_hours: int
) -> hikari.impl.TextSelectMenuBuilder:
    """Create a select menu for topic selection."""

    select_menu = hikari.impl.TextSelectMenuBuilder(
        custom_id=f"forum_topic_save:{forum_channel_id}:{user_id}:{duration_hours}",
        placeholder="Choose notification topics..." if not current_subscriptions else f"Selected: {', '.join(current_subscriptions[:2])}{'...' if len(current_subscriptions) > 2 else ''}",
        min_values=0,  # Allow selecting none (disable all)
        max_values=min(len(topics_data), 25),  # Discord limit is 25
    )

    # Add options to the select menu
    for topic in topics_data:
        # Check if this topic is currently selected
        is_default = topic["topic_name"] in current_subscriptions

        # Add the option to the select menu
        select_menu.add_option(
            topic["topic_name"],  # label (positional)
            topic["topic_name"],  # value (positional)
            description=topic.get("topic_description", "")[:100] if topic.get("topic_description") else None,
            is_default=is_default
        )

    return select_menu




async def handle_topic_save_interaction(event: hikari.InteractionCreateEvent) -> None:
    """Handle topic selection and immediate save."""
    if not isinstance(event.interaction, hikari.ComponentInteraction):
        return

    if not event.interaction.custom_id.startswith("forum_topic_save:"):
        return

    # Parse custom_id
    custom_id_parts = event.interaction.custom_id.split(":")
    if len(custom_id_parts) != 4:
        logger.error(f"Invalid forum_topic_save custom_id format: {event.interaction.custom_id}")
        return

    forum_channel_id = custom_id_parts[1]
    user_id = custom_id_parts[2]
    duration_hours = int(custom_id_parts[3])

    # Get selected topics
    selected_topics = event.interaction.values or []

    try:
        # Get guild info from interaction
        guild_id = str(event.interaction.guild_id)
        username = f"@{event.interaction.user.username}"

        # Save subscription directly to API
        settings = get_settings()
        async with APIClient(
            base_url=settings.api_base_url,
            api_key=settings.bot_api_key,
            default_timeout=30.0
        ) as client:

            subscription_data = {
                "user_id": user_id,
                "username": username,
                "forum_channel_id": forum_channel_id,
                "subscribed_topics": selected_topics,
                "notification_hours": duration_hours
            }

            response = await client.put(
                f"/guilds/{guild_id}/users/{user_id}/forum-subscriptions/{forum_channel_id}",
                json_data=subscription_data
            )

            if response.status_code >= 400:
                error_data = response.json() if response.content else {}
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    f"❌ Error saving subscription: {error_message}",
                    flags=hikari.MessageFlag.EPHEMERAL
                )
                return

            # Success message
            if selected_topics:
                hours_text = f"{duration_hours} hours" if duration_hours != -1 else "forever"
                topics_text = ", ".join(selected_topics)
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    f"✅ **Notification preferences updated!**\n"
                    f"📋 **Topics**: {topics_text}\n"
                    f"⏰ **Duration**: {hours_text}\n"
                    f"📁 **Forum**: <#{forum_channel_id}>",
                    flags=hikari.MessageFlag.EPHEMERAL
                )
            else:
                await event.interaction.create_initial_response(
                    hikari.ResponseType.MESSAGE_CREATE,
                    f"✅ **Notifications disabled** for <#{forum_channel_id}>",
                    flags=hikari.MessageFlag.EPHEMERAL
                )

    except Exception as e:
        logger.error(f"Error in topic save interaction: {e}")
        try:
            await event.interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                f"❌ An error occurred while saving your preferences: {e}",
                flags=hikari.MessageFlag.EPHEMERAL
            )
        except Exception as e2:
            logger.error(f"Failed to send error response: {e2}")




def load(bot: lightbulb.BotApp) -> None:
    """Load the plugin."""
    bot.add_plugin(plugin)

    # Set up interaction handlers for the simplified select menu flow
    @bot.listen(hikari.InteractionCreateEvent)
    async def on_forum_interactions(event: hikari.InteractionCreateEvent) -> None:
        """Handle forum notification interactions."""
        if isinstance(event.interaction, hikari.ComponentInteraction):
            if event.interaction.custom_id.startswith("forum_topic_save:"):
                await handle_topic_save_interaction(event)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the plugin."""
    bot.remove_plugin(plugin)
