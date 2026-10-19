"""Discord forum posts: which forum a post belongs to, and what it is about.

A forum's posts are threads, each with its own channel id, so a settings row
keyed on the forum never matches a message sent in one of its posts. The
proactive agent can be switched on for a whole forum (#248): a post with no
settings row of its own inherits its forum's, and a row for the post itself
(``/proactive off`` inside it) wins. Threads of ordinary text channels do not
inherit.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass

import hikari

from smarter_dev.bot.privacy.blocked_users import get_blocked_users
from smarter_dev.shared.exception_logging import log_exception

logger = logging.getLogger(__name__)

# GUILD_FORUM and GUILD_MEDIA; hikari 2.1 has no name for media (16).
FORUM_CHANNEL_TYPES = frozenset({15, 16})
# News, public and private threads.
THREAD_CHANNEL_TYPES = frozenset({10, 11, 12})
# A post never moves to another forum, so lookups are kept until evicted.
FORUM_PARENT_CACHE_MAX_ENTRIES = 10_000
_BOT_DATA_KEY = "proactive_forum_parents"
# The most one Discord lookup may take, retries and rate-limit waits included:
# these sit on the message, mention, command and wake paths, and hikari's
# own timeout bounds each HTTP attempt, not its rate-limit waits.
LOOKUP_DEADLINE_SECONDS = 5.0
# Shown instead of the title of a post whose author opted out: their words
# never reach a model, and a post's title is theirs (#100).
HIDDEN_POST_TITLE = "a post by someone who opted out"


def thread_title(thread, fallback: str) -> str:
    """A thread's name as a model may see it: hidden when its author's
    words may not reach a model."""
    owner_id = getattr(thread, "owner_id", None)
    if owner_id is not None and get_blocked_users().is_blocked(
        owner_id, getattr(thread, "id", None)
    ):
        return HIDDEN_POST_TITLE
    return getattr(thread, "name", None) or fallback


def _channel_type(channel) -> int | None:
    try:
        return int(getattr(channel, "type", None))
    except (TypeError, ValueError):
        return None


async def resolve_forum_tag_names(
    bot, channel_id: str, tag_ids: list, *, forum_channel=None
) -> list:
    """Resolve forum tag IDs to tag names from the forum's available tags.

    ``forum_channel`` saves the REST fetch when the caller already has the
    forum. Unknown tags come back as ``Unknown-<id>``; when the forum cannot
    be read at all, the IDs come back as strings.
    """
    try:
        if not tag_ids:
            return []
        if forum_channel is None:
            forum_channel = await asyncio.wait_for(
                bot.rest.fetch_channel(channel_id), LOOKUP_DEADLINE_SECONDS
            )

        if not hasattr(forum_channel, "available_tags"):
            logger.warning(
                f"Forum channel {channel_id} has no available_tags attribute"
            )
            return [str(tag_id) for tag_id in tag_ids]  # Fall back to IDs

        tag_map = {str(tag.id): tag.name for tag in forum_channel.available_tags or ()}
        tag_names = []
        for tag_id in tag_ids:
            tag_id_str = str(tag_id)
            if tag_id_str in tag_map:
                tag_names.append(tag_map[tag_id_str])
            else:
                logger.warning(
                    f"Tag ID {tag_id} not found in forum channel available tags"
                )
                tag_names.append(f"Unknown-{tag_id}")
        return tag_names

    except Exception as e:
        logger.error(f"Error resolving forum tag names: {e}")
        return [str(tag_id) for tag_id in tag_ids]  # Fall back to IDs


class ForumParents:
    """Post (thread) id → forum id, from the bot's cache, else Discord REST.

    ``None`` means the channel is not a forum post: a plain channel, a
    text-channel thread, or one Discord says is gone. A lookup Discord could
    not answer (or refused, which a permission change can undo) is not
    remembered, so the next message asks again.
    """

    def __init__(self, bot):
        self.bot = bot
        self._parents: OrderedDict[str, str | None] = OrderedDict()
        # Channel id -> whether it is a forum; a channel's type never changes.
        self._forums: OrderedDict[str, bool] = OrderedDict()

    def known_forum_for(self, channel_id: str) -> str | None:
        """The forum already looked up for this channel; never calls Discord."""
        return self._parents.get(str(channel_id))

    async def forum_for(self, channel_id: str) -> str | None:
        channel_id = str(channel_id)
        if channel_id in self._parents:
            self._parents.move_to_end(channel_id)
            return self._parents[channel_id]
        try:
            forum_id = await self._lookup(channel_id)
        except hikari.NotFoundError:
            forum_id = None
        except Exception:  # noqa: BLE001 — unknown means "not a forum post"
            log_exception(
                logger,
                "forum parent lookup failed channel=%s",
                channel_id,
                level=logging.WARNING,
            )
            return None
        _remember(self._parents, channel_id, forum_id)
        return forum_id

    async def is_forum(self, channel_id: str) -> bool | None:
        """Whether the channel is a forum (or media channel) itself; None
        when Discord could not say, which callers must not act on."""
        try:
            return await self._is_forum(str(channel_id))
        except Exception:  # noqa: BLE001 — unknown, asked again next time
            log_exception(
                logger,
                "forum type lookup failed channel=%s",
                channel_id,
                level=logging.WARNING,
            )
            return None

    async def _is_forum(self, channel_id: str) -> bool:
        """:meth:`is_forum`, raising when Discord could not say."""
        if channel_id in self._forums:
            self._forums.move_to_end(channel_id)
            return self._forums[channel_id]
        try:
            channel = await self.channel(channel_id)
        except hikari.NotFoundError:
            forum = False
        else:
            forum = _channel_type(channel) in FORUM_CHANNEL_TYPES
        _remember(self._forums, channel_id, forum)
        return forum

    async def channel(self, channel_id: str):
        """The channel or thread, from the cache when it is there."""
        cache = getattr(self.bot, "cache", None)
        channel = None
        if cache is not None:
            get_thread = getattr(cache, "get_thread", None)
            if get_thread is not None:
                channel = get_thread(int(channel_id))
            if channel is None:
                channel = cache.get_guild_channel(int(channel_id))
        if channel is None:
            channel = await asyncio.wait_for(
                self.bot.rest.fetch_channel(int(channel_id)), LOOKUP_DEADLINE_SECONDS
            )
        return channel

    async def _lookup(self, channel_id: str) -> str | None:
        thread = await self.channel(channel_id)
        if _channel_type(thread) not in THREAD_CHANNEL_TYPES:
            return None
        parent_id = getattr(thread, "parent_id", None)
        if parent_id is None:
            return None
        if not await self._is_forum(str(parent_id)):
            return None
        return str(parent_id)


def _remember(cache: OrderedDict, key: str, value) -> None:
    cache[key] = value
    while len(cache) > FORUM_PARENT_CACHE_MAX_ENTRIES:
        cache.popitem(last=False)


def forum_parents(bot) -> ForumParents:
    """The bot's one :class:`ForumParents`, kept on ``bot.d``."""
    parents = bot.d.get(_BOT_DATA_KEY)
    if parents is None:
        parents = ForumParents(bot)
        bot.d[_BOT_DATA_KEY] = parents
    return parents


@dataclass(frozen=True)
class EffectiveSettings:
    """The proactive settings that apply to a channel.

    ``forum_id`` is set when they are the post's forum's, inherited because
    the post has no settings row of its own. ``held`` says an enabled
    forum's post is off for now because the consumer that would run it
    cannot run forum posts.
    """

    settings: object
    forum_id: str | None = None
    held: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)


async def effective_settings(
    service, parents: ForumParents, guild_id: str, channel_id: str
) -> EffectiveSettings:
    """The channel's own settings, or its forum's when it is a forum post
    with no row of its own."""
    own = await service.get_settings(guild_id, channel_id)
    # A row for the post itself wins, on or off.
    if own.enabled or getattr(own, "exists", True):
        return EffectiveSettings(own)
    forum_id = await parents.forum_for(channel_id)
    if forum_id is None:
        return EffectiveSettings(own)
    return EffectiveSettings(
        await service.get_settings(guild_id, forum_id), forum_id=forum_id
    )


@dataclass(frozen=True)
class ForumPostInfo:
    """What a post is about: its title and applied tag names."""

    title: str
    tag_names: tuple[str, ...]


def _cached_channel(bot, channel_id: str):
    cache = getattr(bot, "cache", None)
    if cache is None:
        return None
    get_thread = getattr(cache, "get_thread", None)
    channel = get_thread(int(channel_id)) if get_thread is not None else None
    return channel or cache.get_guild_channel(int(channel_id))


def cached_post_info(bot, post_id: str, forum_id: str) -> ForumPostInfo:
    """Title and tags from the bot's cache alone; never calls Discord."""
    post = _cached_channel(bot, post_id)
    forum = _cached_channel(bot, forum_id)
    title = thread_title(post, post_id)
    tag_names = {
        str(tag.id): tag.name for tag in getattr(forum, "available_tags", None) or ()
    }
    return ForumPostInfo(
        title=title,
        tag_names=tuple(
            tag_names[str(tag_id)]
            for tag_id in getattr(post, "applied_tag_ids", None) or ()
            if str(tag_id) in tag_names
        ),
    )


async def post_info(
    parents: ForumParents, post_id: str, forum_id: str
) -> ForumPostInfo:
    """Title and applied tag names, from the cache, else Discord REST.

    Display only: anything Discord will not answer falls back to the id and
    no tags.
    """
    try:
        post = await parents.channel(post_id)
    except Exception:  # noqa: BLE001 — display only
        return ForumPostInfo(title=post_id, tag_names=())
    tag_ids = list(getattr(post, "applied_tag_ids", None) or ())
    tag_names: list = []
    if tag_ids:
        try:
            forum = await parents.channel(forum_id)
        except Exception:  # noqa: BLE001 — display only
            # No tags rather than a second try at the fetch that just failed.
            forum = None
        if forum is not None:
            tag_names = await resolve_forum_tag_names(
                parents.bot, forum_id, tag_ids, forum_channel=forum
            )
    return ForumPostInfo(
        title=thread_title(post, post_id),
        tag_names=tuple(str(name) for name in tag_names),
    )


def forum_post_label(info: ForumPostInfo, forum_name: str) -> str:
    """How the agent sees a post among its channels."""
    tags = f"; tags: {', '.join(info.tag_names)}" if info.tag_names else ""
    return f"{info.title} (post in forum #{forum_name}{tags})"


def new_post_preamble(info: ForumPostInfo) -> str:
    """Prefixed to a post's opening message so its title and tags travel
    with it to the watcher and the agent."""
    tags = f" (tags: {', '.join(info.tag_names)})" if info.tag_names else ""
    return f'[Opened a new forum post titled "{info.title}"{tags}]'
