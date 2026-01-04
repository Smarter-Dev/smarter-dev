from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from smarter_dev.bot.services.api_client import APIClient
import hikari
import lightbulb
import logging
from smarter_dev.shared.config import Settings, get_settings

plugin = lightbulb.Plugin("quests")

logger = logging.getLogger(__name__)
settings = get_settings()


## Abstractions
async def defer_ephemeral(ctx):
    await ctx.respond(
        hikari.ResponseType.DEFERRED_MESSAGE_CREATE,
        flags=hikari.MessageFlag.EPHEMERAL,
    )


@plugin.command
@lightbulb.command("quests", "Quest related commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def quests_group() -> None:
    """Quests command group."""
    pass


def initialize_client(settings: Settings, default_timeout=30):
    return APIClient(
        base_url=settings.api_base_url,
        api_key=settings.bot_api_key,
        default_timeout=default_timeout,
    )


@quests_group.child
@lightbulb.command("current", "View current quest information")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def event_command(ctx: lightbulb.Context) -> None:

    logger.info("Quests/current hit")

    try:
        await defer_ephemeral(ctx)

        guild_id = ctx.guild_id
        if guild_id is None:
            await ctx.edit_last_response("This command can only be used in a server.")
            return

        api_client = initialize_client(settings)

        logger.info("Attempting to hit quests/daily/current")

        response = await api_client.get(f"/quests/daily/current?guild_id={guild_id}")

        logger.info("Received response from quests/daily/current:")

        data = response.json()
        quest = data["quest"]

        if data["quest"] is None:
            await ctx.edit_last_response("🗓️ No daily quest yet.\nCheck back later!")
            return

        logger.info("Embedding quests")

        embed = hikari.Embed(
            title="🗓️ Daily Quest",
            description=(
                f"**{quest['title']}**\n\n"
                f"{quest['prompt']}\n\n"
                f"*{quest['hint']}*"
            ),
            color=0x27AE60,
        )

        embed.add_field(
            name="Quest Type",
            value=quest.get("quest_type", "daily"),
            inline=True,
        )

        embed.set_footer(text="View progress with /daily progress")

        await ctx.edit_last_response(embed=embed)

    except Exception as e:
        logger.error(f"Error in /quests current: {e}")
        await ctx.edit_last_response(
            "Failed to load current quest. Please try again later."
        )


def load(bot: lightbulb.BotApp) -> None:
    """Load the challenges plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the challenges plugin."""
    bot.remove_plugin(plugin)
