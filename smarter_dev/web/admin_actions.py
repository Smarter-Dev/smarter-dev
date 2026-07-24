"""Moderation actions for admin handlers, via Discord REST (worker tier).

These are the privileged operations an admin-handler script can call as external
functions: ban, kick, timeout, delete-message, delete-webhook, plus the mod-audit
reads (``get_member_info`` / ``search_guild_members``). Each is a single bot-token
REST call. Standard (member) handlers never get an ``AdminActor`` and so can't
reach any of this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import ClassVar
from urllib.parse import quote

from smarter_dev.web.discord_rest import DiscordBotClient
from smarter_dev.web.discord_rest import DiscordRestError

# Discord channel types 10/11/12: announcement, public, and private threads.
_THREAD_CHANNEL_TYPES = frozenset({10, 11, 12})

# The ONLY URL shape delete_webhook will act on. Anchored end-to-end so the
# sandbox can never turn a webhook delete into an arbitrary-host DELETE: the host
# must be a real Discord webhook host (optionally a canary/ptb subdomain, with the
# legacy discordapp.com alias), the path exactly /api/webhooks/<id>/<token>, and
# nothing after the token. The id is all-digits and the token a url-safe word run.
_WEBHOOK_URL_RE = re.compile(
    r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/"
    r"(?P<id>\d+)/(?P<token>[\w-]+)$"
)

# A Discord snowflake encodes its creation time in the high bits (ms since the
# Discord epoch). Used to surface account age without a REST round-trip.
_DISCORD_EPOCH_MS = 1420070400000

# Discord's member-search endpoint returns at most this many rows and no total, so
# search_guild_members always over-fetches the full window and slices host-side —
# the overflow count is exact below the window, a floor once it fills.
_MEMBER_SEARCH_WINDOW = 100


def _snowflake_created_at(snowflake_id: str) -> str | None:
    """ISO-8601 UTC creation time encoded in a Discord snowflake, None if unparsable."""
    try:
        ms = (int(snowflake_id) >> 22) + _DISCORD_EPOCH_MS
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _top_role_name(role_ids: list[str], roles_by_id: dict[str, dict]) -> str:
    """Highest-positioned of a member's roles, '@everyone' when they hold none.

    Discord's role positions are guild-wide integers (higher = more senior); the
    legacy !lookup showed each member's most senior role. A role id the guild no
    longer has is skipped (position treated as -1 so it never wins).
    """
    best_name = "@everyone"
    best_position = -1
    for role_id in role_ids:
        role = roles_by_id.get(str(role_id))
        if role is None:
            continue
        position = int(role.get("position", 0))
        if position > best_position:
            best_position = position
            best_name = role.get("name", best_name)
    return best_name


class AdminActionError(DiscordRestError):
    """A moderation action's REST call failed."""


@dataclass(kw_only=True)
class AdminActor(DiscordBotClient):
    """Performs moderation actions for one guild via Discord REST."""

    guild_id: str
    _verified_thread_ids: set[str] = field(default_factory=set, init=False)

    user_agent: ClassVar[str] = "SmarterDev-AdminHandlers/1.0"
    error_type: ClassVar[type[DiscordRestError]] = AdminActionError

    async def ban_user(
        self,
        user_id: str,
        reason: str | None = None,
        delete_message_seconds: int = 0,
    ) -> str:
        """Ban a user, treating an already-absent target as a successful no-op.

        A member can leave between a handler fire and this REST call. Discord
        reports that race as 404, but the handler's immediate goal (the member
        is no longer in the guild) is already true, so it must not abort the
        rest of the script. Other failures still raise.
        """
        # Discord expects the audit-log reason URL-encoded; encoding also keeps
        # non-latin-1 reasons (em dashes, emoji) out of raw header bytes.
        headers = (
            {"X-Audit-Log-Reason": quote(reason[:400])} if reason else None
        )
        # delete_message_seconds purges the banned member's recent messages (the
        # onboarding auto-ban wants an hour swept); 0 = keep history.
        try:
            await self._request(
                "PUT",
                f"/guilds/{self.guild_id}/bans/{user_id}",
                headers=headers,
                json={"delete_message_seconds": int(delete_message_seconds)},
            )
        except AdminActionError as error:
            if error.status_code == 404:
                return f"ban target {user_id} already absent"
            raise
        return f"banned {user_id}"

    async def add_role(
        self, user_id: str, role_id: str, reason: str | None = None
    ) -> bool:
        """Grant a role to a member. A gone member (404) -> False (no-op)."""
        return await self._mutate_role("PUT", user_id, role_id, reason)

    async def remove_role(
        self, user_id: str, role_id: str, reason: str | None = None
    ) -> bool:
        """Revoke a role from a member. A gone member (404) -> False (no-op)."""
        return await self._mutate_role("DELETE", user_id, role_id, reason)

    async def _mutate_role(
        self, method: str, user_id: str, role_id: str, reason: str | None
    ) -> bool:
        """PUT/DELETE a member role; 404 (member gone) -> False, else raise.

        The 404 no-op is the required contract: a 2-day promotion or a sus
        expiry must be a silent no-op when the member already left. Any other
        status (403 role-hierarchy/perms, 429, 5xx) raises AdminActionError.
        """
        headers = (
            {"X-Audit-Log-Reason": quote(reason[:400])} if reason else None
        )
        try:
            await self._request(
                method,
                f"/guilds/{self.guild_id}/members/{user_id}/roles/{role_id}",
                headers=headers,
            )
        except AdminActionError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    async def kick_user(self, user_id: str) -> str:
        """Kick a member; a member who already left is a successful no-op."""
        try:
            await self._request(
                "DELETE", f"/guilds/{self.guild_id}/members/{user_id}"
            )
        except AdminActionError as error:
            if error.status_code == 404:
                return f"kick target {user_id} already absent"
            raise
        return f"kicked {user_id}"

    async def timeout_user(self, user_id: str, duration_seconds: int = 600) -> str:
        """Timeout a member; a member who already left is a successful no-op."""
        until = (
            datetime.now(timezone.utc) + timedelta(seconds=int(duration_seconds))
        ).isoformat()
        try:
            await self._request(
                "PATCH",
                f"/guilds/{self.guild_id}/members/{user_id}",
                json={"communication_disabled_until": until},
            )
        except AdminActionError as error:
            if error.status_code == 404:
                return f"timeout target {user_id} already absent"
            raise
        return f"timed out {user_id} for {int(duration_seconds)}s"

    async def delete_message(self, channel_id: str, message_id: str) -> str:
        """Delete a message; an already-deleted target is a successful no-op."""
        try:
            await self._request(
                "DELETE", f"/channels/{channel_id}/messages/{message_id}"
            )
        except AdminActionError as error:
            if error.status_code == 404:
                return f"message {message_id} already deleted"
            raise
        return f"deleted message {message_id}"

    async def delete_webhook(self, webhook_url: str) -> bool:
        """DELETE a leaked ``discord.com/api/webhooks/<id>/<token>`` URL.

        The URL is validated host-side against :data:`_WEBHOOK_URL_RE` FIRST: an
        arbitrary host, a path-traversal attempt, or a missing token raises
        :class:`AdminActionError` and issues NO request, so a laundered constant
        can never become an arbitrary-URL DELETE. A valid URL is deleted via its
        own token (no bot auth needed, but we keep the bot-token client for its
        rate-limit handling). Returns ``False`` on 404 (already dead) and ``True``
        on success.
        """
        match = _WEBHOOK_URL_RE.match(str(webhook_url))
        if match is None:
            raise AdminActionError(
                f"delete_webhook target is not a Discord webhook URL: {webhook_url!r}"
            )
        endpoint = f"/webhooks/{match.group('id')}/{match.group('token')}"
        try:
            await self._request("DELETE", endpoint)
        except AdminActionError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    async def get_member_info(self, user_id: str) -> dict:
        """Profile a member; falls back to the bare user for departed members.

        ``GET /guilds/{gid}/members/{uid}`` maps to the full profile
        (``in_guild=True``, roles resolved to names via one ``GET /guilds/{gid}/
        roles``). A 404 means the user is not (or no longer) a member: fall back to
        ``GET /users/{uid}`` and return ``in_guild=False`` with empty guild fields,
        matching the legacy Snowflake departed-user lookup. Any other status raises.
        """
        try:
            response = await self._request(
                "GET", f"/guilds/{self.guild_id}/members/{user_id}"
            )
        except AdminActionError as error:
            if error.status_code == 404:
                return await self._departed_user_info(user_id)
            raise
        member = response.json()
        user = member.get("user") or {}
        role_ids = [str(role_id) for role_id in member.get("roles", [])]
        roles_by_id = await self._roles_by_id()
        return {
            "user_id": str(user.get("id", user_id)),
            "username": user.get("username"),
            "nickname": member.get("nick"),
            "joined_at": member.get("joined_at"),
            "account_created_at": _snowflake_created_at(user.get("id", user_id)),
            "is_pending": bool(member.get("pending", False)),
            "role_ids": role_ids,
            "role_names": [
                roles_by_id[rid]["name"] for rid in role_ids if rid in roles_by_id
            ],
            "in_guild": True,
        }

    async def _departed_user_info(self, user_id: str) -> dict:
        """The ``in_guild=False`` profile of a user who is not a guild member."""
        response = await self._request("GET", f"/users/{user_id}")
        user = response.json()
        return {
            "user_id": str(user.get("id", user_id)),
            "username": user.get("username"),
            "nickname": None,
            "joined_at": None,
            "account_created_at": _snowflake_created_at(user.get("id", user_id)),
            "is_pending": False,
            "role_ids": [],
            "role_names": [],
            "in_guild": False,
        }

    async def search_guild_members(self, query: str, limit: int = 10) -> dict:
        """Prefix-search guild members by name/nick; resolve each row's top role.

        Over-fetches Discord's window (``limit=100`` on ``GET /guilds/{gid}/
        members/search``) and slices to ``limit`` host-side, so ``overflow_count``
        (matches seen beyond ``limit``) is exact while the window isn't full and a
        FLOOR once it fills — Discord returns no total, so the legacy exact "N more
        matched" is unobtainable (render as "N+ more" at the floor). ``top_role_name``
        is resolved from one ``GET /guilds/{gid}/roles``: the highest-positioned of
        each member's roles, '@everyone' when they hold none. Discord matches on a
        username/nick PREFIX (the legacy substring match is a documented divergence).
        """
        response = await self._request(
            "GET",
            f"/guilds/{self.guild_id}/members/search",
            params={"query": str(query), "limit": _MEMBER_SEARCH_WINDOW},
        )
        matched = response.json()
        overflow_count = max(0, len(matched) - int(limit))
        window = matched[: int(limit)]
        roles_by_id = await self._roles_by_id() if window else {}
        members = []
        for member in window:
            user = member.get("user") or {}
            role_ids = [str(role_id) for role_id in member.get("roles", [])]
            members.append(
                {
                    "user_id": str(user.get("id")),
                    "username": user.get("username"),
                    "nickname": member.get("nick"),
                    "joined_at": member.get("joined_at"),
                    "top_role_name": _top_role_name(role_ids, roles_by_id),
                }
            )
        return {"members": members, "overflow_count": overflow_count}

    async def _roles_by_id(self) -> dict[str, dict]:
        """Map role id -> role payload for this guild (one REST fetch per call)."""
        response = await self._request("GET", f"/guilds/{self.guild_id}/roles")
        return {str(role["id"]): role for role in response.json()}

    async def close_thread(self, thread_id: str) -> bool:
        """Archive a thread. A gone thread (404) is a silent no-op -> False."""
        return await self._patch_thread(thread_id, {"archived": True})

    async def lock_thread(self, thread_id: str) -> bool:
        """Lock and archive a thread. A gone thread (404) -> False."""
        return await self._patch_thread(
            thread_id, {"locked": True, "archived": True}
        )

    async def reopen_thread(self, thread_id: str) -> bool:
        """Unarchive a thread. A gone thread (404) -> False."""
        return await self._patch_thread(thread_id, {"archived": False})

    async def delete_thread(self, thread_id: str) -> bool:
        """Delete a thread. A gone thread (404) is a silent no-op -> False."""
        if not await self._verify_thread_target(thread_id):
            return False
        try:
            await self._request("DELETE", f"/channels/{thread_id}")
        except AdminActionError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    async def _patch_thread(self, thread_id: str, payload: dict) -> bool:
        """PATCH a thread channel; 404 (gone) -> False, other failures raise."""
        if not await self._verify_thread_target(thread_id):
            return False
        try:
            await self._request("PATCH", f"/channels/{thread_id}", json=payload)
        except AdminActionError as error:
            if error.status_code == 404:
                return False
            raise
        return True

    async def _verify_thread_target(self, thread_id: str) -> bool:
        """Confirm the target is a thread in this actor's guild before mutating.

        Scripts supply thread ids; without this rail a laundered constant could
        aim PATCH/DELETE /channels/{id} at a regular channel (deleting it
        wholesale) or at another guild the bot inhabits. A gone target (404)
        -> False, matching the mutations' silent no-op contract; a non-thread
        or foreign-guild target raises. Verified ids are cached for the fire so
        sweeps pay one fetch per thread.
        """
        if thread_id in self._verified_thread_ids:
            return True
        try:
            response = await self._request("GET", f"/channels/{thread_id}")
        except AdminActionError as error:
            if error.status_code == 404:
                return False
            raise
        channel = response.json()
        if channel.get("type") not in _THREAD_CHANNEL_TYPES:
            raise AdminActionError(
                f"thread op target {thread_id} is not a thread "
                f"(channel type {channel.get('type')})"
            )
        if str(channel.get("guild_id")) != self.guild_id:
            raise AdminActionError(
                f"thread op target {thread_id} is outside guild {self.guild_id}"
            )
        self._verified_thread_ids.add(thread_id)
        return True
