"""Moderation actions for admin handlers, via Discord REST (worker tier).

These are the privileged operations an admin-handler script can call as external
functions: ban, kick, timeout, delete-message, delete-webhook, plus the mod-audit
reads (``get_member_info`` / ``search_guild_members``). Standard (member)
handlers never get an ``AdminActor`` and so can't reach any of this.

Ban, kick, timeout and a member's message delete go through the moderation
gateway (``smarter_dev.web.moderation_gateway``), the one path every
automated actor shares: it re-asks the shared staff-protection rule
(:meth:`AdminActor.staff_refusal`), joins the action to its incident with
whatever the bot's detectors already did, never applies a weaker or duplicate
sanction, never shortens a timeout, and writes one ``moderation_actions`` row
per confirmed effect. A protected target, or an action the incident already
covers, is a no-op that returns a "not ..." result instead of raising, so the
script carries on to its report. A message by a bot or a webhook has no member
behind it to punish: deleting it is channel upkeep (the Disboard tracker clears
Disboard's notices and its own reminders), goes straight to Discord and is not
refused.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from typing import ClassVar
from urllib.parse import quote
from uuid import uuid4

import httpx

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
from smarter_dev.web.moderation_gateway import Outcome
from smarter_dev.web.moderation_gateway import Proposal
from smarter_dev.web.moderation_gateway import Reservation
from smarter_dev.web.moderation_gateway import Trigger
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.port import ModerationPort
from smarter_dev.web.moderation_gateway.rest_port import RestModerationPort
from smarter_dev.web.webhook_urls import parse_discord_webhook_url

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


def _timeout_expiry(response) -> datetime | None:
    """``communication_disabled_until`` from a member response, None if unset."""
    try:
        raw = response.json().get("communication_disabled_until")
        until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return until if until.tzinfo else until.replace(tzinfo=timezone.utc)


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
    fired it (empty for a member event or a timer), so a ban the script
    issues about that message joins the incident the bot's detectors opened
    for the same message.
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


def _refused_reason(outcome: Outcome) -> str:
    return outcome.detail.removeprefix("protected: ")


@dataclass(kw_only=True)
class AdminActor(DiscordBotClient):
    """Performs moderation actions for one guild via Discord REST."""

    guild_id: str
    fire: HandlerFireFacts | None = None
    #: The guild's configured extra staff roles (the filter config's
    #: ``staff_exempt_role_ids``, which the filters, spam engine, spam ring,
    #: triage and coordinator all treat as staff). None means they were not
    #: read, and every staff and role check refuses.
    staff_exempt_role_ids: tuple[str, ...] | None = None
    #: Tests hand in a fake Discord; production talks REST through this actor.
    moderation_port: ModerationPort | None = None
    _verified_thread_ids: set[str] = field(default_factory=set, init=False)
    # One fire's worth: an actor lives for a single handler fire.
    _own_user_id: str | None = field(default=None, init=False)
    _guild_facts: GuildFacts | None = field(default=None, init=False)
    # Refusals stick for the fire, so stripping a staff role and then acting
    # on the member in the same fire still refuses.
    _refusals: dict[str, str] = field(default_factory=dict, init=False)
    _gateway: ModerationGateway | None = field(default=None, init=False)
    #: Incidents this fire's actions joined, newest last; a report the script
    #: sends to an incident's report channel becomes part of that report.
    touched_incidents: list = field(default_factory=list, init=False)

    user_agent: ClassVar[str] = "SmarterDev-AdminHandlers/1.0"
    error_type: ClassVar[type[DiscordRestError]] = AdminActionError

    # -- the gateway ---------------------------------------------------------

    def gateway(self) -> ModerationGateway:
        if self._gateway is None:
            port = self.moderation_port or RestModerationPort(
                self, chain_depth=self.fire.chain_depth if self.fire else 0
            )
            self._gateway = ModerationGateway(port)
        return self._gateway

    @property
    def actor_name(self) -> str:
        return f"handler:{self.fire.handler_name}" if self.fire else "handler"

    def _trigger(
        self,
        target_user_id: str,
        reason: str | None,
        *,
        message_id: str | None = None,
        channel_id: str | None = None,
    ) -> Trigger:
        """The fire as a trigger: its message when the target posted it."""
        fire = self.fire
        about_fire_message = (
            fire is not None
            and fire.author_id is not None
            and str(fire.author_id) == str(target_user_id)
        )
        return Trigger(
            guild_id=str(self.guild_id),
            actor=self.actor_name,
            reason=reason or "",
            source=gw.SOURCE_HANDLER,
            channel_id=channel_id or (fire.channel_id if about_fire_message else None),
            message_id=fire.message_id if about_fire_message else message_id,
            author_id=str(target_user_id),
            content=fire.content if about_fire_message else None,
            related_message_ids=(message_id,)
            if about_fire_message and message_id and message_id != fire.message_id
            else (),
            aliases=(f"handler-fire:{fire.fire_key}:{target_user_id}",)
            if fire is not None
            else (),
            moderator_username=self.actor_name,
        )

    async def _act(self, trigger: Trigger, proposal: Proposal) -> Outcome:
        result = await self.gateway().act(trigger, [proposal])
        if result.incident_id is not None and result.incident_id not in self.touched_incidents:
            self.touched_incidents.append(result.incident_id)
        return result.outcomes[0]

    def _raise_for(self, outcome: Outcome, what: str) -> None:
        """Failures still raise, as the REST calls did, so the fire records them."""
        if outcome.state == gw.UNKNOWN:
            raise AdminActionError(f"{what}: outcome unknown ({outcome.detail}); not retried")
        if outcome.state == gw.FAILED:
            raise AdminActionError(f"{what} failed: {outcome.detail}")

    @contextlib.asynccontextmanager
    async def reserve_warn(
        self, user_id: str, reason: str, channel_id: str
    ) -> AsyncIterator[Reservation]:
        """The gateway's go-ahead for a warn whose notice the runtime posts.

        The target's name for the permanent row is resolved here from
        Discord, never taken from the script.
        """
        uid = str(user_id)
        username = await self._resolve_username(uid)
        trigger = self._trigger(uid, reason, channel_id=channel_id)
        async with self.gateway().reserve(
            trigger,
            Proposal(
                kind=gw.WARN,
                target_user_id=uid,
                target_username=username,
                reason=reason,
                channel_id=channel_id or None,
            ),
        ) as slot:
            if slot.incident_id is not None and slot.incident_id not in self.touched_incidents:
                self.touched_incidents.append(slot.incident_id)
            yield slot

    async def _resolve_username(self, user_id: str) -> str:
        """The member's name for a permanent audit row; the id when unreadable."""
        try:
            info = await self.get_member_info(user_id)
        except (AdminActionError, httpx.HTTPError):
            # A rejection or transport failure degrades to the raw id rather
            # than failing a warn; a programming error still surfaces.
            logger.debug("warn could not resolve username for %s", user_id, exc_info=True)
            return user_id
        return info.get("username") or user_id

    # -- sanctions -----------------------------------------------------------

    async def ban_user(
        self,
        user_id: str,
        reason: str | None = None,
        delete_message_seconds: int = 0,
    ) -> str:
        """Ban a user through the gateway.

        A ban of someone the incident already banned, or who is already
        banned, is a no-op ("not banned: ..."). Discord answering 404 means
        the user does not exist; like a member who left before the call, that
        must not abort the rest of the script. Other failures raise.
        """
        uid = str(user_id)
        outcome = await self._act(
            self._trigger(uid, reason),
            Proposal(
                kind=gw.BAN,
                target_user_id=uid,
                reason=reason,
                delete_message_seconds=int(delete_message_seconds),
            ),
        )
        if outcome.state == gw.CONFIRMED:
            return f"banned {uid}"
        if outcome.state == gw.SKIPPED:
            if outcome.detail.startswith("protected: "):
                return f"not banned: {uid} is protected ({_refused_reason(outcome)})"
            return f"not banned: {uid} {outcome.detail}"
        if outcome.state == gw.FAILED and outcome.detail == "the user does not exist":
            return f"ban target {uid} already absent"
        self._raise_for(outcome, f"ban of {uid}")
        return f"not banned: {uid} {outcome.detail}"

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
        so a script cannot strip a moderator's role in one fire and punish
        them in a later one. Any other role may be removed from staff, but
        their standing is checked first so it holds for the rest of the fire
        (see ``_refusals``).
        """
        await self._refuse_staff_role(role_id)
        await self.staff_refusal(user_id)
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

    async def kick_user(self, user_id: str) -> str:
        """Kick a member; a member who already left is a successful no-op."""
        uid = str(user_id)
        outcome = await self._act(
            self._trigger(uid, None), Proposal(kind=gw.KICK, target_user_id=uid)
        )
        if outcome.state == gw.CONFIRMED:
            return f"kicked {uid}"
        if outcome.state == gw.SKIPPED and outcome.detail == "not a member":
            return f"kick target {uid} already absent"
        if outcome.state == gw.FAILED and outcome.detail == "not a member":
            return f"kick target {uid} already absent"
        if outcome.state == gw.SKIPPED:
            if outcome.detail.startswith("protected: "):
                return f"not kicked: {uid} is protected ({_refused_reason(outcome)})"
            return f"not kicked: {uid} {outcome.detail}"
        self._raise_for(outcome, f"kick of {uid}")
        return f"not kicked: {uid} {outcome.detail}"

    def _claim(self, name: str) -> str:
        handler_id = self.fire.handler_id if self.fire else "handler"
        return f"{handler_id}:{name}"

    async def hold_timeout(
        self, user_id: str, claim: str, duration_seconds: int, *, reason: str | None = None
    ) -> Outcome:
        """Time a member out under ``claim`` (a hold's key), through the gateway.

        Never shortens a timeout already running longer (the claim is still
        recorded, so it keeps the member timed out if that one is lifted), and
        leaves a timeout somebody else placed alone. Raises when Discord
        refused or the outcome is unknown.
        """
        uid = str(user_id)
        outcome = await self._act(
            self._trigger(uid, reason or "held while under review"),
            Proposal(
                kind=gw.TIMEOUT,
                target_user_id=uid,
                duration_seconds=int(duration_seconds),
                claim=self._claim(claim),
                reason=reason,
                provisional=claim.startswith("hold:"),
            ),
        )
        self._raise_for(outcome, f"timeout of {uid}")
        return outcome

    async def release_timeout(self, user_id: str, claim: str | None) -> Outcome:
        """End ``claim`` (or every claim of the automation when None).

        The member's timeout falls back to the longest claim left, or is
        lifted when none is; a timeout somebody else placed is never touched.
        """
        uid = str(user_id)
        outcome = await self._act(
            self._trigger(uid, "review cleared" if claim else "timeout removed"),
            Proposal(
                kind=gw.UNTIMEOUT,
                target_user_id=uid,
                claim=self._claim(claim) if claim else None,
            ),
        )
        self._raise_for(outcome, f"untimeout of {uid}")
        return outcome

    async def timeout_user(self, user_id: str, duration_seconds: int = 600) -> str:
        """Timeout a member; a member who already left is a successful no-op."""
        uid = str(user_id)
        seconds = int(duration_seconds)
        outcome = await self._act(
            self._trigger(uid, None),
            Proposal(
                kind=gw.TIMEOUT,
                target_user_id=uid,
                duration_seconds=seconds,
                claim=self._claim("timeout"),
            ),
        )
        if outcome.state in (gw.CONFIRMED, gw.COVERED):
            return f"timed out {uid} for {seconds}s"
        if outcome.state == gw.SKIPPED and outcome.detail == "not a member":
            return f"timeout target {uid} already absent"
        if outcome.state == gw.SKIPPED:
            if outcome.detail.startswith("protected: "):
                return f"not timed out: {uid} is protected ({_refused_reason(outcome)})"
            return f"not timed out: {uid} {outcome.detail}"
        if outcome.state == gw.FAILED and outcome.detail == "not a member":
            return f"timeout target {uid} already absent"
        self._raise_for(outcome, f"timeout of {uid}")
        return f"not timed out: {uid} {outcome.detail}"

    async def timeout_until(self, user_id: str) -> datetime | None:
        """When the member's timeout ends; None when free or not a member."""
        try:
            response = await self._request(
                "GET", f"/guilds/{self.guild_id}/members/{user_id}"
            )
        except AdminActionError as error:
            if error.status_code == 404:
                return None
            raise
        until = _timeout_expiry(response)
        if until is None or until <= datetime.now(timezone.utc):
            return None
        return until

    async def remove_timeout(self, user_id: str) -> str:
        """Lift the automation's timeout; never a moderator's, never a stranger's."""
        uid = str(user_id)
        outcome = await self.release_timeout(uid, None)
        if outcome.state == gw.CONFIRMED:
            return f"removed timeout for {uid}"
        if outcome.detail == "not a member":
            return f"untimeout target {uid} already absent"
        return f"timeout for {uid} not removed: {outcome.detail}"

    async def delete_message(self, channel_id: str, message_id: str) -> str:
        """Delete a message; an already-deleted target is a successful no-op.

        A member's message goes through the gateway: deleted only when its
        author is not protected, and at most once however many detectors ask.
        A bot's or a webhook's is upkeep and always deleted.
        """
        port = self.gateway().port
        try:
            author = await port.message_author(str(channel_id), str(message_id))
        except gw.EffectRejected as error:
            raise AdminActionError(f"reading message {message_id}: {error.detail}") from error
        if author is None:
            return f"message {message_id} already deleted"
        author_id, author_name, upkeep = author
        if upkeep:
            deletion = await port.delete_messages(str(channel_id), [str(message_id)])
            if deletion.deleted:
                return f"deleted message {message_id}"
            if deletion.missing:
                return f"message {message_id} already deleted"
            raise AdminActionError(
                f"delete of message {message_id} "
                + ("refused" if deletion.refused else "outcome unknown")
            )
        outcome = await self._act(
            self._trigger(author_id, None, message_id=str(message_id), channel_id=str(channel_id)),
            Proposal(
                kind=gw.DELETE,
                target_user_id=author_id,
                target_username=author_name,
                channel_id=str(channel_id),
                message_ids=(str(message_id),),
                target_is_bot=False,
            ),
        )
        if outcome.state == gw.CONFIRMED:
            return f"deleted message {message_id}"
        if outcome.state in (gw.ABSENT,) or outcome.detail == "already deleted":
            return f"message {message_id} already deleted"
        if outcome.state == gw.SKIPPED and outcome.detail.startswith("protected: "):
            return (
                f"not deleted: the author of message {message_id} is protected "
                f"({_refused_reason(outcome)})"
            )
        self._raise_for(outcome, f"delete of message {message_id}")
        return f"message {message_id} not deleted: {outcome.detail}"

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

    async def delete_webhook(self, webhook_url: str) -> bool:
        """DELETE a leaked ``discord.com/api/webhooks/<id>/<token>`` URL.

        The URL is validated host-side by
        :func:`~smarter_dev.web.webhook_urls.parse_discord_webhook_url` FIRST: an
        arbitrary host, a path-traversal attempt, or a missing token raises
        :class:`AdminActionError` and issues NO request, so a laundered constant
        can never become an arbitrary-URL DELETE. That validator normalizes
        first, so the shapes leaks actually arrive in (no scheme, an uppercase
        scheme or host, a trailing ``?wait=true``) are killed rather than
        refused. A valid URL is deleted via its own token (no bot auth needed,
        but we keep the bot-token client for its rate-limit handling). Returns
        ``False`` on 404 (already dead) and ``True`` on success.
        """
        webhook = parse_discord_webhook_url(str(webhook_url))
        if webhook is None:
            raise AdminActionError(
                f"delete_webhook target is not a Discord webhook URL: {webhook_url!r}"
            )
        endpoint = f"/webhooks/{webhook.webhook_id}/{webhook.token}"
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
