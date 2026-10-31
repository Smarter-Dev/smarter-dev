"""Privileged operations for admin handlers, via Discord REST (worker tier).

These are what an admin-handler script reaches through its external functions:
role changes (staff roles refused), thread upkeep, deleting a bot's or a
webhook's message, the mod-audit reads (``get_member_info`` /
``search_guild_members``), and handing a member to the moderators for review.
Standard (member) handlers never get an ``AdminActor`` and so can't reach any
of this.

None of it punishes a member. Bans, kicks, timeouts, warnings and removing a
member's message are the moderation coordinator's, through the moderation
gateway (``smarter_dev.web.moderation_gateway``). A handler that has something
to tell the moderators about a member calls :meth:`AdminActor.report_for_review`:
a review-only incident in that same gateway, with no effect and no
``moderation_actions`` row, reported where the gateway reports every incident.
A message by a bot or a webhook has no member behind it, so deleting it is
channel upkeep (the Disboard tracker clears Disboard's notices and its own
reminders) and goes straight to Discord; a member's message is refused.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import ClassVar
from urllib.parse import quote
from uuid import UUID
from uuid import uuid4

from smarter_dev.shared.staff_protection import A_BOT
from smarter_dev.shared.staff_protection import BOT_ITSELF
from smarter_dev.shared.staff_protection import GuildFacts
from smarter_dev.shared.staff_protection import MemberFacts
from smarter_dev.shared.staff_protection import Overwrite
from smarter_dev.shared.staff_protection import member_refusal
from smarter_dev.shared.staff_protection import non_member_refusal
from smarter_dev.shared.staff_protection import role_refusal
from smarter_dev.shared.staff_protection import unknown
from smarter_dev.web.discord_rest import DiscordBotClient
from smarter_dev.web.discord_rest import DiscordRestError
from smarter_dev.web.moderation_gateway import ModerationGateway
from smarter_dev.web.moderation_gateway import Trigger
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.port import ModerationPort
from smarter_dev.web.moderation_gateway.rest_port import RestModerationPort

logger = logging.getLogger(__name__)

# Discord channel types 10/11/12: announcement, public, and private threads.
_THREAD_CHANNEL_TYPES = frozenset({10, 11, 12})

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


class ProtectedRoleError(AdminActionError):
    """A role change automation may never make: the role confers staff standing."""


@dataclass(frozen=True)
class HandlerFireFacts:
    """What one admin fire is about, for the incidents its actions join.

    ``message_id`` / ``author_id`` / ``content`` describe the message that
    fired it (empty for a member event or a timer), so a review the script
    asks for about that message's author joins the incident the moderation
    coordinator opened for the same message.
    """

    handler_id: str
    handler_name: str
    channel_id: str = ""
    message_id: str | None = None
    author_id: str | None = None
    content: str | None = None
    chain_depth: int = 0
    #: One per fire: what one fire does to one member is one incident even
    #: when no message ties the actions together (a member event, a timer).
    fire_key: str = field(default_factory=lambda: uuid4().hex)


#: What ``report_for_review`` records as the incident's reason.
REVIEW_REASON = "flagged for review"


@dataclass(kw_only=True)
class AdminActor(DiscordBotClient):
    """Performs one admin fire's privileged operations via Discord REST."""

    guild_id: str
    fire: HandlerFireFacts | None = None
    #: The guild's configured extra staff roles (its moderation policy's
    #: ``protected_role_ids``, none without a policy; the content filter,
    #: triage and coordinator treat the same roles as staff). None means they
    #: were not read, and every staff and role check refuses.
    staff_exempt_role_ids: tuple[str, ...] | None = None
    #: Tests hand in a fake Discord; production talks REST through this actor.
    moderation_port: ModerationPort | None = None
    _verified_thread_ids: set[str] = field(default_factory=set, init=False)
    # One fire's worth: an actor lives for a single handler fire.
    _own_user_id: str | None = field(default=None, init=False)
    _guild_facts: GuildFacts | None = field(default=None, init=False)
    # A refusal, once read, holds for the rest of the fire.
    _refusals: dict[str, str] = field(default_factory=dict, init=False)
    _port: ModerationPort | None = field(default=None, init=False)
    _gateway: ModerationGateway | None = field(default=None, init=False)
    #: Incidents this fire's review reports opened or joined, newest last; a
    #: message the script then sends to the incident report channel becomes
    #: part of that report.
    touched_incidents: list = field(default_factory=list, init=False)

    user_agent: ClassVar[str] = "SmarterDev-AdminHandlers/1.0"
    error_type: ClassVar[type[DiscordRestError]] = AdminActionError

    # -- the gateway ---------------------------------------------------------

    def port(self) -> ModerationPort:
        """Discord as the gateway sees it: REST through this actor, or a fake."""
        if self._port is None:
            self._port = self.moderation_port or RestModerationPort(
                self, chain_depth=self.fire.chain_depth if self.fire else 0
            )
        return self._port

    def gateway(self) -> ModerationGateway:
        if self._gateway is None:
            self._gateway = ModerationGateway(self.port())
        return self._gateway

    @property
    def actor_name(self) -> str:
        return f"handler:{self.fire.handler_name}" if self.fire else "handler"

    def _trigger(
        self, target_user_id: str, reason: str, *, report_channel_id: str | None
    ) -> Trigger:
        """The fire as a trigger: its message when the target posted it.

        One fire's reports about one member are one incident (the fire
        alias); a report about the author of the message that fired joins
        the incident the coordinator opened for that message.
        """
        fire = self.fire
        about_fire_message = (
            fire is not None
            and fire.author_id is not None
            and str(fire.author_id) == str(target_user_id)
        )
        return Trigger(
            guild_id=str(self.guild_id),
            actor=self.actor_name,
            reason=reason,
            source=gw.SOURCE_HANDLER,
            channel_id=fire.channel_id if about_fire_message else None,
            message_id=fire.message_id if about_fire_message else None,
            author_id=str(target_user_id),
            content=fire.content if about_fire_message else None,
            aliases=(f"handler-fire:{fire.fire_key}:{target_user_id}",)
            if fire is not None
            else (),
            report_channel_id=report_channel_id,
            moderator_username=self.actor_name,
        )

    # -- review --------------------------------------------------------------

    async def report_for_review(
        self, user_id: str, summary: str, *, report_channel_id: str | None
    ) -> str:
        """Hand ``user_id`` to the moderators: a review-only incident.

        Opens (or joins) the incident through the gateway with no proposal,
        so nothing is applied and no ``moderation_actions`` row is written;
        ``summary`` is added as the incident's evidence entry. The report is posted in
        ``report_channel_id`` — or, with None (the guild reports nowhere),
        the incident is only recorded. Returns the report's message id, or
        ``""`` when there is none.
        """
        uid = str(user_id)
        gateway = self.gateway()
        incident_id: UUID = await gateway.ensure_incident(
            self._trigger(uid, REVIEW_REASON, report_channel_id=report_channel_id),
            subject=uid,
        )
        if incident_id not in self.touched_incidents:
            self.touched_incidents.append(incident_id)
        await gateway.add_note(
            incident_id,
            source=self.actor_name,
            text=summary,
            # A script's words about a member, which may quote them: kept
            # 90 days like a message the bot acted on, never permanently.
            kind=gw.ENTRY_EVIDENCE,
            report_channel_id=report_channel_id,
            target_user_id=uid,
        )
        return await gateway.report_message_of(incident_id) or ""

    # -- upkeep --------------------------------------------------------------

    async def delete_bot_message(self, channel_id: str, message_id: str) -> str:
        """Delete a bot's or a webhook's message; never a member's.

        The message is read first. Gone: ``"message <id> already deleted"``.
        Posted by a member: ``"not deleted: ..."`` and nothing is deleted
        (the moderation coordinator removes members' messages). Posted by a
        bot (this one included) or a webhook: deleted straight through
        Discord, outside the moderation gateway — upkeep, not a sanction —
        and ``"deleted message <id>"``. A refused or unknown delete raises.
        """
        port = self.port()
        mid = str(message_id)
        try:
            author = await port.message_author(str(channel_id), mid)
        except gw.EffectRejected as error:
            raise AdminActionError(f"reading message {mid}: {error.detail}") from error
        if author is None:
            return f"message {mid} already deleted"
        _author_id, _author_name, by_bot_or_webhook = author
        if not by_bot_or_webhook:
            return (
                f"not deleted: message {mid} was posted by a member; handlers "
                "delete only bots' and webhooks' messages"
            )
        deletion = await port.delete_messages(str(channel_id), [mid])
        if deletion.deleted:
            return f"deleted message {mid}"
        if deletion.missing:
            return f"message {mid} already deleted"
        raise AdminActionError(
            f"delete of message {mid} "
            + ("refused" if deletion.refused else "outcome unknown")
        )

    # -- roles ---------------------------------------------------------------

    async def add_role(
        self, user_id: str, role_id: str, reason: str | None = None
    ) -> bool:
        """Grant a role to a member. A gone member (404) -> False (no-op).

        A role that confers staff standing is refused (:meth:`role_refusal`).
        """
        await self._refuse_staff_role(role_id)
        return await self._mutate_role("PUT", user_id, role_id, reason)

    async def remove_role(
        self, user_id: str, role_id: str, reason: str | None = None
    ) -> bool:
        """Revoke a role from a member. A gone member (404) -> False (no-op).

        A role that confers staff standing is refused (:meth:`role_refusal`),
        so a script can never take a moderator's standing away.
        """
        await self._refuse_staff_role(role_id)
        return await self._mutate_role("DELETE", user_id, role_id, reason)

    async def role_refusal(self, role_id: str) -> str | None:
        """Why automation may not grant or remove ``role_id``; never raises.

        The shared rule (``staff_protection.role_refusal``) over the guild's
        roles and channel overwrites, read once per fire. A failed read
        refuses.
        """
        if self.staff_exempt_role_ids is None:
            return unknown("the guild's staff roles were not read")
        try:
            return role_refusal(
                str(role_id),
                await self._protection_guild_facts(),
                extra_role_ids=self.staff_exempt_role_ids,
            )
        except Exception as error:  # noqa: BLE001 — unknown refuses
            logger.warning(
                "Role protection check for role %s in guild %s failed: %s",
                role_id,
                self.guild_id,
                type(error).__name__,
            )
            return unknown(type(error).__name__)

    async def _refuse_staff_role(self, role_id: str) -> None:
        refusal = await self.role_refusal(role_id)
        if refusal is not None:
            raise ProtectedRoleError(
                f"role {role_id} {refusal}: automation never grants or removes it"
            )

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

    async def staff_refusal(self, user_id: str, *, is_bot: bool | None = None) -> str | None:
        """Why automation may not act on this user, or None when it may.

        The shared rule, fed over REST: the member, the guild (owner and roles)
        and every channel's overwrites. Never raises; a failed lookup is an
        "could not be checked" refusal. A refusal is kept for the fire.
        """
        uid = str(user_id)
        if uid not in self._refusals:
            refusal = await self._check_staff(uid, is_bot=is_bot)
            if refusal is None:
                return None
            self._refusals[uid] = refusal
        return self._refusals[uid]

    async def _check_staff(self, uid: str, *, is_bot: bool | None) -> str | None:
        try:
            bot_user_id = await self._bot_user_id()
            if uid == bot_user_id:
                return BOT_ITSELF
            if is_bot:
                return A_BOT
            try:
                response = await self._request(
                    "GET", f"/guilds/{self.guild_id}/members/{uid}"
                )
            except AdminActionError as error:
                if error.status_code != 404:
                    raise
                if is_bot is None:
                    user = (await self._request("GET", f"/users/{uid}")).json()
                    is_bot = bool(user.get("bot", False))
                return non_member_refusal(uid, is_bot=is_bot, bot_user_id=bot_user_id)
            member = response.json()
            facts = MemberFacts(
                user_id=uid,
                is_bot=bool((member.get("user") or {}).get("bot", False)),
                role_ids=tuple(str(role_id) for role_id in member.get("roles", [])),
            )
            if self.staff_exempt_role_ids is None:
                return unknown("the guild's staff roles were not read")
            return member_refusal(
                facts,
                await self._protection_guild_facts(),
                bot_user_id=bot_user_id,
                extra_role_ids=self.staff_exempt_role_ids,
            )
        except Exception as error:  # noqa: BLE001 — unknown protects
            logger.warning(
                "Staff protection check for user %s in guild %s failed: %s",
                uid,
                self.guild_id,
                type(error).__name__,
            )
            return unknown(type(error).__name__)

    async def _bot_user_id(self) -> str:
        if self._own_user_id is None:
            response = await self._request("GET", "/users/@me")
            self._own_user_id = str(response.json()["id"])
        return self._own_user_id

    async def _protection_guild_facts(self) -> GuildFacts:
        if self._guild_facts is None:
            guild = (await self._request("GET", f"/guilds/{self.guild_id}")).json()
            channels = (
                await self._request("GET", f"/guilds/{self.guild_id}/channels")
            ).json()
            self._guild_facts = GuildFacts(
                guild_id=str(self.guild_id),
                owner_id=str(guild["owner_id"]) if guild.get("owner_id") else None,
                role_permissions={
                    str(role["id"]): int(role["permissions"])
                    for role in guild.get("roles", [])
                },
                channel_overwrites=tuple(
                    {
                        str(entry["id"]): Overwrite(
                            allow=int(entry.get("allow", 0)),
                            deny=int(entry.get("deny", 0)),
                        )
                        for entry in channel.get("permission_overwrites") or []
                    }
                    for channel in channels
                ),
            )
        return self._guild_facts

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

    async def reopen_thread(self, thread_id: str) -> bool:
        """Unarchive a thread. A gone thread (404) -> False."""
        return await self._patch_thread(thread_id, {"archived": False})

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
