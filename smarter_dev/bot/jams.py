"""What the bot does for code jams.

Four jobs, all driven by what an administrator set up on the jam admin pages:

- post scheduled jam messages when they fall due;
- tag a new post in the showcase forum with the jam's tag and its year;
- give and take the Jammer role (``/jam subscribe``, ``/jam unsubscribe``);
- make a private team thread and add teammates up to the jam's team size
  (``/jam team``, ``/jam invite``).

Jams live in the web database, and a team is its Discord thread: its members
are the thread's members. The only state kept here is :class:`TeamLimits`,
which is in memory and forgotten when the bot restarts.

The functions take the REST client and return the text to answer with, so the
plugin (``smarter_dev.bot.plugins.jams``) is only wiring.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from weakref import WeakValueDictionary

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

# One member may make one team thread in this long. Kept in memory, so a bot
# restart forgets it; the cap below is read from Discord and does not.
TEAM_COOLDOWN_SECONDS = 60 * 60.0

# The most team threads a guild may have open at once. Discord archives a
# quiet thread after a week, and an archived thread does not count.
TEAM_THREADS_MAX = 100

# The REST client's logger. It asks Discord again after a timeout or a server
# error and says so here.
_REST_LOGGER = "hikari.rest"

NO_JAM = "There is no code jam open right now."
NOT_SET_UP = "The code jam is not set up in this server yet. Please tell a moderator."
UNAVAILABLE = "I could not check the jam right now. Please try again in a minute."


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

    A message is claimed before it is posted, so it is handed to one bot
    process once. If the bot cannot tell whether Discord took it, it is
    reported as failed with a note to check the channel, and an administrator
    decides.

    One case can still post a message twice. When Discord times out or
    answers with a server error, the REST client asks again by itself, up to
    three times. If an earlier try did reach the channel, the message is
    there once for that try and once for the retry. Nothing here prevents
    that; a retry is logged as a warning so the channel can be checked.
    """
    posted = 0
    for message in await api.claim_due_messages():
        try:
            if await _post_message(api, rest, message):
                posted += 1
        except Exception:  # noqa: BLE001 - the rest of the batch still goes out
            logger.exception("jam message could not be handled: %r", message.get("id"))
    return posted


async def _post_message(api: JamApi, rest: Any, message: dict[str, Any]) -> bool:
    message_id = str(message["id"])
    role_id = message.get("role_id")
    content = message["content"]
    if role_id:
        content = f"<@&{role_id}>\n{content}"
    try:
        with _count_retries() as retries:
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
        _warn_of_retries(message_id, retries)
        await _report(api.mark_sent(message_id), message_id)
        return True
    _warn_of_retries(message_id, retries)
    await _report(api.mark_failed(message_id, problem), message_id)
    return False


class _RetryCounter(logging.Handler):
    """Counts the REST client's "asking again" warnings."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        # Only the timeout and 5xx retries. A rate-limit (429) retry is
        # logged as "Backing off and retrying request" and cannot duplicate:
        # Discord did not act on the request it turned away.
        if "backing off for" in str(record.msg):
            self.count += 1


@contextmanager
def _count_retries() -> Iterator[_RetryCounter]:
    """Count the requests the REST client sends again while this is open.

    The client retries inside one call and reports it only in its log, so the
    log is where it is read. A retry of some other request made at the same
    moment is counted too: the count means "check", not "duplicated".
    """
    counter = _RetryCounter()
    rest_logger = logging.getLogger(_REST_LOGGER)
    rest_logger.addHandler(counter)
    try:
        yield counter
    finally:
        rest_logger.removeHandler(counter)


def _warn_of_retries(message_id: str, retries: _RetryCounter) -> None:
    if retries.count:
        logger.warning(
            "jam message %s: Discord was asked again %d time(s) after a timeout "
            "or server error; the message may be in the channel twice",
            message_id,
            retries.count,
        )


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
    """Give the member the Jammer role, which is what jam news pings.

    The command is ``/jam subscribe``: the role only brings pings, and taking
    part in a jam does not need it.
    """
    info = await api.guild_jam(str(guild_id))
    if info is None:
        return UNAVAILABLE
    if not info.role_id:
        return NOT_SET_UP
    if info.jam is None:
        return NO_JAM
    try:
        await rest.add_role_to_member(
            guild_id, user_id, int(info.role_id), reason="Subscribed to code jam news"
        )
    except hikari.HTTPError as error:
        logger.warning("could not give the jam role in %s: %r", guild_id, error)
        return "I could not give you the role. Please tell a moderator."
    return (
        f"You will be pinged with **{info.jam.name}** news. You have the "
        f"<@&{info.role_id}> role. Use `/jam unsubscribe` to stop."
    )


async def leave_jam(api: JamApi, rest: Any, guild_id: int, user_id: int) -> str:
    """Take the Jammer role away (``/jam unsubscribe``). Works between jams too."""
    info = await api.guild_jam(str(guild_id))
    if info is None:
        return UNAVAILABLE
    if not info.role_id:
        return NOT_SET_UP
    try:
        await rest.remove_role_from_member(
            guild_id,
            user_id,
            int(info.role_id),
            reason="Unsubscribed from code jam news",
        )
    except hikari.HTTPError as error:
        logger.warning("could not remove the jam role in %s: %r", guild_id, error)
        return "I could not remove the role. Please tell a moderator."
    return "Done. You no longer have the jam role and will not get jam pings."


# -- Teams --------------------------------------------------------------------


class TeamLimits:
    """What keeps team threads from being made or filled without bound.

    Held in memory by the one bot process that answers commands.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # (guild, member) -> when their last team thread was made.
        self._made: dict[tuple[int, int], float] = {}
        # A lock nobody holds or waits for is dropped.
        self._locks: WeakValueDictionary[tuple[str, int], asyncio.Lock] = (
            WeakValueDictionary()
        )

    def _lock(self, kind: str, key: int) -> asyncio.Lock:
        lock = self._locks.get((kind, key))
        if lock is None:
            lock = self._locks[(kind, key)] = asyncio.Lock()
        return lock

    def guild_lock(self, guild_id: int) -> asyncio.Lock:
        """One team is made at a time in a guild, so the cap is not overrun."""
        return self._lock("guild", guild_id)

    def thread_lock(self, thread_id: int) -> asyncio.Lock:
        """One invite at a time in a thread, so two cannot share the last seat."""
        return self._lock("thread", thread_id)

    def wait_seconds(self, guild_id: int, user_id: int) -> float:
        """How long until the member may make another team; 0 if they may now."""
        made = self._made.get((guild_id, user_id))
        if made is None:
            return 0.0
        return max(0.0, TEAM_COOLDOWN_SECONDS - (self._clock() - made))

    def record(self, guild_id: int, user_id: int) -> None:
        """Note that the member just made a team, and forget expired notes."""
        now = self._clock()
        self._made = {
            key: made
            for key, made in self._made.items()
            if now - made < TEAM_COOLDOWN_SECONDS
        }
        self._made[(guild_id, user_id)] = now


def _minutes(seconds: float) -> str:
    minutes = max(1, -(-int(seconds) // 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def clean_team_name(raw: str) -> str | None:
    """A team name fit for a thread title, or ``None`` if nothing is left."""
    name = " ".join(raw.split())
    return name if 0 < len(name) <= TEAM_NAME_MAX else None


def _is_team_thread(
    thread: Any, teams_channel_id: str | None, bot_user_id: int
) -> bool:
    """A team is a private thread the bot made in the teams channel."""
    return bool(
        teams_channel_id
        and thread.type == hikari.ChannelType.GUILD_PRIVATE_THREAD
        and str(thread.parent_id) == teams_channel_id
        and int(thread.owner_id) == bot_user_id
    )


async def create_team(
    api: JamApi,
    rest: Any,
    limits: TeamLimits,
    guild_id: int,
    user_id: int,
    raw_name: str,
    *,
    bot_user_id: int,
) -> str:
    """Make a private team thread in the teams channel and add its creator.

    A member may make one team per :data:`TEAM_COOLDOWN_SECONDS`, and a guild
    may have :data:`TEAM_THREADS_MAX` team threads open. Only threads the bot
    made count towards that: a thread a member opens by hand in the teams
    channel is not a team, so members cannot use up the cap for everyone.
    """
    info = await api.guild_jam(str(guild_id))
    if info is None:
        return UNAVAILABLE
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
    async with limits.guild_lock(guild_id):
        wait = limits.wait_seconds(guild_id, user_id)
        if wait:
            return (
                "You made a team thread a little while ago. You can make another "
                f"in {_minutes(wait)}. To rename or remove a thread, ask a moderator."
            )
        failed = "I could not make the team thread. Please tell a moderator."
        try:
            threads = await rest.fetch_active_threads(guild_id)
        except hikari.HTTPError as error:
            logger.warning("could not count jam teams in %s: %r", guild_id, error)
            return failed
        open_teams = sum(
            1
            for thread in threads
            if _is_team_thread(thread, teams_channel_id, bot_user_id)
        )
        if open_teams >= TEAM_THREADS_MAX:
            logger.warning("jam team cap reached in %s: %d", guild_id, open_teams)
            return (
                f"There are already {open_teams} team threads open, which is the "
                "most this server allows. Please tell a moderator."
            )
        try:
            thread = await rest.create_thread(
                int(teams_channel_id),
                hikari.ChannelType.GUILD_PRIVATE_THREAD,
                name,
                auto_archive_duration=_TEAM_THREAD_ARCHIVE,
                # Members cannot add people themselves; `/jam invite` does it
                # and counts the seats.
                invitable=False,
                reason=f"Code jam team made by {user_id}",
            )
        except hikari.HTTPError as error:
            logger.warning("could not make a jam team in %s: %r", guild_id, error)
            return failed
        try:
            await rest.add_thread_member(thread.id, user_id)
        except hikari.HTTPError as error:
            logger.warning("could not add %s to their jam team: %r", user_id, error)
            await _remove_empty_team(rest, thread.id)
            return failed
        limits.record(guild_id, user_id)
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


async def _remove_empty_team(rest: Any, thread_id: Any) -> None:
    """Delete a team thread nobody could be added to, so it is not left over."""
    try:
        await rest.delete_channel(thread_id)
    except hikari.HTTPError as error:
        logger.warning("could not remove empty jam team %s: %r", thread_id, error)


async def invite_to_team(
    api: JamApi,
    rest: Any,
    limits: TeamLimits,
    *,
    guild_id: int,
    channel_id: int,
    bot_user_id: int,
    target_id: int,
    target_is_bot: bool,
) -> str:
    """Add a member to the team thread the command was used in.

    Only a thread the bot made counts as a team: a private thread a member
    made by hand in the teams channel lets its members add anyone, so its
    size could not be held to the limit.
    """
    info = await api.guild_jam(str(guild_id))
    if info is None:
        return UNAVAILABLE
    if info.jam is None:
        return NO_JAM
    teams_channel_id = info.channels.get("teams")
    here = "Use this inside your team's thread. Make one with `/jam team`."
    try:
        thread = await rest.fetch_channel(channel_id)
    except hikari.HTTPError:
        return here
    if not _is_team_thread(thread, teams_channel_id, bot_user_id):
        return here
    if target_is_bot:
        return "A bot cannot join a team."
    # Counted and added under the thread's lock: two invites sent together
    # would otherwise both see the last free seat.
    async with limits.thread_lock(channel_id):
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
            return (
                "I could not add them. Check that they are still in the server "
                "and that this thread is not locked, then try again."
            )
    return f"<@{target_id}> is now on the team."
