"""Build an ``AgentInput`` from live Discord state.

Two entry points:

- ``build_initial_input`` — used on the first activation of an engagement
  (the @mention or reply that wakes the engine). Pulls the last ~10 channel
  messages so the agent can read the room, and merges in durable memory
  (``topic`` + ``notes``) from Redis.

- ``build_followup_input`` — used on every subsequent turn. Takes a snapshot
  of hikari messages already pulled out of the engine's queue and only
  passes those forward — older context lives in the agent's Pydantic AI
  message history.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import hikari

from smarter_dev.bot.agents.chat_models import (
    AgentInput,
    Author,
    ChannelInfo,
    Me,
    Message,
)
from smarter_dev.bot.services.chat_memory import ChatMemory
from smarter_dev.bot.utils.messages import (
    fetch_channel_info,
    fetch_user_roles,
    resolve_mentions,
)

logger = logging.getLogger(__name__)

CONTEXT_MESSAGE_LIMIT = 10


async def build_initial_input(
    *,
    bot: hikari.GatewayBot,
    channel_id: int,
    guild_id: int,
    memory: ChatMemory,
) -> AgentInput:
    """Build the ``AgentInput`` for the first activation of an engagement.

    Pulls the last ``CONTEXT_MESSAGE_LIMIT`` channel messages so the agent
    can read the room, and merges in any non-stale ``topic`` and ``notes``
    from durable memory.
    """
    raw_messages = await _fetch_recent_messages(
        bot, channel_id, CONTEXT_MESSAGE_LIMIT
    )
    topic = await memory.topic_for_activation(channel_id)
    notes = await memory.get_notes(channel_id)
    return await _build_input(
        bot=bot,
        channel_id=channel_id,
        guild_id=guild_id,
        raw_messages=raw_messages,
        is_initial_activation=True,
        topic=topic,
        notes=notes,
    )


async def build_followup_input(
    *,
    bot: hikari.GatewayBot,
    channel_id: int,
    guild_id: int,
    queued: list[hikari.Message],
) -> AgentInput:
    """Build the ``AgentInput`` for a follow-up turn inside an active engagement.

    Only the queued messages (already drained from the engine queue) are
    surfaced. The rest of the conversation lives in the agent's Pydantic AI
    message history.
    """
    return await _build_input(
        bot=bot,
        channel_id=channel_id,
        guild_id=guild_id,
        raw_messages=queued,
        is_initial_activation=False,
        topic=None,
        notes=None,
    )


async def _build_input(
    *,
    bot: hikari.GatewayBot,
    channel_id: int,
    guild_id: int,
    raw_messages: list[hikari.Message],
    is_initial_activation: bool,
    topic: str | None,
    notes: str | None,
) -> AgentInput:
    bot_user = bot.get_me()
    bot_user_id = bot_user.id if bot_user else None

    messages: list[Message] = []
    for msg in raw_messages:
        body = msg.content or ""
        if bot_user_id is not None and msg.author.id == bot_user_id and not body.strip():
            continue
        resolved_body = await resolve_mentions(body, bot, guild_id)
        ref = getattr(msg, "referenced_message", None)
        reply_to = str(ref.id) if ref else None
        reactions = [
            (reaction.emoji.name if hasattr(reaction.emoji, "name") else str(reaction.emoji))
            for reaction in (msg.reactions or [])
        ]
        mentions_bot = False
        if bot_user_id is not None:
            if bot_user_id in (msg.user_mentions_ids or []):
                mentions_bot = True
            elif ref is not None and getattr(ref, "author", None) and ref.author.id == bot_user_id:
                mentions_bot = True
        messages.append(
            Message(
                message_id=str(msg.id),
                author_id=str(msg.author.id),
                reply_to_message_id=reply_to,
                body=resolved_body,
                reactions=reactions,
                has_attachments=bool(msg.attachments),
                mentions_bot=mentions_bot,
            )
        )

    authors = await _build_authors(bot, guild_id, raw_messages)
    channel = await _build_channel_info(bot, channel_id)

    me = Me(
        user_id=str(bot_user.id) if bot_user else "",
        username=bot_user.username if bot_user else "bot",
    )

    return AgentInput(
        me=me,
        new_messages=messages,
        authors=authors,
        channel=channel,
        now_utc=datetime.now(UTC),
        is_initial_activation=is_initial_activation,
        topic=topic,
        notes=notes,
    )


async def _fetch_recent_messages(
    bot: hikari.GatewayBot,
    channel_id: int,
    limit: int,
) -> list[hikari.Message]:
    """Fetch the most recent messages in oldest-first order."""
    fetched: list[hikari.Message] = []
    async for msg in bot.rest.fetch_messages(channel_id).limit(limit):
        fetched.append(msg)
    fetched.reverse()
    return fetched


async def _build_authors(
    bot: hikari.GatewayBot,
    guild_id: int,
    messages: list[hikari.Message],
) -> list[Author]:
    """Build Author entries for everyone referenced in ``messages``."""
    seen: dict[int, hikari.User] = {}
    for msg in messages:
        if msg.author.id not in seen:
            seen[msg.author.id] = msg.author

    guild_roles: dict[int, str] = {}
    try:
        roles = await bot.rest.fetch_roles(guild_id)
        guild_roles = {r.id: r.name for r in roles}
    except Exception as e:
        logger.debug("Could not fetch guild roles for %s: %s", guild_id, e)

    authors: list[Author] = []
    for user_id, user in seen.items():
        nickname: str | None = None
        try:
            member = await bot.rest.fetch_member(guild_id, user_id)
            nickname = member.nickname
        except Exception:
            pass

        role_names = await fetch_user_roles(bot, guild_id, user_id, guild_roles)
        authors.append(
            Author(
                user_id=str(user_id),
                username=user.username,
                nickname=nickname,
                role_names=role_names,
            )
        )
    return authors


async def _build_channel_info(
    bot: hikari.GatewayBot,
    channel_id: int,
) -> ChannelInfo:
    info = await fetch_channel_info(bot, channel_id)
    return ChannelInfo(
        channel_id=str(channel_id),
        name=info.get("channel_name") or f"channel-{channel_id}",
        description=info.get("channel_description"),
    )
