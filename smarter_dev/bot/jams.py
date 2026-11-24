"""What the bot does for code jams.

Four jobs, all driven by what an administrator set up on the jam admin pages:

- post scheduled jam messages when they fall due;
- tag a new post in the showcase forum with the jam's tag and its year;
- give and take the Jammer role (``/jam join``, ``/jam leave``);
- make a private team thread and add teammates up to the jam's team size
  (``/jam team``, ``/jam invite``).

Nothing here keeps state of its own. Jams live in the web database, and a team
is its Discord thread: its members are the thread's members.

The functions take the REST client and return the text to answer with, so the
plugin (``smarter_dev.bot.plugins.jams``) is only wiring.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import hikari

logger = logging.getLogger(__name__)

# How long the bot trusts what it last read about a guild's jam. An admin's
# change (a new jam, a finished setup) reaches the bot within this time.
GUILD_JAM_TTL_SECONDS = 60.0

# A thread-create event for a post older than this is not a new post (Discord
# sends the same event when the bot is added to an existing thread).
NEW_POST_WINDOW = timedelta(minutes=10)

# Discord allows a forum post this many tags.
POST_TAG_LIMIT = 5

TEAM_NAME_MAX = 80
_TEAM_THREAD_ARCHIVE = timedelta(days=7)

NO_JAM = "There is no code jam open right now."
NOT_SET_UP = "The code jam is not set up in this server yet. Please tell a moderator."


@dataclass(frozen=True)
class JamInfo:
    """The jam a guild's members can take part in now."""

    name: str
    max_team_size: int
    starts_at: datetime
    submissions_close_at: datetime
    showcase_tag_id: str | None
    year_tag_id: str | None

    def accepts_entries(self, now: datetime) -> bool:
        """Whether a showcase post made at ``now`` is an entry to this jam."""
        return self.starts_at <= now <= self.submissions_close_at


@dataclass(frozen=True)
class GuildJam:
    """A guild's shared jam channels and role, and its current jam if any."""

    channels: dict[str, str | None]
    role_id: str | None
    jam: JamInfo | None


def _parse_guild_jam(data: dict[str, Any]) -> GuildJam:
    jam = data.get("jam")
    return GuildJam(
        channels=dict(data.get("channels") or {}),
        role_id=data.get("role_id"),
        jam=None
        if jam is None
        else JamInfo(
            name=jam["name"],
            max_team_size=int(jam["max_team_size"]),
            starts_at=datetime.fromisoformat(jam["starts_at"]),
            submissions_close_at=datetime.fromisoformat(jam["submissions_close_at"]),
            showcase_tag_id=jam.get("showcase_tag_id"),
            year_tag_id=jam.get("year_tag_id"),
        ),
    )


class JamApi:
    """The jam calls on the web API, with a short per-guild cache."""

    def __init__(
        self, api_client: Any, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._client = api_client
        self._clock = clock
        self._guild_jams: dict[str, tuple[float, GuildJam]] = {}

    async def guild_jam(self, guild_id: str) -> GuildJam | None:
        """What is set up for the guild; ``None`` if the API cannot say."""
        cached = self._guild_jams.get(guild_id)
        if cached and self._clock() - cached[0] < GUILD_JAM_TTL_SECONDS:
            return cached[1]
        try:
            response = await self._client.get(f"/guilds/{guild_id}/jam")
            info = _parse_guild_jam(response.json())
        except Exception as error:  # noqa: BLE001 - a jam lookup never breaks a listener
            logger.warning("jam lookup failed for guild %s: %r", guild_id, error)
            return None
        self._guild_jams[guild_id] = (self._clock(), info)
        return info

    async def claim_due_messages(self) -> list[dict[str, Any]]:
        """Claim the messages that are due. Each claim is handed out once."""
        response = await self._client.post("/jam-messages/claim")
        return list(response.json().get("messages") or [])

    async def mark_sent(self, message_id: str) -> None:
        await self._client.post(f"/jam-messages/{message_id}/mark-sent")

    async def mark_failed(self, message_id: str, error: str) -> None:
        await self._client.post(
            f"/jam-messages/{message_id}/mark-failed", json_data={"error": error}
        )


# -- Scheduled messages -------------------------------------------------------


async def post_due_messages(api: JamApi, rest: Any) -> int:
    """Claim and post every due jam message; returns how many were posted.

    A message is claimed before it is posted and never posted twice. If the
    bot cannot tell whether Discord took it, it is reported as failed with a
    note to check the channel, and an administrator decides.
    """
    posted = 0
    for message in await api.claim_due_messages():
        if await _post_message(api, rest, message):
            posted += 1
    return posted


async def _post_message(api: JamApi, rest: Any, message: dict[str, Any]) -> bool:
    message_id = str(message["id"])
    role_id = message.get("role_id")
    content = message["content"]
    if role_id:
        content = f"<@&{role_id}>\n{content}"
    try:
        await rest.create_message(
            int(message["channel_id"]),
            content,
            mentions_everyone=False,
            user_mentions=False,
            role_mentions=[int(role_id)] if role_id else False,
        )
    except hikari.ForbiddenError:
        problem = "The bot is not allowed to post in the channel."
    except hikari.NotFoundError:
        problem = "The channel no longer exists."
    except hikari.ClientHTTPResponseError as error:
        problem = f"Discord rejected the message (HTTP {int(error.status)})."
    except Exception as error:  # noqa: BLE001 - every outcome is reported
        logger.warning("jam message %s: unconfirmed send: %r", message_id, error)
        problem = (
            "Discord did not confirm the message. "
            "Check the channel before sending it again."
        )
    else:
        await _report(api.mark_sent(message_id), message_id)
        return True
    await _report(api.mark_failed(message_id, problem), message_id)
    return False


async def _report(call: Any, message_id: str) -> None:
    """Send a report to the web API; a lost report leaves the message stuck.

    The admin page shows a stuck message and lets an administrator decide,
    which is safer than the bot guessing and posting twice.
    """
    try:
        await call
    except Exception as error:  # noqa: BLE001 - one bad report must not stop the rest
        logger.error("jam message %s: could not report: %r", message_id, error)


# -- Showcase tags ------------------------------------------------------------


async def tag_showcase_post(
    api: JamApi, rest: Any, thread: Any, now: datetime | None = None
) -> bool:
    """Tag a new showcase post as an entry to the current jam.

    Returns whether tags were added. Only a post made in the guild's showcase
    forum while the jam is taking submissions is an entry.
    """
    now = now or datetime.now(UTC)
    if thread.type != hikari.ChannelType.GUILD_PUBLIC_THREAD:
        return False
    if now - thread.created_at > NEW_POST_WINDOW:
        return False
    info = await api.guild_jam(str(thread.guild_id))
    if info is None or info.jam is None:
        return False
    showcase_id = info.channels.get("showcase")
    if not showcase_id or str(thread.parent_id) != showcase_id:
        return False
    if not info.jam.accepts_entries(now):
        return False
    wanted = [tag for tag in (info.jam.showcase_tag_id, info.jam.year_tag_id) if tag]
    current = [str(tag) for tag in thread.applied_tag_ids]
    if all(tag in current for tag in wanted):
        return False
    # The jam's tags go first so they survive Discord's limit.
    tags = [*wanted, *(tag for tag in current if tag not in wanted)][:POST_TAG_LIMIT]
    try:
        await rest.edit_channel(
            thread.id,
            applied_tags=[hikari.Snowflake(tag) for tag in tags],
            reason="Code jam entry",
        )
    except hikari.HTTPError as error:
        logger.warning("could not tag jam entry %s: %r", thread.id, error)
        return False
    return True


# -- Jammer role --------------------------------------------------------------


async def join_jam(api: JamApi, rest: Any, guild_id: int, user_id: int) -> str:
    """Give the member the Jammer role."""
    info = await api.guild_jam(str(guild_id))
    if info is None or not info.role_id:
        return NOT_SET_UP
    if info.jam is None:
        return NO_JAM
    try:
        await rest.add_role_to_member(
            guild_id, user_id, int(info.role_id), reason="Joined the code jam"
        )
    except hikari.HTTPError as error:
        logger.warning("could not give the jam role in %s: %r", guild_id, error)
        return "I could not give you the role. Please tell a moderator."
    return (
        f"You're in **{info.jam.name}**. You have the <@&{info.role_id}> role "
        "and will be pinged with jam news. Use `/jam leave` to stop."
    )


async def leave_jam(api: JamApi, rest: Any, guild_id: int, user_id: int) -> str:
    """Take the Jammer role away. Works between jams too."""
    info = await api.guild_jam(str(guild_id))
    if info is None or not info.role_id:
        return NOT_SET_UP
    try:
        await rest.remove_role_from_member(
            guild_id, user_id, int(info.role_id), reason="Left the code jam"
        )
    except hikari.HTTPError as error:
        logger.warning("could not remove the jam role in %s: %r", guild_id, error)
        return "I could not remove the role. Please tell a moderator."
    return "Done. You no longer have the jam role and will not get jam pings."


# -- Teams --------------------------------------------------------------------


def clean_team_name(raw: str) -> str | None:
    """A team name fit for a thread title, or ``None`` if nothing is left."""
    name = " ".join(raw.split())
    return name if 0 < len(name) <= TEAM_NAME_MAX else None


async def create_team(
    api: JamApi, rest: Any, guild_id: int, user_id: int, raw_name: str
) -> str:
    """Make a private team thread in the teams channel and add its creator."""
    info = await api.guild_jam(str(guild_id))
    if info is None:
        return NOT_SET_UP
    if info.jam is None:
        return NO_JAM
    teams_channel_id = info.channels.get("teams")
    if not teams_channel_id:
        return NOT_SET_UP
    if info.jam.max_team_size < 2:
        return f"**{info.jam.name}** is a solo jam, so there are no teams."
    name = clean_team_name(raw_name)
    if name is None:
        return f"A team name needs 1 to {TEAM_NAME_MAX} characters."
    try:
        thread = await rest.create_thread(
            int(teams_channel_id),
            hikari.ChannelType.GUILD_PRIVATE_THREAD,
            name,
            auto_archive_duration=_TEAM_THREAD_ARCHIVE,
            # Only `/jam invite` adds people, so the team size limit holds.
            invitable=False,
            reason=f"Code jam team made by {user_id}",
        )
        await rest.add_thread_member(thread.id, user_id)
    except hikari.HTTPError as error:
        logger.warning("could not make a jam team in %s: %r", guild_id, error)
        return "I could not make the team thread. Please tell a moderator."
    others = info.jam.max_team_size - 1
    try:
        await rest.create_message(
            thread.id,
            f"This is your team's private thread for **{info.jam.name}**. "
            f"Add up to {others} teammate{'s' if others != 1 else ''} here "
            "with `/jam invite`.",
            mentions_everyone=False,
            user_mentions=False,
            role_mentions=False,
        )
    except hikari.HTTPError as error:
        logger.warning("could not post in jam team thread %s: %r", thread.id, error)
    return f"Your team thread is ready: <#{thread.id}>"


async def invite_to_team(
    api: JamApi,
    rest: Any,
    *,
    guild_id: int,
    channel_id: int,
    bot_user_id: int,
    target_id: int,
    target_is_bot: bool,
) -> str:
    """Add a member to the team thread the command was used in."""
    info = await api.guild_jam(str(guild_id))
    if info is None:
        return NOT_SET_UP
    if info.jam is None:
        return NO_JAM
    teams_channel_id = info.channels.get("teams")
    here = "Use this inside your team's thread. Make one with `/jam team`."
    try:
        thread = await rest.fetch_channel(channel_id)
    except hikari.HTTPError:
        return here
    if (
        not teams_channel_id
        or thread.type != hikari.ChannelType.GUILD_PRIVATE_THREAD
        or str(thread.parent_id) != teams_channel_id
    ):
        return here
    if target_is_bot:
        return "A bot cannot join a team."
    try:
        members = await rest.fetch_thread_members(channel_id)
    except hikari.HTTPError as error:
        logger.warning("could not read jam team %s: %r", channel_id, error)
        return "I could not read this team's members. Please try again."
    people = {int(member.user_id) for member in members} - {bot_user_id}
    if target_id in people:
        return f"<@{target_id}> is already on this team."
    limit = info.jam.max_team_size
    if len(people) >= limit:
        return f"This team is full. A team can have up to {limit} people."
    try:
        await rest.add_thread_member(channel_id, target_id)
    except hikari.HTTPError as error:
        logger.warning("could not add to jam team %s: %r", channel_id, error)
        return "I could not add them. They may have left the server."
    return f"<@{target_id}> is now on the team."
