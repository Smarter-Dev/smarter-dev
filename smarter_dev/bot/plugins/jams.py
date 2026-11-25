"""Code jams: scheduled posts, showcase entries, the Jammer role and teams.

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
_work_task: asyncio.Task | None = None
_api: jams.JamApi | None = None
_limits = jams.TeamLimits()
_pacer = jams.WorkPacer()


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


async def work_once() -> None:
    """Find missed entries, close teams and count votes, if this process acts.

    A step can take minutes, so only one post's or one team's part is held
    through a shutdown; the rest is picked up again by whichever process acts
    next.
    """
    api = _jam_api()
    if api is None or not leadership.is_acting():
        return
    await jams.run_jam_work(
        api,
        plugin.bot.rest,
        _pacer,
        shield=leadership.run_accepted,
        acting=leadership.is_acting,
    )


async def _loop(once, what: str) -> None:
    while True:
        try:
            await once()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the loop outlives any one failure
            logger.warning("jam %s poll failed: %r", what, error)
        await asyncio.sleep(POLL_SECONDS)


@plugin.listener(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    global _task, _work_task
    if _task is None:
        _task = asyncio.create_task(_loop(poll_once, "message"))
    if _work_task is None:
        # Its own loop: a long vote count never holds up a scheduled message.
        _work_task = asyncio.create_task(_loop(work_once, "work"))


@plugin.listener(hikari.GuildThreadCreateEvent)
async def on_thread_create(event: hikari.GuildThreadCreateEvent) -> None:
    api = _jam_api()
    if api is None:
        return
    try:
        await jams.tag_showcase_post(api, plugin.bot.rest, event.thread)
    except Exception as error:  # noqa: BLE001 - tagging never breaks other listeners
        logger.warning("jam entry tagging failed: %r", error)
    try:
        await jams.record_showcase_post(api, plugin.bot.rest, event.thread)
    except Exception as error:  # noqa: BLE001 - nor does recording the entry
        logger.warning("jam entry recording failed: %r", error)


@plugin.listener(hikari.GuildThreadDeleteEvent)
async def on_thread_delete(event: hikari.GuildThreadDeleteEvent) -> None:
    api = _jam_api()
    if api is None:
        return
    try:
        await jams.forget_showcase_post(
            api, int(event.guild_id), int(event.parent_id), int(event.thread_id)
        )
    except Exception as error:  # noqa: BLE001 - never breaks other listeners
        logger.warning("jam entry removal failed: %r", error)


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
        text = jams.UNAVAILABLE if api is None else await work(api)
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
@lightbulb.command("subscribe", "Get pinged with code jam news")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def subscribe_command(ctx: lightbulb.Context) -> None:
    await _answer(
        ctx,
        lambda api: jams.join_jam(
            api, ctx.bot.rest, int(ctx.guild_id), int(ctx.user.id)
        ),
    )


@jam_group.child
@lightbulb.command("unsubscribe", "Stop the code jam news pings")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def unsubscribe_command(ctx: lightbulb.Context) -> None:
    await _answer(
        ctx,
        lambda api: jams.leave_jam(
            api, ctx.bot.rest, int(ctx.guild_id), int(ctx.user.id)
        ),
    )


async def _me_or_say_starting(ctx: lightbulb.Context) -> hikari.OwnUser | None:
    """The bot's own user, or None after telling the member to try again.

    Without the bot's own id its threads cannot be told from others.
    """
    me = ctx.bot.get_me()
    if me is None:
        await ctx.respond(
            "I am still starting up. Please try again in a minute.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
    return me


@jam_group.child
@lightbulb.option("name", "Your team's name", max_length=jams.TEAM_NAME_MAX)
@lightbulb.command("team", "Make a private thread for your jam team")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def team_command(ctx: lightbulb.Context) -> None:
    me = await _me_or_say_starting(ctx)
    if me is None:
        return
    await _answer(
        ctx,
        lambda api: jams.create_team(
            api,
            ctx.bot.rest,
            _limits,
            int(ctx.guild_id),
            int(ctx.user.id),
            ctx.options.name,
            bot_user_id=int(me.id),
        ),
    )


@jam_group.child
@lightbulb.option("user", "Who to add", type=hikari.User)
@lightbulb.command("invite", "Add a teammate to this team thread")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def invite_command(ctx: lightbulb.Context) -> None:
    target = ctx.options.user
    await _answer(
        ctx,
        lambda api: jams.invite_to_team(
            api,
            ctx.bot.rest,
            guild_id=int(ctx.guild_id),
            channel_id=int(ctx.channel_id),
            user_id=int(ctx.user.id),
            target_id=int(target.id),
            target_is_bot=bool(target.is_bot),
        ),
    )


@jam_group.child
@lightbulb.command("leave", "Leave your jam team")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def leave_command(ctx: lightbulb.Context) -> None:
    await _answer(
        ctx,
        lambda api: jams.leave_team(
            api, ctx.bot.rest, int(ctx.guild_id), int(ctx.user.id)
        ),
    )


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    global _task, _work_task, _api
    for task in (_task, _work_task):
        if task is not None:
            task.cancel()
    _task = _work_task = None
    _api = None
    bot.remove_plugin(plugin)
