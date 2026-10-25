"""Moderation monitor plugin — watches for role mentions and triggers AI triage.

When a user mentions a monitored role (configured per-guild), the bot:
1. Fetches recent channel history for context
2. Runs the moderation triage agent with guild-configured instructions
3. The agent decides what action to take (timeout, purge, flag, or just message)
4. A channel message is posted with appended user pings
5. The run joins the ping's incident in the moderation gateway, whose one
   report in the mod channel lists what was done; the assessment, flags and
   any failure are added to it

A member's further pings in the same channel within a minute of their first
join that incident as evidence: one incident, one triage run, at most one
channel notice (#384). The window is kept in memory; a restart forgets it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from uuid import UUID

import hikari
import lightbulb

from smarter_dev.bot import leadership
from smarter_dev.bot.agents.mod_tools import TRIAGE_ACTOR
from smarter_dev.bot.agents.mod_tools import triage_notes
from smarter_dev.bot.agents.mod_tools import triage_trigger
from smarter_dev.bot.agents.moderation_agent import run_moderation_agent
from smarter_dev.bot.moderation_port import bot_gateway
from smarter_dev.bot.triage_config import TriageConfig
from smarter_dev.bot.triage_config import load_triage_config
from smarter_dev.bot.utils.messages import gather_message_context
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.report import jump_link

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("mod_monitor")

#: How long after a member's first staff ping in a channel their further
#: pings join its incident instead of opening another.
PING_WINDOW_SECONDS = 60.0

_clock = time.monotonic


@dataclass(slots=True)
class _PingBurst:
    """A member's first staff ping in a channel, which later ones join."""

    message_id: str
    started_at: float
    #: The first ping's incident, once its run has opened it (None if the
    #: run failed first).
    incident: asyncio.Future[UUID | None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


_bursts: dict[tuple[str, str, str], _PingBurst] = {}


def _join_burst(key: tuple[str, str, str], message_id: str) -> tuple[_PingBurst, bool]:
    """The burst this ping belongs to, and whether it is the burst's first ping."""
    now = _clock()
    for stale in [
        k for k, b in _bursts.items() if now - b.started_at >= PING_WINDOW_SECONDS
    ]:
        del _bursts[stale]
    burst = _bursts.get(key)
    if burst is not None:
        return burst, False
    burst = _bursts[key] = _PingBurst(message_id=message_id, started_at=now)
    return burst, True


@plugin.listener(hikari.GuildMessageCreateEvent)
async def on_message_create(event: hikari.GuildMessageCreateEvent) -> None:
    """Listen for messages that mention monitored roles."""
    # Ignore bot messages
    if not event.message.author or event.message.author.is_bot:
        return

    mentioned_role_ids = {str(r) for r in (event.message.role_mention_ids or [])}
    if not mentioned_role_ids:
        return

    # Read the config now, not from a startup cache: a save in the admin UI
    # (which runs in the web process) must reach the very next ping. Only
    # messages that mention a role get this far, so the read is rare.
    guild_id = str(event.guild_id)
    try:
        config = await load_triage_config(guild_id)
    except Exception:
        log_exception(logger, f"Could not read moderation config for guild {guild_id}")
        return
    if config is None or not mentioned_role_ids & config.monitored_role_ids:
        return

    logger.info(
        f"Moderation trigger in guild {guild_id}, channel {event.channel_id} "
        f"by author {event.message.author.id}: message {event.message.id} "
        f"({len(event.message.content or '')} chars)"
    )

    # Decided here, with no await since the burst lookup, so two pings
    # arriving together still agree on which one is first.
    burst, first = _join_burst(
        (guild_id, str(event.channel_id), str(event.message.author.id)),
        str(event.message.id),
    )
    work = (
        _handle_moderation(event, config, burst)
        if first
        else _join_incident(event, config, burst)
    )

    # Run moderation in background to not block the event loop; tracked so a
    # process handing over finishes it rather than dropping it.
    leadership.track(asyncio.create_task(work, name=f"mod_monitor:{event.message.id}"))


def _trigger_for(
    event: hikari.GuildMessageCreateEvent, config: TriageConfig
) -> gw.Trigger:
    channel_id = str(event.channel_id)
    return triage_trigger(
        guild_id=str(event.guild_id),
        channel_id=channel_id,
        trigger_message_id=str(event.message.id),
        trigger_author_id=str(event.message.author.id),
        trigger_author=event.message.author.username,
        trigger_content=event.message.content or "",
        report_channel_id=config.response_channel_id or channel_id,
    )


def _display_name(event: hikari.GuildMessageCreateEvent) -> str | None:
    member = getattr(event.message, "member", None)
    name = getattr(member, "display_name", None) or getattr(
        event.message.author, "display_name", None
    )
    return name if isinstance(name, str) else None


async def _join_incident(
    event: hikari.GuildMessageCreateEvent,
    config: TriageConfig,
    burst: _PingBurst,
) -> None:
    """Add a further ping to its burst's incident; no second triage run."""
    try:
        # Waiting lets the first ping open the incident, so the report links
        # the first ping; if that run stalls, the shared key still joins.
        try:
            incident_id = await asyncio.wait_for(
                asyncio.shield(burst.incident), PING_WINDOW_SECONDS
            )
        except TimeoutError:
            incident_id = None
        trigger = dataclasses.replace(
            _trigger_for(event, config), related_message_ids=(burst.message_id,)
        )
        gateway = bot_gateway(plugin.bot)
        joined = await gateway.ensure_incident(trigger)
        if incident_id is not None and joined != incident_id:
            logger.warning(
                f"Staff ping {event.message.id} joined incident {joined}, "
                f"not its burst's {incident_id}"
            )
        link = jump_link(
            str(event.guild_id), str(event.channel_id), str(event.message.id)
        )
        await gateway.add_notes(
            joined,
            source=TRIAGE_ACTOR,
            notes=[
                (
                    gw.ENTRY_NOTE,
                    f"Another staff ping by {event.message.author.username}: {link}",
                )
            ],
        )
        logger.info(
            f"Staff ping {event.message.id} joined the incident of ping {burst.message_id}"
        )
    except Exception:
        log_exception(
            logger,
            f"Joining staff ping {event.message.id} to its incident failed "
            f"in guild {event.guild_id}",
        )


async def _handle_moderation(
    event: hikari.GuildMessageCreateEvent,
    config: TriageConfig,
    burst: _PingBurst | None = None,
) -> None:
    """Handle a moderation trigger by gathering context and running the triage agent."""
    try:
        guild_id = str(event.guild_id)
        channel_id = str(event.channel_id)
        bot = plugin.bot
        trigger_message_id = str(event.message.id)

        # Gather channel history for context
        context_messages = await gather_message_context(
            bot,
            int(channel_id),
            guild_id=int(guild_id),
            limit=config.context_message_limit,
        )

        # One incident per burst of pings, reported where the triage always
        # reported; the burst's later pings join it (_join_incident).
        trigger = _trigger_for(event, config)
        gateway = bot_gateway(bot)
        incident_id = await gateway.ensure_incident(trigger)
        if burst is not None and not burst.incident.done():
            burst.incident.set_result(incident_id)

        # Run the triage agent — always operate in the INCIDENT channel
        assessment, tracker = await run_moderation_agent(
            bot=bot,
            guild_id=guild_id,
            channel_id=channel_id,  # incident channel, NOT response_channel_id
            trigger_message_content=event.message.content or "",
            trigger_author=event.message.author.username,
            trigger_author_id=str(event.message.author.id),
            trigger_author_display_name=_display_name(event),
            context_messages=context_messages,
            guild_instructions=config.instructions,
            enabled_tools=list(config.enabled_tools),
            trigger_message_id=trigger_message_id,
            trigger=trigger,
        )

        # Post channel message with appended user pings
        if tracker.channel_message:
            impacted_ids = tracker.all_impacted_user_ids
            message_parts = [tracker.channel_message]
            if impacted_ids:
                pings = " ".join(f"<@{uid}>" for uid in sorted(impacted_ids))
                message_parts.append(f"-# {pings}")
            message_parts.append("-# Moderators have been notified")
            full_message = "\n".join(message_parts)

            try:
                await bot.rest.create_message(int(channel_id), full_message)
            except Exception:
                log_exception(logger, f"Failed to send channel message for guild {guild_id}")

        # The incident's report already lists every effect; add what only the
        # run knows.
        await gateway.add_notes(
            incident_id, source=TRIAGE_ACTOR, notes=triage_notes(tracker, assessment)
        )

        logger.info(f"Moderation triage completed for guild {guild_id}: {assessment[:200]}")

    except Exception:
        log_exception(
            logger,
            f"Moderation handling failed for message {event.message.id} "
            f"in guild {event.guild_id}"
        )
    finally:
        if burst is not None and not burst.incident.done():
            burst.incident.set_result(None)


def load(bot: lightbulb.BotApp) -> None:
    """Load the mod monitor plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the mod monitor plugin."""
    bot.remove_plugin(plugin)
