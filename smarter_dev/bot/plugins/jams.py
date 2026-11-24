"""Code jams: scheduled posts, showcase tags, the Jammer role and teams.

Wiring only. What each piece does lives in ``smarter_dev.bot.jams``.
"""

from __future__ import annotations

import asyncio
import logging

import hikari
import lightbulb

from smarter_dev.bot import jams
from smarter_dev.bot import leadership

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("jams")

POLL_SECONDS = 30.0

_task: asyncio.Task | None = None
_api: jams.JamApi | None = None


def _jam_api() -> jams.JamApi | None:
    """The jam API wrapper, made on first use; None without the bot API."""
    global _api
    if _api is None:
        api_client = plugin.bot.d.get("api_client")
        if api_client is None:
            return None
        _api = jams.JamApi(api_client)
    return _api


async def poll_once() -> None:
    """Post what is due, if this process is the one acting."""
    api = _jam_api()
    if api is None or not leadership.is_acting():
        return
    # Tracked: a shutdown waits for a claimed message to be posted and reported.
    await leadership.run_accepted(jams.post_due_messages(api, plugin.bot.rest))


async def _poll_loop() -> None:
    while True:
        try:
            await poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the loop outlives any one failure
            logger.warning("jam message poll failed: %r", error)
        await asyncio.sleep(POLL_SECONDS)


@plugin.listener(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_poll_loop())


@plugin.listener(hikari.GuildThreadCreateEvent)
async def on_thread_create(event: hikari.GuildThreadCreateEvent) -> None:
    api = _jam_api()
    if api is None:
        return
    try:
        await jams.tag_showcase_post(api, plugin.bot.rest, event.thread)
    except Exception as error:  # noqa: BLE001 - tagging never breaks other listeners
        logger.warning("jam entry tagging failed: %r", error)


async def _answer(ctx: lightbulb.Context, work) -> None:
    """Run a command's work and answer only the caller.

    ``work`` takes the jam API and returns the text to answer with. The reply
    is deferred first: the work makes several Discord calls and Discord gives
    an interaction three seconds.
    """
    if ctx.guild_id is None:
        await ctx.respond("Use this in the server.", flags=hikari.MessageFlag.EPHEMERAL)
        return
    await ctx.respond(
        hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=hikari.MessageFlag.EPHEMERAL
    )
    api = _jam_api()
    try:
        text = jams.NOT_SET_UP if api is None else await work(api)
    except Exception as error:  # noqa: BLE001 - the caller always gets an answer
        logger.exception("jam command failed: %r", error)
        text = "Something went wrong. Please try again."
    await ctx.respond(text)


@plugin.command
@lightbulb.command("jam", "Code jam commands")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def jam_group(ctx: lightbulb.Context) -> None:
    pass


@jam_group.child
@lightbulb.command("join", "Join the code jam and get pinged with jam news")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def join_command(ctx: lightbulb.Context) -> None:
    await _answer(
        ctx,
        lambda api: jams.join_jam(
            api, ctx.bot.rest, int(ctx.guild_id), int(ctx.user.id)
        ),
    )


@jam_group.child
@lightbulb.command("leave", "Leave the code jam and stop the jam pings")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def leave_command(ctx: lightbulb.Context) -> None:
    await _answer(
        ctx,
        lambda api: jams.leave_jam(
            api, ctx.bot.rest, int(ctx.guild_id), int(ctx.user.id)
        ),
    )


@jam_group.child
@lightbulb.option("name", "Your team's name", max_length=jams.TEAM_NAME_MAX)
@lightbulb.command("team", "Make a private thread for your jam team")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def team_command(ctx: lightbulb.Context) -> None:
    await _answer(
        ctx,
        lambda api: jams.create_team(
            api, ctx.bot.rest, int(ctx.guild_id), int(ctx.user.id), ctx.options.name
        ),
    )


@jam_group.child
@lightbulb.option("user", "Who to add", type=hikari.User)
@lightbulb.command("invite", "Add a teammate to this team thread")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def invite_command(ctx: lightbulb.Context) -> None:
    me = ctx.bot.get_me()
    target = ctx.options.user
    await _answer(
        ctx,
        lambda api: jams.invite_to_team(
            api,
            ctx.bot.rest,
            guild_id=int(ctx.guild_id),
            channel_id=int(ctx.channel_id),
            bot_user_id=int(me.id) if me else 0,
            target_id=int(target.id),
            target_is_bot=bool(target.is_bot),
        ),
    )


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    global _task, _api
    if _task is not None:
        _task.cancel()
        _task = None
    _api = None
    bot.remove_plugin(plugin)
