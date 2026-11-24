"""Create the Discord pieces jams need.

A guild's jams share one category, four channels and one role
(:class:`JamDiscordSetup`). Each jam adds two tags to the showcase forum: its
own and the year it starts in. :func:`run_jam_setup` brings Discord in line
with both, makes whatever is missing and records each id.

It is safe to run again. A piece whose stored id still exists is left alone,
a piece that already exists under the expected name is adopted rather than
duplicated, and a piece that fails is reported while the rest carry on, so a
second run only has the failed pieces left to do.

The caller commits; this module does no database work.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Protocol

import httpx

from smarter_dev.web.discord_admin_client import DiscordAdminError
from smarter_dev.web.discord_admin_client import DiscordChannel
from smarter_dev.web.discord_admin_client import DiscordRole
from smarter_dev.web.models import JAM_CHANNEL_COLUMNS
from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamDiscordSetup

CATEGORY_NAME = "Code Jams"
ROLE_NAME = "Jammer"
CHANNEL_NAMES: dict[str, str] = {key: f"jam-{key}" for key in JAM_CHANNEL_COLUMNS}

# Discord allows a forum this many tags.
FORUM_TAG_LIMIT = 20

_TEXT, _CATEGORY, _NEWS, _FORUM = 0, 4, 5, 15
_CHANNEL_TYPES: dict[str, int] = {
    "news": _TEXT,
    "chat": _TEXT,
    "teams": _TEXT,
    "showcase": _FORUM,
}
_TOPICS: dict[str, str] = {
    "news": "Code jam announcements.",
    "chat": "Talk about the current code jam.",
    "teams": "Find teammates for the current code jam.",
    "showcase": "Post your code jam entry here. One post per entry.",
}

_VIEW_CHANNEL = 1 << 10
_SEND_MESSAGES = 1 << 11
_ROLE_OVERWRITE, _MEMBER_OVERWRITE = 0, 1

_AUDIT_REASON = "Code jam setup from the admin panel"

# What a Discord call can raise: a refusal from Discord, or no answer at all
# (the client lets network failures through as httpx errors).
DISCORD_ERRORS = (DiscordAdminError, httpx.HTTPError)


class JamDiscordClient(Protocol):
    """The Discord calls setup needs; ``DiscordAdminClient`` provides them."""

    async def get_guild_channels(self, guild_id: str) -> list[DiscordChannel]: ...
    async def get_guild_roles(self, guild_id: str) -> list[DiscordRole]: ...
    async def get_bot_user_id(self) -> str: ...
    async def create_guild_channel(
        self, guild_id: str, payload: dict, *, reason: str
    ) -> DiscordChannel: ...
    async def create_guild_role(
        self, guild_id: str, name: str, *, reason: str
    ) -> DiscordRole: ...
    async def get_forum_tags(self, channel_id: str) -> list[dict]: ...
    async def set_forum_tags(
        self, channel_id: str, tags: list[dict], *, reason: str
    ) -> list[dict]: ...


@dataclass
class JamSetupReport:
    """What one setup run did, in words an admin can read."""

    created: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_kind(channel: DiscordChannel, key: str) -> bool:
    """Whether a channel can serve as the jam channel ``key``.

    An administrator may turn #jam-news into an Announcement channel; it is
    still the news channel.
    """
    if key == "news":
        return channel.type in (_TEXT, _NEWS)
    return channel.type == _CHANNEL_TYPES[key]


def _why(exc: Exception, permission: str) -> str:
    """A plain reason for a failed Discord call.

    The exception text quotes the request and Discord's raw body, which is
    noise on an admin page; the status is what tells the admin what to fix.
    """
    status = getattr(exc, "status_code", None)
    if status == 403:
        return f"the bot does not have the {permission} permission"
    if status == 429:
        return "Discord is rate limiting the bot; try again in a minute"
    if status is None:
        return "Discord could not be reached"
    return f"Discord refused the request (HTTP {status})"


async def run_jam_setup(
    client: JamDiscordClient, setup: JamDiscordSetup, jam: Jam | None = None
) -> JamSetupReport:
    """Create the missing shared pieces, then ``jam``'s showcase tags.

    Ids are recorded on ``setup`` and ``jam`` as each piece is made.
    """
    report = JamSetupReport()
    try:
        channels = await client.get_guild_channels(setup.guild_id)
        roles = await client.get_guild_roles(setup.guild_id)
    except DISCORD_ERRORS as exc:
        report.errors.append(
            "Could not read the server's channels and roles: "
            + _why(exc, "View Channels")
        )
        return report

    await _ensure_role(client, setup, roles, report)
    if await _ensure_category(client, setup, channels, report):
        for key in JAM_CHANNEL_COLUMNS:
            await _ensure_channel(client, setup, key, channels, report)
    if jam is not None:
        await _ensure_tags(client, setup, jam, report)
    return report


async def _ensure_role(
    client: JamDiscordClient,
    setup: JamDiscordSetup,
    roles: list[DiscordRole],
    report: JamSetupReport,
) -> None:
    if setup.role_id and any(role.id == setup.role_id for role in roles):
        return
    label = f"@{ROLE_NAME} role"
    setup.role_id = None
    same_name = [role for role in roles if role.name.lower() == ROLE_NAME.lower()]
    # Members will be able to give themselves this role, so an existing one
    # is only picked up if holding it grants nothing.
    harmless = next(
        (role for role in same_name if not role.managed and role.permissions == 0),
        None,
    )
    if harmless is not None:
        setup.role_id = harmless.id
        report.adopted.append(label)
        return
    if same_name:
        report.errors.append(
            f"{label}: the server already has a role with this name that "
            "grants permissions. Rename that role or clear its permissions"
        )
        return
    try:
        role = await client.create_guild_role(
            setup.guild_id, ROLE_NAME, reason=_AUDIT_REASON
        )
    except DISCORD_ERRORS as exc:
        report.errors.append(f"{label}: {_why(exc, 'Manage Roles')}")
        return
    setup.role_id = role.id
    report.created.append(label)


async def _ensure_category(
    client: JamDiscordClient,
    setup: JamDiscordSetup,
    channels: list[DiscordChannel],
    report: JamSetupReport,
) -> bool:
    """Make sure ``setup.category_id`` names a live category; False if it cannot."""
    categories = [channel for channel in channels if channel.type == _CATEGORY]
    if setup.category_id and any(c.id == setup.category_id for c in categories):
        return True
    label = f"{CATEGORY_NAME} category"
    existing = next(
        (c for c in categories if c.name.lower() == CATEGORY_NAME.lower()), None
    )
    if existing is not None:
        setup.category_id = existing.id
        report.adopted.append(label)
        return True
    setup.category_id = None
    try:
        category = await client.create_guild_channel(
            setup.guild_id,
            {"name": CATEGORY_NAME, "type": _CATEGORY},
            reason=_AUDIT_REASON,
        )
    except DISCORD_ERRORS as exc:
        report.errors.append(
            f"{label}: {_why(exc, 'Manage Channels')}. "
            "No channels were created because they belong in it"
        )
        return False
    setup.category_id = category.id
    report.created.append(label)
    return True


async def _ensure_channel(
    client: JamDiscordClient,
    setup: JamDiscordSetup,
    key: str,
    channels: list[DiscordChannel],
    report: JamSetupReport,
) -> None:
    column = JAM_CHANNEL_COLUMNS[key]
    stored = getattr(setup, column)
    name = CHANNEL_NAMES[key]
    label = f"#{name}"
    kind = _CHANNEL_TYPES[key]
    # A stored channel is kept wherever it lives, so an admin can point the
    # showcase at a forum the server already has.
    if stored and any(c.id == stored and _is_kind(c, key) for c in channels):
        return
    existing = next(
        (
            channel
            for channel in channels
            if channel.name == name
            and _is_kind(channel, key)
            and channel.parent_id == setup.category_id
        ),
        None,
    )
    if existing is not None:
        setattr(setup, column, existing.id)
        report.adopted.append(label)
        return
    setattr(setup, column, None)
    payload: dict = {
        "name": name,
        "type": kind,
        "parent_id": setup.category_id,
        "topic": _TOPICS[key],
    }
    try:
        if key == "news":
            payload["permission_overwrites"] = await _read_only_overwrites(
                client, setup.guild_id
            )
        channel = await client.create_guild_channel(
            setup.guild_id, payload, reason=_AUDIT_REASON
        )
    except DISCORD_ERRORS as exc:
        report.errors.append(f"{label}: {_why(exc, 'Manage Channels')}")
        return
    setattr(setup, column, channel.id)
    report.created.append(label)


async def _read_only_overwrites(client: JamDiscordClient, guild_id: str) -> list[dict]:
    """Overwrites for a channel only the bot (and server staff) can post in.

    Only Send Messages is denied. Discord lets a bot set an overwrite only for
    a permission the bot itself holds, and Send Messages is the one it is
    certain to have. The @everyone role's id is the guild's id.
    """
    bot_user_id = await client.get_bot_user_id()
    return [
        {
            "id": guild_id,
            "type": _ROLE_OVERWRITE,
            "allow": "0",
            "deny": str(_SEND_MESSAGES),
        },
        {
            "id": bot_user_id,
            "type": _MEMBER_OVERWRITE,
            "allow": str(_VIEW_CHANNEL | _SEND_MESSAGES),
            "deny": "0",
        },
    ]


async def _ensure_tags(
    client: JamDiscordClient,
    setup: JamDiscordSetup,
    jam: Jam,
    report: JamSetupReport,
) -> None:
    """Make sure the showcase forum has the jam's tag and its year tag.

    A tag that already exists under the same name is used as it is; that is
    how every jam in a year shares one year tag. New tags are moderated, so
    only the bot and server staff can put them on a post.
    """
    wanted = {"showcase_tag_id": jam.tag_name, "year_tag_id": jam.year_tag_name}
    labels = {column: f"“{name}” showcase tag" for column, name in wanted.items()}
    forum_id = setup.showcase_channel_id
    if not forum_id:
        report.errors.extend(
            f"{labels[column]}: #{CHANNEL_NAMES['showcase']} does not exist yet"
            for column in wanted
            if not getattr(jam, column)
        )
        return
    try:
        tags = await client.get_forum_tags(forum_id)
    except DISCORD_ERRORS as exc:
        report.errors.append(
            "Could not read the showcase forum's tags: " + _why(exc, "View Channels")
        )
        return

    by_name = {str(tag["name"]).lower(): tag for tag in tags}
    live_ids = {tag["id"] for tag in tags}
    to_create: list[str] = []
    for column, name in wanted.items():
        if getattr(jam, column) in live_ids:
            continue
        existing = by_name.get(name.lower())
        if existing is not None:
            setattr(jam, column, existing["id"])
            report.adopted.append(labels[column])
        else:
            setattr(jam, column, None)
            to_create.append(column)
    if not to_create:
        return

    if len(tags) + len(to_create) > FORUM_TAG_LIMIT:
        report.errors.extend(
            f"{labels[column]}: the forum already has {len(tags)} of the "
            f"{FORUM_TAG_LIMIT} tags Discord allows. Delete one it no longer needs"
            for column in to_create
        )
        return
    new_tags = [{"name": wanted[column], "moderated": True} for column in to_create]
    try:
        stored = await client.set_forum_tags(
            forum_id, [*tags, *new_tags], reason=_AUDIT_REASON
        )
    except DISCORD_ERRORS as exc:
        report.errors.extend(
            f"{labels[column]}: {_why(exc, 'Manage Channels')}" for column in to_create
        )
        return
    stored_by_name = {str(tag["name"]).lower(): tag for tag in stored}
    for column in to_create:
        tag = stored_by_name.get(wanted[column].lower())
        if tag is None:
            report.errors.append(f"{labels[column]}: Discord did not create it")
            continue
        setattr(jam, column, tag["id"])
        report.created.append(labels[column])
