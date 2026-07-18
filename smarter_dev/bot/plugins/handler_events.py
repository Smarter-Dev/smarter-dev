"""Bot-side event dispatch for the agentic handler system.

Listens for user messages and reactions and, when a channel has an event handler
installed, asks the web API to enqueue a fire. Two invariants enforced here:

- ONLY user actions fire triggers. The bot's own messages and reactions are
  ignored, which structurally prevents trigger loops (bot reacts -> fires ->
  reacts -> ...).
- The hot path stays cheap: a short-TTL in-memory cache of which (channel,
  trigger) pairs have a handler means we don't hit the API on every message —
  only when the channel actually has a handler.

Actual execution (the sandbox, the budget, emitting) happens in the worker; this
just decides whether to dispatch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import hikari
import lightbulb

from smarter_dev.shared.config import get_settings
from smarter_dev.web.handler_caps import GUILD_MEMBER_EVENTS_PER_MIN, WINDOW_SECONDS

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("handler_events")

ACTIVITY_FLUSH_SECONDS = 30.0


class ActivityBatcher:
    """Collects (guild, user) -> latest message time and flushes in one call.

    Every human guild message is recorded (not just handler channels) so the
    activity facts in handler contexts reflect real guild-wide activity. One
    API call per flush interval instead of one per message; a failed flush
    re-queues its events without regressing anything newer.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], datetime] = {}

    def record(self, guild_id: str, user_id: str, message_at: datetime) -> None:
        key = (guild_id, user_id)
        current = self._pending.get(key)
        if current is None or message_at > current:
            self._pending[key] = message_at

    async def flush(self, api: Any) -> None:
        if not self._pending:
            return
        taken, self._pending = self._pending, {}
        events = [
            {"guild_id": g, "user_id": u, "message_at": at.isoformat()}
            for (g, u), at in taken.items()
        ]
        try:
            await api.post("/activity/batch", json_data={"events": events})
        except Exception:  # noqa: BLE001 — activity is best-effort; keep for retry
            logger.debug("activity flush failed; re-queueing", exc_info=True)
            for (g, u), at in taken.items():
                self.record(g, u, at)

    async def run(self, api: Any) -> None:
        while True:
            await asyncio.sleep(ACTIVITY_FLUSH_SECONDS)
            await self.flush(api)


_activity = ActivityBatcher()


_DISCORD_EPOCH_MS = 1420070400000


def _snowflake_created_at(snowflake: int) -> str:
    """ISO-8601 UTC creation time encoded in a Discord snowflake id."""
    from datetime import datetime, timezone

    ms = (snowflake >> 22) + _DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


class ActiveChannelsCache:
    """Short-TTL cache of which (channel, trigger) and (guild, trigger) fire.

    ``_pairs`` covers standard + channel-scoped admin handlers; ``_guild_triggers``
    covers admin handlers scoped to all channels in a guild. The two
    ``_bot_message_*`` sets are the bot-message opt-in guard: which channels /
    guilds have a message handler with ``include_bot_messages``, so a bot/webhook
    message only ever POSTs /dispatch where some handler asked for it.
    """

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._pairs: set[tuple[str, str]] = set()
        self._guild_triggers: set[tuple[str, str]] = set()
        self._bot_message_channels: set[str] = set()
        self._bot_message_guilds: set[str] = set()
        self._expires_at: float = 0.0

    def invalidate(self) -> None:
        self._expires_at = 0.0

    async def _refresh(self, api: Any) -> None:
        resp = await api.get("/handlers/active-channels")
        if resp.status_code < 400:
            data = resp.json()
            self._pairs = {(str(c), str(t)) for c, t in data.get("channels", [])}
            self._guild_triggers = {
                (str(g), str(t)) for g, t in data.get("guild_triggers", [])
            }
            self._bot_message_channels = {
                str(c) for c in data.get("bot_message_channels", [])
            }
            self._bot_message_guilds = {
                str(g) for g in data.get("bot_message_guild_triggers", [])
            }
            self._expires_at = time.monotonic() + self._ttl

    async def _ensure_fresh(self, api: Any) -> bool:
        """Refresh the cache when stale; False when the refresh failed."""
        if time.monotonic() >= self._expires_at:
            try:
                await self._refresh(api)
            except Exception:  # noqa: BLE001 — never let dispatch crash on a cache miss
                logger.debug("active-channels refresh failed", exc_info=True)
                return False
        return True

    async def has(
        self, api: Any, channel_id: str, guild_id: str, trigger_type: str
    ) -> bool:
        if not await self._ensure_fresh(api):
            return False
        return (
            (str(channel_id), str(trigger_type)) in self._pairs
            or (str(guild_id), str(trigger_type)) in self._guild_triggers
        )

    async def guilds_with_trigger(self, api: Any, trigger_type: str) -> set[str]:
        """Guild ids with an enabled guild-wide handler for ``trigger_type``.

        Backs DM routing: a DM carries no guild, so it fans out only to mutual
        guilds that actually have a ``dm_message`` handler. A refresh failure
        yields an empty set (fail closed — no dispatch, never a crash)."""
        if not await self._ensure_fresh(api):
            return set()
        return {g for g, t in self._guild_triggers if t == str(trigger_type)}

    async def has_bot_message(
        self, api: Any, channel_id: str, guild_id: str
    ) -> bool:
        """Whether a bot/webhook message here fires some opted-in handler.

        True only when the channel (or the whole guild, for a guild-wide admin
        handler) has a message handler with ``include_bot_messages``. A refresh
        failure returns False — the same fail-safe as ``has``."""
        if not await self._ensure_fresh(api):
            return False
        return (
            str(channel_id) in self._bot_message_channels
            or str(guild_id) in self._bot_message_guilds
        )


_cache = ActiveChannelsCache()
_api_client: Any = None


def _get_api_client() -> Any:
    global _api_client
    if _api_client is None:
        from smarter_dev.bot.services.api_client import APIClient

        settings = get_settings()
        _api_client = APIClient(
            base_url=settings.api_base_url, api_key=settings.bot_api_key
        )
    return _api_client


async def _dispatch(
    channel_id: str,
    guild_id: str,
    trigger_type: str,
    context: dict,
    *,
    bot_message: bool = False,
) -> None:
    api = _get_api_client()
    # A bot/webhook message uses the opt-in guard (only fires where a handler
    # asked for bot messages); everything else uses the normal per-trigger guard.
    present = (
        await _cache.has_bot_message(api, channel_id, guild_id)
        if bot_message
        else await _cache.has(api, channel_id, guild_id, trigger_type)
    )
    if not present:
        return
    try:
        await api.post(
            "/handlers/dispatch",
            json_data={
                "guild_id": guild_id,
                "channel_id": channel_id,
                "trigger_type": trigger_type,
                "trigger_context": context,
            },
        )
    except Exception:  # noqa: BLE001 — dispatch is best-effort for a toy
        logger.debug("handler dispatch failed", exc_info=True)


# Channel types that are threads (dispatch keys off their PARENT channel).
_THREAD_CHANNEL_TYPES = (
    hikari.ChannelType.GUILD_PUBLIC_THREAD,
    hikari.ChannelType.GUILD_PRIVATE_THREAD,
    hikari.ChannelType.GUILD_NEWS_THREAD,
)


def _has_custom_avatar(entity: Any) -> bool:
    """True when the user set a non-default avatar (global or per-guild)."""
    return (
        getattr(entity, "avatar_hash", None) is not None
        or getattr(entity, "guild_avatar_hash", None) is not None
    )


def _iso_or_none(value: Any) -> str | None:
    """ISO-8601 string for a datetime, or None when absent."""
    return value.isoformat() if value is not None else None


def _roles_beyond_everyone(member: Any) -> list[str]:
    """Role ids a member actually holds, excluding @everyone.

    hikari always appends the @everyone role — whose id equals the guild id — to
    ``Member.role_ids``, so it is never a real role for rules-acceptance or
    leave-notice purposes and must be filtered out before either reasons about
    "roles held".
    """
    everyone_role_id = str(getattr(member, "guild_id", "") or "")
    return [str(role_id) for role_id in member.role_ids if str(role_id) != everyone_role_id]


# ---------------------------------------------------------------------------
# Member-event context builders (pure — unit-testable without a gateway)
# ---------------------------------------------------------------------------


def guild_member_counts(guild: Any) -> tuple[int | None, int | None]:
    """(total, human) member counts from the gateway cache, best-effort.

    Returns (None, None) when the guild is not cached; the human count is
    derived from the cached member view and may lag a large guild.
    """
    if guild is None:
        return None, None
    total = getattr(guild, "member_count", None)
    members = guild.get_members()
    human = (
        sum(1 for m in members.values() if not m.is_bot) if members else None
    )
    return total, human


def member_join_context(
    member: Any, guild_member_count: int | None, guild_human_member_count: int | None
) -> dict:
    """member_join context (fires for bots too, flagged is_bot)."""
    return {
        "trigger_type": "member_join",
        "member_id": str(member.id),
        "username": member.username,
        "display_name": member.display_name,
        "is_bot": bool(member.is_bot),
        "account_created_at": _snowflake_created_at(int(member.id)),
        "has_custom_avatar": _has_custom_avatar(member),
        "guild_member_count": guild_member_count,
        "guild_human_member_count": guild_human_member_count,
    }


def member_leave_context(user: Any, old_member: Any, role_names: list[str]) -> dict:
    """member_leave context; informational — fires always, partial on cache miss.

    On cache miss (``old_member`` is None) the join history and roles are
    unknown, so ``joined_at``/``role_ids``/``role_names`` are empty and
    ``cache_incomplete`` is True. ``account_created_at`` is always available
    from the snowflake.
    """
    cache_incomplete = old_member is None
    joined_at = None
    role_ids: list[str] = []
    if old_member is not None:
        joined_at = _iso_or_none(old_member.joined_at)
        role_ids = _roles_beyond_everyone(old_member)
    return {
        "trigger_type": "member_leave",
        "member_id": str(user.id),
        "username": user.username,
        "display_name": getattr(user, "display_name", None) or user.username,
        "is_bot": bool(user.is_bot),
        "account_created_at": _snowflake_created_at(int(user.id)),
        "joined_at": joined_at,
        "role_ids": role_ids,
        "role_names": role_names,
        "cache_incomplete": cache_incomplete,
    }


def _rules_accepted_context(member: Any) -> dict:
    return {
        "trigger_type": "member_rules_accepted",
        "member_id": str(member.id),
        "username": member.username,
        "display_name": member.display_name,
        "nickname": member.nickname,
        "account_created_at": _snowflake_created_at(int(member.id)),
        "has_custom_avatar": _has_custom_avatar(member),
        "joined_at": _iso_or_none(member.joined_at),
    }


def _became_rules_accepted(old_member: Any, new_member: Any) -> bool:
    """Whether this update is the member's rules-acceptance moment.

    With cached history: fires on the pending True -> False transition. On
    cache miss uses the at-least-once heuristic — fire iff the member is not
    pending and holds no roles beyond @everyone (role_ids excludes @everyone).
    Duplicate fires are possible on cache misses; handlers must be idempotent.
    """
    if new_member.is_pending:
        return False
    if old_member is None:
        return not _roles_beyond_everyone(new_member)
    return bool(old_member.is_pending)


def member_update_deltas(old_member: Any, new_member: Any) -> list[tuple[str, dict]]:
    """Which member_* triggers a MemberUpdate fires, with pure (id-level) context.

    Returns a list of (trigger_type, context) pairs. ``member_role_change``
    contexts carry only the id-level delta here; the listener enriches them
    with guild-derived role names, boost flags, and counts. A cache miss (no
    ``old_member``) yields no role_change delta — the structural boost re-fire
    guard.
    """
    deltas: list[tuple[str, dict]] = []
    if _became_rules_accepted(old_member, new_member):
        deltas.append(("member_rules_accepted", _rules_accepted_context(new_member)))
    if old_member is not None:
        old_ids = {str(r) for r in old_member.role_ids}
        new_ids = {str(r) for r in new_member.role_ids}
        added = [str(r) for r in new_member.role_ids if str(r) not in old_ids]
        removed = [str(r) for r in old_member.role_ids if str(r) not in new_ids]
        if added or removed:
            deltas.append(
                (
                    "member_role_change",
                    {
                        "trigger_type": "member_role_change",
                        "member_id": str(new_member.id),
                        "member_display_name": new_member.display_name,
                        "added_role_ids": added,
                        "removed_role_ids": removed,
                    },
                )
            )
    return deltas


# ---------------------------------------------------------------------------
# E6 — startup rules-acceptance replay (bot-core, trigger synthesis only)
# ---------------------------------------------------------------------------


def find_missed_rules_acceptances(members: Any) -> list[dict]:
    """Members whose pending -> accepted transition the bot may have missed.

    Pure selector over a member snapshot (gateway cache or REST page). A member
    who is NOT pending, holds no role beyond @everyone, and is not a bot has
    accepted the rules yet shows no sign of onboarding — exactly the population
    the legacy ready-sweep repaired for events lost while the bot was down. Each
    result is a member_rules_accepted context identical to the live one apart from
    ``is_reconciliation: True``, so a handler (and the judge) can tell a replayed
    fire from a real delta. No side effects: selection only.
    """
    contexts: list[dict] = []
    for member in members:
        if member.is_bot:
            continue
        if member.is_pending:
            continue
        if _roles_beyond_everyone(member):
            continue
        contexts.append(
            {**_rules_accepted_context(member), "is_reconciliation": True}
        )
    return contexts


async def replay_missed_rules_acceptances(
    guild_id: str,
    members: Any,
    dispatch: Any,
    *,
    batch_size: int = GUILD_MEMBER_EVENTS_PER_MIN,
    window_seconds: float = WINDOW_SECONDS,
    sleep: Any = asyncio.sleep,
) -> int:
    """Re-dispatch a synthetic member_rules_accepted for each missed member, paced.

    Fires go through the SAME ``dispatch`` path a live delta uses, so the
    onboarding handler stays the single authority. The per-guild
    ``GUILD_MEMBER_EVENTS_PER_MIN`` gate would *decline* a burst — and a decline
    here loses the member a second time — so we pace instead: at most one window's
    worth of fires, then wait for the window to roll over before the next batch. A
    post-downtime backlog therefore drains over a few minutes rather than firing
    at once. ``sleep``/``batch_size``/``window_seconds`` are injected so pacing is
    testable without real sleeps. Returns the number of fires dispatched.
    """
    contexts = find_missed_rules_acceptances(members)
    for index, context in enumerate(contexts):
        if index and index % batch_size == 0:
            # This window is full; wait for headroom before the next batch so the
            # raid gate never declines a replay (which would drop the member again).
            await sleep(window_seconds)
        await dispatch("", guild_id, "member_rules_accepted", context)
    if contexts:
        logger.info(
            "Startup replay: synthesized %d member_rules_accepted fires for guild %s",
            len(contexts),
            guild_id,
        )
    return len(contexts)


async def _paged_guild_members(bot: Any, guild_id: Any) -> list:
    """All members of a guild via REST paging (authoritative, chunk-independent).

    Gateway member chunking is requested at connect (``bot.start`` runs with the
    default ``chunk_members=True`` and the GUILD_MEMBERS intent), but chunks
    arrive asynchronously and are not guaranteed complete at StartedEvent, so the
    member cache cannot be relied on at replay time. REST paging returns the full
    membership regardless of chunk state.
    """
    return [member async for member in bot.rest.fetch_members(guild_id)]


async def replay_startup_rules_acceptances(bot: Any) -> None:
    """Run the missed-rules-acceptance replay for every guild the bot is in.

    Each guild paces against its own per-guild window. A REST failure for one
    guild is logged and skipped so it cannot abort the replay for the rest.
    """
    try:
        guild_ids = list(bot.cache.get_guilds_view().keys())
    except Exception:  # noqa: BLE001 — a cold guild view must not crash startup
        logger.debug("startup replay: guild view unavailable", exc_info=True)
        return
    for guild_id in guild_ids:
        try:
            members = await _paged_guild_members(bot, guild_id)
        except Exception:  # noqa: BLE001 — one guild's fetch must not drop the rest
            logger.warning(
                "startup replay: member fetch failed for guild %s",
                guild_id,
                exc_info=True,
            )
            continue
        await replay_missed_rules_acceptances(str(guild_id), members, _dispatch)


def resolve_role_names(guild: Any, role_ids: list[str]) -> list[str]:
    """Resolve role ids to names via the guild cache; empty when guild uncached.

    An id whose role is not cached falls back to the id string so a name list
    is never silently short.
    """
    if guild is None:
        return []
    names: list[str] = []
    for role_id in role_ids:
        role = guild.get_role(int(role_id))
        names.append(role.name if role is not None else str(role_id))
    return names


def _includes_boost_role(guild: Any, role_ids: list[str]) -> bool:
    if guild is None:
        return False
    for role_id in role_ids:
        role = guild.get_role(int(role_id))
        if role is not None and getattr(role, "is_premium_subscriber_role", False):
            return True
    return False


def _boosting_member_count(guild: Any) -> int | None:
    if guild is None:
        return None
    members = guild.get_members()
    if not members:
        return None
    return sum(
        1 for m in members.values() if getattr(m, "premium_since", None) is not None
    )


def _role_member_counts(guild: Any, role_ids: set[str]) -> dict[str, int]:
    if guild is None:
        return {}
    members = guild.get_members()
    counts: dict[str, int] = {}
    for role_id in role_ids:
        target = int(role_id)
        counts[str(role_id)] = sum(
            1 for m in members.values() if target in {int(r) for r in m.role_ids}
        )
    return counts


def enrich_role_change_context(
    context: dict, old_member: Any, new_member: Any, guild: Any
) -> dict:
    """Add guild-derived role names, boost flag, and counts to a role_change ctx.

    Pure: returns a new dict, leaving ``context`` untouched.
    """
    added_ids = context["added_role_ids"]
    removed_ids = context["removed_role_ids"]
    return {
        **context,
        "added_role_names": resolve_role_names(guild, added_ids),
        "removed_role_names": resolve_role_names(guild, removed_ids),
        "is_boost_role_added": _includes_boost_role(guild, added_ids),
        "premium_subscription_count": (
            getattr(guild, "premium_subscription_count", None)
            if guild is not None
            else None
        ),
        "boosting_member_count": _boosting_member_count(guild),
        "role_member_counts": _role_member_counts(
            guild, set(added_ids) | set(removed_ids)
        ),
    }


# ---------------------------------------------------------------------------
# thread_create context + helpers
# ---------------------------------------------------------------------------


def _is_forum_channel(channel: Any) -> bool:
    return (
        channel is not None
        and getattr(channel, "type", None) == hikari.ChannelType.GUILD_FORUM
    )


def _is_thread_channel(channel: Any) -> bool:
    return channel is not None and getattr(channel, "type", None) in _THREAD_CHANNEL_TYPES


def _resolve_forum_tag_names(forum_channel: Any, tag_ids: list[str]) -> list[str]:
    """Map applied tag ids to names against a forum channel's available tags."""
    if forum_channel is None or not tag_ids:
        return []
    tag_names_by_id = {
        str(tag.id): tag.name
        for tag in getattr(forum_channel, "available_tags", None) or []
    }
    return [tag_names_by_id.get(tag_id, tag_id) for tag_id in tag_ids]


def thread_create_context(
    thread: Any,
    creator: Any,
    starter_message_content: str,
    is_forum_post: bool,
    applied_tag_names: list[str],
) -> dict:
    """thread_create context (regular threads and forum posts alike)."""
    return {
        "trigger_type": "thread_create",
        "thread_id": str(thread.id),
        "thread_name": thread.name,
        "parent_channel_id": str(thread.parent_id),
        "creator_id": str(thread.owner_id) if thread.owner_id is not None else "",
        "creator_username": creator.username if creator is not None else "",
        "creator_display_name": (
            getattr(creator, "display_name", None) or creator.username
            if creator is not None
            else ""
        ),
        "is_forum_post": is_forum_post,
        "applied_tag_ids": [str(t) for t in getattr(thread, "applied_tag_ids", None) or []],
        "applied_tag_names": applied_tag_names,
        "starter_message_content": starter_message_content,
        "created_at": _iso_or_none(getattr(thread, "created_at", None)) or "",
    }


# Guild-level permissions that mark a message author as "staff" for the
# message-trigger context's cheap staff-exemption guard (§3.1). ADMINISTRATOR
# implies every permission, so it counts even without explicit MANAGE_MESSAGES.
_STAFF_MESSAGE_PERMISSIONS = (
    hikari.Permissions.MANAGE_MESSAGES | hikari.Permissions.ADMINISTRATOR
)


def author_has_manage_messages(member: Any) -> bool:
    """Guild-level MANAGE_MESSAGES/ADMINISTRATOR for a message author.

    Fail CLOSED: an uncached (``None``) author or an unresolvable permission set
    reads as NON-staff, so the author is scanned rather than exempted — a
    staff-exemption gate must never fail open. Guild-level only (no per-channel
    overwrites), matching the invite-filter plan's "express staff as roles".
    """
    if member is None:
        return False
    try:
        permissions = lightbulb.utils.permissions_for(member)
    except Exception:  # noqa: BLE001 — an unresolved author is scanned, not exempt
        return False
    return bool(permissions & _STAFF_MESSAGE_PERMISSIONS)


def _category_id_of(channel: Any) -> str | None:
    """Category (parent) id of a guild channel, or None when absent/uncached."""
    if channel is None:
        return None
    parent_id = getattr(channel, "parent_id", None)
    return str(parent_id) if parent_id is not None else None


def resolve_channel_parent_id(bot: Any, channel_id: Any) -> str | None:
    """Category id of the surface a message was posted in, or None (§3.1).

    A top-level channel reports its own parent_id (the category). A thread
    message reports the thread's parent text channel's parent_id — the
    grandparent category — so an invite/private-category check reads the same in
    a thread as in the channel it hangs off. None on any cache miss (fail closed;
    a handler must treat None as "unknown category").
    """
    thread_channel = _get_thread_channel(bot, channel_id)
    if thread_channel is not None:
        return _category_id_of(_get_guild_channel(bot, thread_channel.parent_id))
    return _category_id_of(_get_guild_channel(bot, channel_id))


def message_context(
    msg: Any,
    *,
    author_is_bot: bool,
    author_role_ids: list[str],
    author_has_manage_messages: bool,
    mentioned_user_ids: list[str],
    mentioned_role_ids: list[str],
    mentions_everyone: bool,
    channel_parent_id: str | None,
    author_joined_at: str | None,
    attachments: list[dict],
    thread_fields: dict,
) -> dict:
    """message trigger context (pure — no cache/REST).

    The impure inputs (author roles, guild-level manage-messages, the mention id
    lists off the gateway payload, the category id) are resolved by the caller
    and passed in; this only assembles the dict. The enrichment fields are inert
    for every dispatch — cheap staff-exemption / anti-mention-injection guards
    the auto-mod handlers and the future message_edit trigger reuse.
    """
    return {
        "trigger_type": "message",
        "message_content": msg.content or "",
        "message_id": str(msg.id),
        "author_id": str(msg.author.id),
        "author_name": msg.author.username,
        # True for any non-human author (bot/webhook) that survived the own-bot
        # anti-loop guard; the /dispatch filter keys off it. False for humans.
        "author_is_bot": author_is_bot,
        # For admin handlers that gate on new accounts / recent joiners.
        "author_account_created_at": _snowflake_created_at(int(msg.author.id)),
        "author_joined_at": author_joined_at,
        # §3.1 enrichment — staff-exemption + anti-mention-injection guards.
        "author_role_ids": author_role_ids,
        "author_has_manage_messages": author_has_manage_messages,
        "mentioned_user_ids": mentioned_user_ids,
        "mentioned_role_ids": mentioned_role_ids,
        "mentions_everyone": mentions_everyone,
        "channel_parent_id": channel_parent_id,
        # Files posted with the message — scripts can read these via the
        # gathering agent's web_read tool (it handles image/pdf/audio urls).
        "attachments": attachments,
        **thread_fields,
    }


def message_thread_fields(thread_channel: Any) -> dict:
    """Extra message-context fields naming the enclosing thread (or is_thread=False)."""
    if thread_channel is None:
        return {"is_thread": False}
    return {
        "is_thread": True,
        "thread_id": str(thread_channel.id),
        "thread_name": thread_channel.name,
    }


def _get_guild_channel(bot: Any, channel_id: Any) -> Any:
    try:
        return bot.cache.get_guild_channel(channel_id)
    except Exception:  # noqa: BLE001 — a cache miss must never crash dispatch
        return None


def _get_thread_channel(bot: Any, channel_id: Any) -> Any:
    # Threads live behind cache.get_thread() — get_guild_channel() never returns
    # them — so a message's channel is resolved here, not via _get_guild_channel.
    try:
        channel = bot.cache.get_thread(channel_id)
    except Exception:  # noqa: BLE001 — a cache miss must never crash dispatch
        return None
    return channel if _is_thread_channel(channel) else None


def _get_thread_creator(bot: Any, guild_id: Any, owner_id: Any) -> Any:
    if owner_id is None:
        return None
    try:
        member = bot.cache.get_member(guild_id, owner_id)
        if member is not None:
            return member
        return bot.cache.get_user(owner_id)
    except Exception:  # noqa: BLE001 — best-effort creator resolution
        return None


def _get_starter_message_content(bot: Any, message_id: Any) -> str:
    # For a forum post the starter message's id equals the thread's id.
    try:
        message = bot.cache.get_message(message_id)
    except Exception:  # noqa: BLE001 — starter content is best-effort
        return ""
    return getattr(message, "content", None) or "" if message is not None else ""


@plugin.listener(hikari.StartedEvent)
async def start_activity_flush(_: hikari.StartedEvent) -> None:
    asyncio.create_task(_activity.run(_get_api_client()))


@plugin.listener(hikari.StartedEvent)
async def start_rules_acceptance_replay(_: hikari.StartedEvent) -> None:
    # Pace inside a background task: the replay sleeps between rate-limit windows
    # while a backlog drains, and must not block other StartedEvent listeners.
    asyncio.create_task(replay_startup_rules_acceptances(plugin.bot))


async def dispatch_message(bot: Any, event: Any) -> None:
    msg = event.message
    me = bot.get_me()
    # Structural anti-loop invariant: the smarter-dev bot's OWN messages never
    # fire a handler (post -> fire -> post -> ...), dropped before any opt-in.
    if me is not None and msg.author.id == me.id:
        return
    # A non-human (bot/webhook) message fires only opted-in handlers. When
    # get_me() is None (pre-READY) the own-bot invariant can't be verified, so a
    # bot message is dropped — fail closed. Human messages fire unchanged.
    is_bot_message = not event.is_human
    if is_bot_message and me is None:
        return
    if not is_bot_message:
        # Activity facts stay human-only — a bot/webhook is not a guild member.
        _activity.record(
            str(event.guild_id),
            str(event.message.author.id),
            datetime.now(timezone.utc),
        )
    joined_at = None
    if event.member is not None and event.member.joined_at is not None:
        joined_at = event.member.joined_at.isoformat()
    attachments = [
        {
            "url": a.url,
            "content_type": a.media_type or "",
            "filename": a.filename or "",
        }
        for a in msg.attachments
    ]
    thread_channel = _get_thread_channel(bot, event.channel_id)
    author_role_ids = (
        _roles_beyond_everyone(event.member) if event.member is not None else []
    )
    context = message_context(
        msg,
        author_is_bot=is_bot_message,
        author_role_ids=author_role_ids,
        author_has_manage_messages=author_has_manage_messages(event.member),
        mentioned_user_ids=[str(x) for x in msg.user_mentions_ids],
        mentioned_role_ids=[str(x) for x in msg.role_mention_ids],
        mentions_everyone=bool(msg.mentions_everyone),
        channel_parent_id=resolve_channel_parent_id(bot, event.channel_id),
        author_joined_at=joined_at,
        attachments=attachments,
        thread_fields=message_thread_fields(thread_channel),
    )
    # A message inside a thread dispatches to the thread's PARENT channel (a
    # single fire whose home channel is the parent, §4); a non-thread message
    # dispatches to its own channel. Either way it's exactly one dispatch.
    dispatch_channel_id = (
        str(thread_channel.parent_id)
        if thread_channel is not None
        else str(event.channel_id)
    )
    await _dispatch(
        dispatch_channel_id,
        str(event.guild_id),
        "message",
        context,
        bot_message=is_bot_message,
    )


@plugin.listener(hikari.GuildMessageCreateEvent)
async def on_message(event: hikari.GuildMessageCreateEvent) -> None:
    await dispatch_message(plugin.bot, event)


@plugin.listener(hikari.MemberCreateEvent)
async def on_member_join(event: hikari.MemberCreateEvent) -> None:
    total, human = guild_member_counts(event.get_guild())
    context = member_join_context(event.member, total, human)
    # Guild-scoped: no home channel (channel_id="").
    await _dispatch("", str(event.guild_id), "member_join", context)


@plugin.listener(hikari.MemberDeleteEvent)
async def on_member_leave(event: hikari.MemberDeleteEvent) -> None:
    guild = event.get_guild()
    old_member = event.old_member
    # Resolve names for the same @everyone-filtered set the context reports, so
    # role_ids and role_names stay aligned.
    role_names = (
        resolve_role_names(guild, _roles_beyond_everyone(old_member))
        if old_member is not None
        else []
    )
    context = member_leave_context(event.user, old_member, role_names)
    await _dispatch("", str(event.guild_id), "member_leave", context)


@plugin.listener(hikari.MemberUpdateEvent)
async def on_member_update(event: hikari.MemberUpdateEvent) -> None:
    guild = event.get_guild()
    for trigger_type, context in member_update_deltas(event.old_member, event.member):
        if trigger_type == "member_role_change":
            context = enrich_role_change_context(
                context, event.old_member, event.member, guild
            )
        await _dispatch("", str(event.guild_id), trigger_type, context)


async def dispatch_thread_create(bot: Any, event: Any) -> None:
    # Discord re-sends THREAD_CREATE when the bot merely gains access to an
    # existing thread; only a genuinely new thread carries newly_created.
    if not getattr(event, "newly_created", True):
        return
    thread = event.thread
    parent_channel = _get_guild_channel(bot, thread.parent_id)
    is_forum_post = _is_forum_channel(parent_channel)
    tag_ids = [str(t) for t in getattr(thread, "applied_tag_ids", None) or []]
    applied_tag_names = _resolve_forum_tag_names(parent_channel, tag_ids)
    creator = _get_thread_creator(bot, event.guild_id, thread.owner_id)
    # A forum post's starter message shares the thread's id (Discord contract).
    starter_content = (
        _get_starter_message_content(bot, thread.id) if is_forum_post else ""
    )
    context = thread_create_context(
        thread, creator, starter_content, is_forum_post, applied_tag_names
    )
    # Dispatch keys off the PARENT channel (§3.3).
    await _dispatch(
        str(thread.parent_id), str(event.guild_id), "thread_create", context
    )


@plugin.listener(hikari.GuildThreadCreateEvent)
async def on_thread_create(event: hikari.GuildThreadCreateEvent) -> None:
    await dispatch_thread_create(plugin.bot, event)


# ---------------------------------------------------------------------------
# message_edit trigger (admin-tier auto-mod; automated-and-command-moderation §3.3)
# ---------------------------------------------------------------------------


def message_edit_context(
    msg: Any,
    *,
    old_content: str,
    author_role_ids: list[str],
    author_has_manage_messages: bool,
    channel_parent_id: str | None,
    author_joined_at: str | None,
    thread_fields: dict,
) -> dict:
    """message_edit trigger context (pure — no cache/REST).

    Carries the message's content NOW (``message_content`` — legacy auto-mod
    only scans the new text) plus the best-effort cached ``old_content`` and the
    same author permission/category enrichment as the message trigger, so a
    handler can apply the same staff-exemption and @everyone guards to an edit
    that it applies to a fresh message.
    """
    return {
        "trigger_type": "message_edit",
        "message_id": str(msg.id),
        "message_content": msg.content or "",
        "old_content": old_content,
        "author_id": str(msg.author.id),
        "author_name": msg.author.username,
        "author_account_created_at": _snowflake_created_at(int(msg.author.id)),
        "author_joined_at": author_joined_at,
        "author_role_ids": author_role_ids,
        "author_has_manage_messages": author_has_manage_messages,
        "channel_parent_id": channel_parent_id,
        **thread_fields,
    }


async def dispatch_message_edit(bot: Any, event: Any) -> None:
    # Bot/webhook edits never fire — reuses the message trigger's is_human guard,
    # preserving the no-loop invariant. For an embed/link-unfurl update Discord
    # reports is_human as UNDEFINED (author unknown); that is falsy, so it is
    # dropped here too.
    if not event.is_human:
        return
    msg = event.message
    new_content = msg.content
    # Suppress no-op edits: Discord re-emits GuildMessageUpdateEvent for
    # link/embed unfurls (content UNDEFINED) and pin/embed-only updates, both
    # with UNCHANGED text — firing on those would turn a rare trigger into a
    # per-message one. Skip when there is no new content, or when the cached old
    # content matches the new content. On a cache miss we cannot compare, so we
    # fire with old_content="" (fail toward scanning — the auto-mod stance).
    if not new_content:
        return
    old_message = event.old_message
    if old_message is not None and old_message.content == new_content:
        return
    old_content = old_message.content or "" if old_message is not None else ""
    # event.member is UNDEFINED (not None) for the author-unknown unfurl case; the
    # is_human guard above already dropped those, so a truthy member is a real one.
    member = event.member or None
    joined_at = None
    if member is not None and member.joined_at is not None:
        joined_at = member.joined_at.isoformat()
    author_role_ids = _roles_beyond_everyone(member) if member is not None else []
    thread_channel = _get_thread_channel(bot, event.channel_id)
    context = message_edit_context(
        msg,
        old_content=old_content,
        author_role_ids=author_role_ids,
        author_has_manage_messages=author_has_manage_messages(member),
        channel_parent_id=resolve_channel_parent_id(bot, event.channel_id),
        author_joined_at=joined_at,
        thread_fields=message_thread_fields(thread_channel),
    )
    # An edit inside a thread dispatches to the thread's PARENT channel — a single
    # fire whose home channel is the parent, so channel-scoped admin handlers
    # catch edits exactly as they catch messages (§4, mirrors dispatch_message).
    dispatch_channel_id = (
        str(thread_channel.parent_id)
        if thread_channel is not None
        else str(event.channel_id)
    )
    await _dispatch(
        dispatch_channel_id, str(event.guild_id), "message_edit", context
    )


@plugin.listener(hikari.GuildMessageUpdateEvent)
async def on_message_edit(event: hikari.GuildMessageUpdateEvent) -> None:
    await dispatch_message_edit(plugin.bot, event)


# ---------------------------------------------------------------------------
# dm_message context + mutual-guild routing (staff-communication-channels.md E1)
# ---------------------------------------------------------------------------


def dm_message_context(message: Any, author: Any) -> dict:
    """dm_message trigger context (pure — no cache/REST).

    A DM has no guild and no guild member, so the context carries only the DM
    channel and the author's user-level fields (NO role ids — there is no guild
    member object at the DM event; context-rails' author_role_ids do not apply
    here). Attachment URLs are best-effort: Discord CDN links are signed and
    expire, so a mirror handler treats them as transient.
    """
    return {
        "trigger_type": "dm_message",
        "content": message.content or "",
        "message_id": str(message.id),
        "dm_channel_id": str(message.channel_id),
        "author_id": str(author.id),
        "author_username": author.username,
        "author_display_name": (
            getattr(author, "display_name", None) or author.username
        ),
        "author_account_created_at": _snowflake_created_at(int(author.id)),
        "attachment_urls": [a.url for a in message.attachments],
    }


def route_dm_guilds(
    mutual_guild_ids: list[str], guilds_with_dm_handlers: set[str]
) -> list[str]:
    """Which guilds a DM fans out to: mutual guilds that have a dm_message handler.

    A DM carries no guild, so it routes to every guild the author shares with the
    bot AND that has an enabled dm_message admin handler (the plan's mutual-guild
    rule — correct for the single-guild reality, degrades safely to N). Order
    follows ``mutual_guild_ids``, deduplicated. An empty result (no mutual guild
    resolved, or none has a handler) means no dispatch — fail closed.
    """
    seen: set[str] = set()
    routed: list[str] = []
    for guild_id in mutual_guild_ids:
        gid = str(guild_id)
        if gid in guilds_with_dm_handlers and gid not in seen:
            seen.add(gid)
            routed.append(gid)
    return routed


def _mutual_guild_ids(bot: Any, author_id: Any) -> list[str]:
    """Guild ids the author shares with the bot, from the gateway member cache.

    A cold or incomplete member cache yields fewer/no guilds, so a legitimate DM
    may be dropped (no mutual guild resolved) — acceptable for the single-guild
    reality. Fails closed: any cache error returns an empty list, never crashes.
    """
    try:
        guild_ids = list(bot.cache.get_guilds_view().keys())
    except Exception:  # noqa: BLE001 — a cache miss must never crash dispatch
        return []
    mutual: list[str] = []
    for guild_id in guild_ids:
        try:
            if bot.cache.get_member(guild_id, author_id) is not None:
                mutual.append(str(guild_id))
        except Exception:  # noqa: BLE001 — one uncached guild must not drop the rest
            continue
    return mutual


async def dispatch_dm_message(bot: Any, event: Any) -> None:
    # Only human DMs relay — is_human (from the MessageCreateEvent base) is False
    # for the bot's own DMs and any other bot, structurally preventing a relay
    # loop and mirroring on_message's own-bot guard.
    if not event.is_human:
        return
    message = event.message
    author = message.author
    context = dm_message_context(message, author)
    mutual_guild_ids = _mutual_guild_ids(bot, author.id)
    handler_guilds = await _cache.guilds_with_trigger(_get_api_client(), "dm_message")
    # One dispatch per routed guild, each with NO home channel (channel_id="")
    # so a broken dm handler's error notice is skipped, never posted into the
    # user's DM (see admin_handlers_jobs / notify_handler_error).
    for guild_id in route_dm_guilds(mutual_guild_ids, handler_guilds):
        await _dispatch("", guild_id, "dm_message", context)


@plugin.listener(hikari.DMMessageCreateEvent)
async def on_dm_message(event: hikari.DMMessageCreateEvent) -> None:
    await dispatch_dm_message(plugin.bot, event)


@plugin.listener(hikari.GuildReactionAddEvent)
async def on_reaction(event: hikari.GuildReactionAddEvent) -> None:
    # Ignore the bot's own reactions — only user actions fire triggers.
    me = plugin.bot.get_me()
    if me is not None and event.user_id == me.id:
        return
    emoji = event.emoji_name or (str(event.emoji_id) if event.emoji_id else "")
    await _dispatch(
        str(event.channel_id),
        str(event.guild_id),
        "reaction",
        {
            "trigger_type": "reaction",
            "reaction_emoji": emoji,
            "reaction_message_id": str(event.message_id),
            "reaction_user_id": str(event.user_id),
        },
    )


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)
    logger.info("Handler events plugin loaded (handler dispatch)")


def unload(bot: lightbulb.BotApp) -> None:
    bot.remove_plugin(plugin)
