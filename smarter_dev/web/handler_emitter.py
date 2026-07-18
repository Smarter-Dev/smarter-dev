"""Discord REST emitter for the worker tier.

The handler runtime runs in the agent-worker process, which has no gateway
connection — so it emits to Discord over the REST API with the bot token. This
is the *only* way a handler reaches a channel; the sandboxed script can call it
solely through the metered external functions in
:mod:`smarter_dev.web.handler_runtime`.

Kept deliberately small: send a message, edit a bot-authored message, add a
reaction, rename a channel. Request plumbing lives in
:mod:`smarter_dev.web.discord_rest`, shared with ``AdminActor``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import quote

from smarter_dev.web.discord_rest import DiscordBotClient, DiscordRestError

logger = logging.getLogger(__name__)

_MESSAGE_MAX = 2000
# Discord's hard channel-name length limit; a longer rename is rejected by REST.
_CHANNEL_NAME_MAX = 100
# Discord message flag: suppress auto-generated link-preview embeds. Handler
# output is often a list of links (e.g. a news digest); without this each URL
# explodes into a large preview card, flooding the channel.
_SUPPRESS_EMBEDS = 1 << 2


def _allowed_mentions(ping_role_id: str | None = None) -> dict:
    """Mention rail applied to every content-posting emit.

    Discord's default parses EVERYTHING, so an un-suppressed send that echoes
    user content could ping ``@everyone``/``@here`` or any role. Parsing only
    ``users`` keeps a moderation handler able to ping the offending user by id
    while stripping mass pings. An explicit ``ping_role_id`` (admin escalation
    only) re-adds exactly that one role.
    """
    if ping_role_id is not None:
        return {"parse": ["users"], "roles": [str(ping_role_id)]}
    return {"parse": ["users"]}

# Merged active + recently-archived thread list is capped so a script can't
# fan a single read into an unbounded page of threads.
_THREAD_LIST_MAX = 50
# Discord channel ``type`` values that identify a thread (announcement,
# public, private). Anything else is a regular channel with no parent thread.
_THREAD_CHANNEL_TYPES = (10, 11, 12)
# Discord channel ``type`` for a standalone public thread created via
# POST /channels/{id}/threads (no starter message).
_PUBLIC_THREAD_TYPE = 11


class DiscordEmitError(DiscordRestError):
    """A Discord REST emit failed."""


def _thread_summary(thread: dict, tag_names_by_id: dict[str, str]) -> dict:
    """Flatten a raw Discord thread object into the scripting-surface shape."""
    metadata = thread.get("thread_metadata") or {}
    applied_tag_ids = thread.get("applied_tags") or []
    return {
        "thread_id": str(thread.get("id", "")),
        "name": thread.get("name", ""),
        "created_at": metadata.get("create_timestamp", ""),
        "archived": bool(metadata.get("archived", False)),
        "locked": bool(metadata.get("locked", False)),
        "owner_id": str(thread.get("owner_id", "")),
        "message_count": int(thread.get("message_count", 0)),
        "applied_tag_names": [
            tag_names_by_id[str(tag_id)]
            for tag_id in applied_tag_ids
            if str(tag_id) in tag_names_by_id
        ],
    }


@dataclass(kw_only=True)
class DiscordEmitter(DiscordBotClient):
    """Minimal bot-token REST emitter used by handler executions."""

    # The active-threads endpoint is guild-scoped, so the emitter carries the
    # fire's guild. Defaulted so existing callers that only send messages /
    # reactions keep constructing with ``bot_token`` alone; ``list_threads``
    # requires the runtime to pass the real guild id.
    guild_id: str = ""
    # Per-fire cache of user_id -> opened DM channel id, so repeated relay
    # replies to one user (send_dm) open the channel once. One emitter is built
    # per fire in admin_handlers_jobs, so this is naturally fire-scoped.
    _dm_channel_cache: dict[str, str] = field(default_factory=dict)

    user_agent: ClassVar[str] = "SmarterDev-Handlers/1.0"
    error_type: ClassVar[type[DiscordRestError]] = DiscordEmitError

    async def create_message(
        self, channel_id: str, content: str, ping_role_id: str | None = None
    ) -> str:
        """Post a message to a channel; return the new message id.

        Link-preview embeds are suppressed so a handler that posts URLs doesn't
        flood the channel with large preview cards. Mass mentions are suppressed
        by default (only user mentions parse); ``ping_role_id`` re-allows exactly
        one role, for admin mod-escalation sends.
        """
        payload = {
            "content": content[:_MESSAGE_MAX],
            "flags": _SUPPRESS_EMBEDS,
            "allowed_mentions": _allowed_mentions(ping_role_id),
        }
        response = await self._request(
            "POST", f"/channels/{channel_id}/messages", json=payload
        )
        return str(response.json().get("id", ""))

    async def send_dm(self, user_id: str, content: str) -> str | bool:
        """DM a user; return the new message id, or ``False`` on a closed door.

        Resolves (opening if needed) the user's DM channel via
        ``POST /users/@me/channels`` — cached per fire so repeated relay replies
        to one user pay a single channel-open — then posts with the same
        truncation, embed suppression, and mention rail as ``create_message``.

        Returns ``False`` on 403 (DMs closed / no mutual guild) or 404 (unknown
        user): these are *expected* outcomes the relay script branches to ❌ on,
        not infrastructure failures. Everything else raises (fail fast).
        """
        try:
            channel_id = await self._resolve_dm_channel(user_id)
            payload = {
                "content": content[:_MESSAGE_MAX],
                "flags": _SUPPRESS_EMBEDS,
                "allowed_mentions": _allowed_mentions(),
            }
            response = await self._request(
                "POST", f"/channels/{channel_id}/messages", json=payload
            )
        except DiscordEmitError as error:
            if error.status_code in (403, 404):
                return False
            raise
        return str(response.json().get("id", ""))

    async def _resolve_dm_channel(self, user_id: str) -> str:
        """The user's DM channel id, opening + caching it once per fire."""
        cached = self._dm_channel_cache.get(user_id)
        if cached is not None:
            return cached
        response = await self._request(
            "POST", "/users/@me/channels", json={"recipient_id": user_id}
        )
        channel_id = str(response.json().get("id", ""))
        self._dm_channel_cache[user_id] = channel_id
        return channel_id

    async def edit_message(
        self, channel_id: str, message_id: str, content: str
    ) -> str:
        """Edit a bot-authored message in place; return the message id.

        Discord REST only permits editing the bot's OWN messages, so
        bot-authored-only is enforced by the API (403 otherwise) — no ownership
        bookkeeping needed on our side. The same embed- and mention-suppression
        rails as ``create_message`` apply, so a maintained post can't start
        pinging when its content is re-rendered.
        """
        payload = {
            "content": content[:_MESSAGE_MAX],
            "flags": _SUPPRESS_EMBEDS,
            "allowed_mentions": _allowed_mentions(),
        }
        response = await self._request(
            "PATCH", f"/channels/{channel_id}/messages/{message_id}", json=payload
        )
        return str(response.json().get("id", ""))

    async def rename_channel(self, channel_id: str, name: str) -> bool:
        """Rename a channel; return True. Raises on any failure.

        ``PATCH /channels/{id}`` with ``{"name": name[:100]}`` (Discord's channel
        name limit). Discord hard-limits renames to 2 per 10 min per channel and
        missing MANAGE_CHANNELS surfaces as a REST error — both error the fire
        loudly rather than silently no-op.
        """
        await self._request(
            "PATCH", f"/channels/{channel_id}", json={"name": name[:_CHANNEL_NAME_MAX]}
        )
        return True

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        """React to a message. ``emoji`` is ``name:id`` for custom, else unicode."""
        cleaned = emoji.strip().lstrip("<").rstrip(">")
        encoded = quote(cleaned, safe="")
        await self._request(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        )

    async def list_threads(self, channel_id: str, limit: int = 50) -> list[dict]:
        """Active + recently-archived threads/posts of ``channel_id``.

        The guild active-threads list is filtered to this parent channel and
        merged (active first) with the channel's public archived threads under
        a hard 50-entry cap. Forum tag ids are resolved to names via the
        parent channel's ``available_tags`` only when a listed thread carries
        tags. A 404 (gone channel) is an informational empty read; any other
        failure raises.
        """
        try:
            active_response = await self._request(
                "GET", f"/guilds/{self.guild_id}/threads/active"
            )
            archived_response = await self._request(
                "GET",
                f"/channels/{channel_id}/threads/archived/public",
                params={"limit": limit},
            )
        except DiscordEmitError as error:
            if error.status_code == 404:
                return []
            raise
        active_threads = [
            thread
            for thread in active_response.json().get("threads", [])
            if str(thread.get("parent_id", "")) == channel_id
        ]
        archived_threads = archived_response.json().get("threads", [])
        merged = (active_threads + archived_threads)[:_THREAD_LIST_MAX]
        tag_names_by_id = await self._forum_tag_names(channel_id, merged)
        return [_thread_summary(thread, tag_names_by_id) for thread in merged]

    async def _forum_tag_names(
        self, channel_id: str, threads: list[dict]
    ) -> dict[str, str]:
        """Map available forum tag id -> name, or ``{}`` when no thread has tags."""
        if not any(thread.get("applied_tags") for thread in threads):
            return {}
        channel = await self._request("GET", f"/channels/{channel_id}")
        return {
            str(tag["id"]): tag.get("name", "")
            for tag in channel.json().get("available_tags", [])
        }

    async def create_thread(
        self, channel_id: str, name: str, message_id: str | None = None
    ) -> str:
        """Create a thread; return its id. Raises on any failure.

        With ``message_id`` the thread hangs off that message; without it a
        standalone public thread is created.
        """
        if message_id is not None:
            endpoint = f"/channels/{channel_id}/messages/{message_id}/threads"
            payload: dict = {"name": name}
        else:
            endpoint = f"/channels/{channel_id}/threads"
            payload = {"name": name, "type": _PUBLIC_THREAD_TYPE}
        response = await self._request("POST", endpoint, json=payload)
        return str(response.json().get("id", ""))

    async def create_post(
        self,
        channel_id: str,
        title: str,
        content: str,
        tag_names: list[str] | None = None,
    ) -> str:
        """Create a forum post; return its thread id. Raises on any failure.

        ``tag_names`` are resolved against the forum's ``available_tags``; an
        unknown name raises ``ValueError`` listing the valid names (fail fast).
        """
        payload: dict = {
            "name": title,
            "message": {"content": content, "allowed_mentions": _allowed_mentions()},
        }
        if tag_names:
            channel = await self._request("GET", f"/channels/{channel_id}")
            id_by_name = {
                tag.get("name", ""): str(tag["id"])
                for tag in channel.json().get("available_tags", [])
            }
            unknown = [name for name in tag_names if name not in id_by_name]
            if unknown:
                valid = ", ".join(sorted(id_by_name)) or "(none)"
                raise ValueError(
                    f"unknown forum tag(s): {', '.join(unknown)}. valid tags: {valid}"
                )
            payload["applied_tags"] = [id_by_name[name] for name in tag_names]
        response = await self._request(
            "POST", f"/channels/{channel_id}/threads", json=payload
        )
        return str(response.json().get("id", ""))

    async def get_guild_member_count(self) -> int:
        """Approximate total member count of this fire's guild.

        ``GET /guilds/{guild_id}?with_counts=true`` -> ``approximate_member_count``
        (Discord's lazily-updated figure, fine for a coarse ``1.2k`` display). No
        gateway intent or member cache needed. A REST error propagates and errors
        the fire — never a silent 0.
        """
        response = await self._request(
            "GET", f"/guilds/{self.guild_id}", params={"with_counts": "true"}
        )
        return int(response.json()["approximate_member_count"])

    async def get_channel_guild_id(self, channel_id: str) -> str | None:
        """Guild id that owns ``channel_id``, or ``None`` when gone/not a guild
        channel.

        Supports the admin ``list_threads`` guild-scope rail: a guild-wide admin
        handler (empty ``channel_ids``) may only read a channel that belongs to
        its own guild, so a foreign-guild channel id can't leak archived threads.
        A 404 (gone channel) returns ``None``; any other failure raises.
        """
        try:
            response = await self._request("GET", f"/channels/{channel_id}")
        except DiscordEmitError as error:
            if error.status_code == 404:
                return None
            raise
        guild_id = response.json().get("guild_id")
        return str(guild_id) if guild_id else None

    async def get_thread_parent_id(self, thread_id: str) -> str | None:
        """Parent channel id of ``thread_id`` when it is a thread, else ``None``.

        Supports the runtime's home-channel-thread send check. A 404 (gone
        channel) returns ``None``; a non-thread channel returns ``None``; any
        other failure raises.
        """
        try:
            response = await self._request("GET", f"/channels/{thread_id}")
        except DiscordEmitError as error:
            if error.status_code == 404:
                return None
            raise
        channel = response.json()
        if channel.get("type") not in _THREAD_CHANNEL_TYPES:
            return None
        parent_id = channel.get("parent_id")
        return str(parent_id) if parent_id else None
