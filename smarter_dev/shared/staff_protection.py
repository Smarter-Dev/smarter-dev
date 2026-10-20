"""Who automated moderation may never act on.

Every automated actor (content filter, spam engine, attachment filter, spam
ring, AI triage, admin-handler scripts) asks this one rule before it times out,
bans, kicks, warns or deletes a message of a member. Protected are:

* the bot itself, and any other bot;
* the guild owner;
* holders of a staff role (:data:`STAFF_ROLE_IDS`, plus any the caller's
  config adds);
* anyone holding one of :data:`STAFF_PERMISSIONS` through their roles at guild
  level, or through a permission overwrite in any channel of the guild.

What cannot be established counts as protected: an unresolvable role, a
missing guild or channel list, a lookup that failed. A user Discord reports as
not a member holds no guild permission, so they are protected only when they
are a bot.

This module is the pure rule, with Discord's permission bits as plain ints so
both the gateway bot (hikari) and the REST-only worker can feed it. The
adapters that gather the facts live in ``smarter_dev.bot.staff_protection``
and ``smarter_dev.web.admin_actions``.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass

#: The guild's two staff roles. Protected whatever permissions they carry.
STAFF_ROLE_IDS = frozenset({"644390354157568014", "644301991832453120"})

# Discord permission bits (https://discord.com/developers/docs/topics/permissions).
KICK_MEMBERS = 1 << 1
BAN_MEMBERS = 1 << 2
ADMINISTRATOR = 1 << 3
MANAGE_CHANNELS = 1 << 4
MANAGE_GUILD = 1 << 5
VIEW_AUDIT_LOG = 1 << 7
MANAGE_MESSAGES = 1 << 13
MANAGE_NICKNAMES = 1 << 27
MANAGE_ROLES = 1 << 28
MANAGE_WEBHOOKS = 1 << 29
MODERATE_MEMBERS = 1 << 40
MENTION_EVERYONE = 1 << 17
MUTE_MEMBERS = 1 << 22
DEAFEN_MEMBERS = 1 << 23
MOVE_MEMBERS = 1 << 24
MANAGE_GUILD_EXPRESSIONS = 1 << 30
MANAGE_EVENTS = 1 << 33
MANAGE_THREADS = 1 << 34

#: Holding any of these, at guild level or in any channel, makes a member staff.
STAFF_PERMISSIONS = (
    ADMINISTRATOR
    | MANAGE_GUILD
    | MANAGE_ROLES
    | MANAGE_CHANNELS
    | MANAGE_MESSAGES
    | MODERATE_MEMBERS
    | KICK_MEMBERS
    | BAN_MEMBERS
    | MANAGE_NICKNAMES
    | MANAGE_WEBHOOKS
    | VIEW_AUDIT_LOG
)

#: Not staff standing (holding one does not protect a member), but real power
#: over others: mass pings, locking or deleting threads, muting or moving
#: people in voice, editing events and emoji. Automation never hands out or
#: takes away a role carrying one (:func:`role_refusal`).
ELEVATED_PERMISSIONS = (
    MENTION_EVERYONE
    | MANAGE_THREADS
    | MANAGE_EVENTS
    | MANAGE_GUILD_EXPRESSIONS
    | MUTE_MEMBERS
    | DEAFEN_MEMBERS
    | MOVE_MEMBERS
)

# Refusal reasons. Callers put them in logs, tool results and reports.
BOT_ITSELF = "the bot itself"
A_BOT = "a bot"
GUILD_OWNER = "the server owner"
STAFF_ROLE = "holds a staff role"
GUILD_STAFF_PERMISSION = "holds a staff permission"
CHANNEL_STAFF_PERMISSION = "holds a staff permission in a channel"
UNKNOWN_PREFIX = "could not be checked"


def unknown(detail: str) -> str:
    """The refusal for a target whose protection could not be established."""
    return f"{UNKNOWN_PREFIX} ({detail}); treated as protected"


@dataclass(frozen=True, slots=True)
class Overwrite:
    """One channel permission overwrite, as Discord's allow/deny bit sets."""

    allow: int
    deny: int


@dataclass(frozen=True, slots=True)
class GuildFacts:
    """The guild-wide facts the rule reads.

    ``role_permissions`` maps every role id the guild has to its permission
    bits; @everyone's id is the guild's own. ``channel_overwrites`` holds each
    channel's overwrites, keyed by role or member id.
    """

    guild_id: str
    owner_id: str | None
    role_permissions: Mapping[str, int]
    channel_overwrites: tuple[Mapping[str, Overwrite], ...]


@dataclass(frozen=True, slots=True)
class MemberFacts:
    """A guild member as the rule needs them."""

    user_id: str
    is_bot: bool
    role_ids: tuple[str, ...]


def apply_channel_overwrites(
    base: int,
    overwrites: Mapping[str, Overwrite],
    *,
    everyone_id: str,
    role_ids: Iterable[str],
    member_id: str,
) -> int:
    """Discord's overwrite order: @everyone, then the member's roles, then them."""
    permissions = base
    if everyone := overwrites.get(everyone_id):
        permissions = (permissions & ~everyone.deny) | everyone.allow
    allow = 0
    deny = 0
    for role_id in role_ids:
        if role := overwrites.get(str(role_id)):
            allow |= role.allow
            deny |= role.deny
    permissions = (permissions & ~deny) | allow
    if member := overwrites.get(member_id):
        permissions = (permissions & ~member.deny) | member.allow
    return permissions


def member_refusal(
    member: MemberFacts,
    guild: GuildFacts,
    *,
    bot_user_id: str | None,
    extra_role_ids: Iterable[str] = (),
) -> str | None:
    """Why ``member`` may not be acted on, or None when they may.

    ``bot_user_id`` None means the bot's own id is unknown, which fails closed.
    """
    if bot_user_id is None:
        return unknown("the bot's own id is unknown")
    if member.user_id == bot_user_id:
        return BOT_ITSELF
    if member.is_bot:
        return A_BOT
    if guild.owner_id is None:
        return unknown("the server owner is unknown")
    if member.user_id == guild.owner_id:
        return GUILD_OWNER
    role_ids = tuple(str(role_id) for role_id in member.role_ids)
    staff_roles = STAFF_ROLE_IDS | {str(role_id) for role_id in extra_role_ids}
    if staff_roles.intersection(role_ids):
        return STAFF_ROLE
    permissions = 0
    for role_id in (guild.guild_id, *role_ids):
        role_permissions = guild.role_permissions.get(role_id)
        if role_permissions is None:
            return unknown(f"role {role_id} could not be resolved")
        permissions |= int(role_permissions)
    if permissions & STAFF_PERMISSIONS:
        return GUILD_STAFF_PERMISSION
    for overwrites in guild.channel_overwrites:
        in_channel = apply_channel_overwrites(
            permissions,
            overwrites,
            everyone_id=guild.guild_id,
            role_ids=role_ids,
            member_id=member.user_id,
        )
        if in_channel & STAFF_PERMISSIONS:
            return CHANNEL_STAFF_PERMISSION
    return None


def non_member_refusal(
    user_id: str, *, is_bot: bool | None, bot_user_id: str | None
) -> str | None:
    """The rule for a user Discord reports as not in the guild.

    They hold no guild permission and cannot own the guild, so only being a
    bot (or not knowing whether they are one) protects them.
    """
    if bot_user_id is not None and user_id == bot_user_id:
        return BOT_ITSELF
    if is_bot is None:
        return unknown("whether the user is a bot is unknown")
    if is_bot:
        return A_BOT
    return None


# Why a role may not be granted or removed by automation.
A_STAFF_ROLE = "is a staff role"
ROLE_STAFF_PERMISSION = "carries a staff permission"
ROLE_CHANNEL_STAFF_PERMISSION = "carries a staff permission in a channel"
ROLE_ELEVATED_PERMISSION = "carries an elevated permission"


def role_refusal(
    role_id: str, guild: GuildFacts, *, extra_role_ids: Iterable[str] = ()
) -> str | None:
    """Why automation may not grant or remove ``role_id``, or None when it may.

    A role that makes its holder staff under :func:`member_refusal` is never
    changed by automation: granting one would put a member out of moderation's
    reach, and removing one would let a later run punish a moderator (strip the
    role in one fire, ban in the next). Nor is a role carrying one of
    :data:`ELEVATED_PERMISSIONS`. The @everyone role, an unknown role and a
    role the guild facts cannot resolve are refused too.
    """
    rid = str(role_id)
    if rid == str(guild.guild_id):
        return "is the @everyone role"
    staff_roles = STAFF_ROLE_IDS | {str(extra) for extra in extra_role_ids}
    if rid in staff_roles:
        return A_STAFF_ROLE
    permissions = guild.role_permissions.get(rid)
    if permissions is None:
        return unknown(f"role {rid} could not be resolved")
    if int(permissions) & STAFF_PERMISSIONS:
        return ROLE_STAFF_PERMISSION
    for overwrites in guild.channel_overwrites:
        overwrite = overwrites.get(rid)
        if overwrite is not None and overwrite.allow & STAFF_PERMISSIONS:
            return ROLE_CHANNEL_STAFF_PERMISSION
    if int(permissions) & ELEVATED_PERMISSIONS or any(
        overwrite.allow & ELEVATED_PERMISSIONS
        for overwrites in guild.channel_overwrites
        if (overwrite := overwrites.get(rid)) is not None
    ):
        return ROLE_ELEVATED_PERMISSION
    return None
