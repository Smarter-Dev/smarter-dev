"""The bot's side of code jams: scheduled posts, showcase entries, role, teams."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import hikari
import pytest

from smarter_dev.bot import client
from smarter_dev.bot import jams
from smarter_dev.bot import leadership
from smarter_dev.bot.plugins import jams as jams_plugin
from tests.bot.fake_discord import forbidden
from tests.bot.fake_discord import not_found
from tests.bot.fake_discord import server_error

GUILD = 644299523686006834
NOW = datetime(2026, 12, 10, tzinfo=UTC)
BOT_ID = 1000
CHAT = 12
SHOWCASE = 14
ROLE = 20
THREAD = 500
DM = 900


def guild_jam(*, jam: bool = True, **overrides) -> dict:
    body = {
        "channels": {"news": "11", "chat": "12", "showcase": "14"},
        "role_id": "20",
        "jam": {
            "id": "j1",
            "name": "Holiday Chatbot Jam",
            "max_team_size": 3,
            "starts_at": "2026-12-01T00:00:00+00:00",
            "submissions_close_at": "2027-01-03T00:00:00+00:00",
            "showcase_tag_id": "31",
            "year_tag_id": "32",
        }
        if jam
        else None,
    }
    body.update(overrides)
    return body


JAM_API = f"/guilds/{GUILD}/jam"


class FakeApi:
    """The web API as the bot sees it: one guild's jam and a queue of claims.

    It keeps teams and showcase posts the way the web API does, so a test can
    read ``teams`` and ``entries`` afterwards. ``refuse`` makes one kind of
    team change fail with a given reason: ``{"teams": "on_a_team"}``.
    """

    def __init__(self, body: dict | None = None, due: list[dict] | None = None):
        self.body = guild_jam() if body is None else body
        self.due = list(due or [])
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict | None]] = []
        self.fail_get = False
        self.fail_posts: set[str] = set()
        # Paths whose change is saved but whose reply never arrives.
        self.lose_replies: set[str] = set()
        # thread id -> {"name", "thread_id", "member_ids"}
        self.teams: dict[str, dict] = {}
        # showcase thread id -> the team's thread id, or "solo:<author>"
        self.entries: dict[str, str] = {}
        self.refuse: dict[str, str] = {}
        # What ``/jam-work`` answers, and what its reports are answered with.
        self.work: list[dict] = []
        self.work_reply: dict = {"ok": True, "reason": None}

    def add_team(self, thread_id: int = THREAD, *members: int, name: str = "Elves"):
        self.teams[str(thread_id)] = {
            "name": name,
            "thread_id": str(thread_id),
            "member_ids": [str(member) for member in members or (1,)],
        }

    def _team_of(self, user_id: str) -> dict | None:
        for team in self.teams.values():
            if user_id in team["member_ids"]:
                return team
        return None

    def _has_entry(self, owner: str) -> bool:
        return owner in self.entries.values()

    def _cannot_join(self, user_id: str) -> dict | None:
        if (team := self._team_of(user_id)) is not None:
            return {"ok": False, "reason": "on_a_team", "team": team}
        if self._has_entry(f"solo:{user_id}"):
            return {"ok": False, "reason": "has_entry", "team": None}
        return None

    async def get(self, path, **_):
        self.gets.append(path)
        if self.fail_get:
            raise RuntimeError("api down")
        if path.startswith(f"{JAM_API}/members/"):
            user_id = path.rsplit("/", 1)[1]
            body = self._cannot_join(user_id) or {
                "ok": True,
                "reason": None,
                "team": None,
            }
            if "members" in self.refuse:
                body = {"ok": False, "reason": self.refuse["members"], "team": None}
            return SimpleNamespace(json=lambda: body)
        if path == "/jam-work":
            work = list(self.work)
            return SimpleNamespace(json=lambda: {"jams": work})
        return SimpleNamespace(json=lambda: self.body)

    async def post(self, path, json_data=None):
        if any(path.endswith(suffix) for suffix in self.fail_posts):
            raise RuntimeError("api down")
        self.posts.append((path, json_data))
        if any(path.endswith(suffix) for suffix in self.lose_replies):
            self._jam_post(path[len(JAM_API) + 1 :].split("/"), json_data)
            raise RuntimeError("timed out")
        if path == "/jam-messages/claim":
            due, self.due = self.due, []
            return SimpleNamespace(json=lambda: {"messages": due})
        if path.startswith(f"{JAM_API}/"):
            body = self._jam_post(path[len(JAM_API) + 1 :].split("/"), json_data)
            return SimpleNamespace(json=lambda: body)
        if path.startswith("/jam-work/"):
            return SimpleNamespace(json=lambda: dict(self.work_reply))
        return SimpleNamespace(json=lambda: {"success": True})

    def _jam_post(self, parts: list[str], data: dict) -> dict:
        kind = parts[-1]
        if kind in self.refuse:
            return {"ok": False, "reason": self.refuse[kind], "team": None}
        done = {"ok": True, "reason": None, "already": False, "members_left": 0}
        if parts == ["teams"]:
            if (refused := self._cannot_join(data["user_id"])) is not None:
                return refused
            self.add_team(int(data["thread_id"]), data["user_id"], name=data["name"])
            return {**done, "team": self.teams[data["thread_id"]]}
        if parts == ["submissions"]:
            thread_id, author = data["thread_id"], data["author_id"]
            team = self._team_of(author)
            owner = team["thread_id"] if team else f"solo:{author}"
            new = thread_id not in self.entries
            self.entries.setdefault(thread_id, owner)
            first = min(
                (post for post, whose in self.entries.items() if whose == owner),
                key=int,
            )
            return {
                "ok": True,
                "reason": None,
                "team": team,
                "entry_thread_id": first,
                "new": new,
            }
        if parts[0] == "submissions":
            return {"removed": self.entries.pop(parts[1], None) is not None}
        team = self.teams.get(parts[1])
        if kind == "leave":
            if team is None or data["user_id"] not in team["member_ids"]:
                return {**done, "team": team, "already": True}
            if self._has_entry(team["thread_id"]):
                return {"ok": False, "reason": "team_has_entry", "team": team}
            team["member_ids"].remove(data["user_id"])
            if not team["member_ids"]:
                del self.teams[parts[1]]
            return {**done, "team": team, "members_left": len(team["member_ids"])}
        # members
        if team is None:
            return {"ok": False, "reason": "not_a_team", "team": None}
        if data["invited_by"] not in team["member_ids"]:
            return {"ok": False, "reason": "not_your_team", "team": team}
        if data["user_id"] in team["member_ids"]:
            return {**done, "team": team, "already": True}
        if self._has_entry(team["thread_id"]):
            return {"ok": False, "reason": "team_has_entry", "team": team}
        if (refused := self._cannot_join(data["user_id"])) is not None:
            return refused
        if len(team["member_ids"]) >= self.body["jam"]["max_team_size"]:
            return {"ok": False, "reason": "full", "team": team}
        team["member_ids"].append(data["user_id"])
        return {**done, "team": team}


def make_rest() -> Mock:
    rest = Mock()
    for name in (
        "create_message",
        "edit_channel",
        "add_role_to_member",
        "remove_role_from_member",
        "add_thread_member",
        "remove_thread_member",
        "delete_channel",
    ):
        setattr(rest, name, AsyncMock())
    rest.create_thread = AsyncMock(return_value=SimpleNamespace(id=THREAD))
    rest.fetch_active_threads = AsyncMock(return_value=[])
    rest.create_dm_channel = AsyncMock(return_value=SimpleNamespace(id=DM))
    rest.fetch_public_archived_threads = Mock(side_effect=lambda channel: lazy([]))
    return rest


def lazy(items):
    """What hikari returns for a paged list: something to ``async for`` over."""

    async def each():
        for item in items:
            yield item

    return each()


def team_thread(**overrides) -> SimpleNamespace:
    """A team thread as `/jam team` makes it: private, in #jam-chat, the bot's."""
    values = {
        "type": hikari.ChannelType.GUILD_PRIVATE_THREAD,
        "parent_id": CHAT,
        "owner_id": BOT_ID,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def due_message(**overrides) -> dict:
    message = {
        "id": "m1",
        "guild_id": str(GUILD),
        "channel_id": "11",
        "content": "The jam is open!",
        "role_id": None,
    }
    message.update(overrides)
    return message


def showcase_post(**overrides) -> SimpleNamespace:
    values = {
        "id": 700,
        "guild_id": GUILD,
        "parent_id": SHOWCASE,
        "owner_id": 1,
        "type": hikari.ChannelType.GUILD_PUBLIC_THREAD,
        "created_at": NOW - timedelta(seconds=2),
        "applied_tag_ids": [],
        "is_archived": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# -- reading the guild's jam ---------------------------------------------------


async def test_guild_jam_is_read_once_within_the_cache_time():
    now = [0.0]
    api = FakeApi()
    jam_api = jams.JamApi(api, clock=lambda: now[0])

    first = await jam_api.guild_jam(str(GUILD))
    now[0] = jams.GUILD_JAM_TTL_SECONDS - 1
    second = await jam_api.guild_jam(str(GUILD))
    now[0] = jams.GUILD_JAM_TTL_SECONDS
    await jam_api.guild_jam(str(GUILD))

    assert first is second
    assert first.jam.max_team_size == 3
    assert first.jam.starts_at == datetime(2026, 12, 1, tzinfo=UTC)
    assert api.gets == [f"/guilds/{GUILD}/jam"] * 2


async def test_a_failed_lookup_is_not_remembered():
    api = FakeApi()
    api.fail_get = True
    jam_api = jams.JamApi(api)

    assert await jam_api.guild_jam(str(GUILD)) is None
    api.fail_get = False

    assert (await jam_api.guild_jam(str(GUILD))).role_id == "20"


# -- scheduled messages --------------------------------------------------------


async def test_a_due_message_is_posted_and_reported_sent():
    api = FakeApi(due=[due_message()])
    rest = make_rest()

    posted = await jams.post_due_messages(jams.JamApi(api), rest)

    assert posted == 1
    rest.create_message.assert_awaited_once_with(
        11,
        "The jam is open!",
        mentions_everyone=False,
        user_mentions=False,
        role_mentions=False,
    )
    assert api.posts == [
        ("/jam-messages/claim", None),
        ("/jam-messages/m1/mark-sent", None),
    ]


async def test_a_ping_mentions_only_the_jam_role():
    api = FakeApi(due=[due_message(role_id="20", content="@everyone go")])
    rest = make_rest()

    await jams.post_due_messages(jams.JamApi(api), rest)

    call = rest.create_message.await_args
    assert call.args == (11, "<@&20>\n@everyone go")
    assert call.kwargs == {
        "mentions_everyone": False,
        "user_mentions": False,
        "role_mentions": [20],
    }


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (forbidden(), "not allowed to post"),
        (not_found(), "no longer exists"),
        (
            hikari.BadRequestError(
                url="https://discord.test", headers={}, raw_body=b""
            ),
            "HTTP 400",
        ),
        (server_error(), "Check the channel before sending it again"),
        (TimeoutError(), "Check the channel before sending it again"),
    ],
)
async def test_a_failed_post_is_reported_with_a_plain_reason(error, reason):
    api = FakeApi(due=[due_message()])
    rest = make_rest()
    rest.create_message.side_effect = error

    posted = await jams.post_due_messages(jams.JamApi(api), rest)

    assert posted == 0
    path, body = api.posts[-1]
    assert path == "/jam-messages/m1/mark-failed"
    assert reason in body["error"]
    assert not any(path.endswith("mark-sent") for path, _ in api.posts)


async def test_one_failure_does_not_stop_the_next_message():
    api = FakeApi(due=[due_message(id="m1"), due_message(id="m2")])
    rest = make_rest()
    rest.create_message.side_effect = [forbidden(), None]

    assert await jams.post_due_messages(jams.JamApi(api), rest) == 1
    assert [path for path, _ in api.posts[1:]] == [
        "/jam-messages/m1/mark-failed",
        "/jam-messages/m2/mark-sent",
    ]


def _hikari_retries(times: int):
    """A create_message that hikari had to send again ``times`` times."""

    async def create_message(*args, **kwargs):
        for left in range(times, 0, -1):
            # The line hikari's REST client logs before each retry.
            logging.getLogger("hikari.rest").warning(
                "Received status %s on request, backing off for %.2fs and retrying. "
                "Retries remaining: %s",
                502,
                0.5,
                left,
            )

    return create_message


async def test_a_rate_limit_retry_is_not_reported_as_a_possible_duplicate(caplog):
    async def create_message(*args, **kwargs):
        # hikari's line for a 429: Discord turned the request away unposted.
        logging.getLogger("hikari.rest").warning(
            "you are being rate-limited on bucket %s for route '%s'. "
            "Backing off and retrying request...",
            "b",
            "r",
        )

    api = FakeApi(due=[due_message()])
    rest = make_rest()
    rest.create_message.side_effect = create_message

    with caplog.at_level(logging.WARNING, logger="smarter_dev.bot.jams"):
        assert await jams.post_due_messages(jams.JamApi(api), rest) == 1

    assert not [r for r in caplog.records if r.name == "smarter_dev.bot.jams"]


async def test_a_malformed_claim_does_not_stop_the_rest_of_the_batch():
    broken = due_message(id="m1")
    del broken["content"]
    api = FakeApi(due=[broken, due_message(id="m2")])
    rest = make_rest()

    assert await jams.post_due_messages(jams.JamApi(api), rest) == 1
    assert api.posts[-1][0] == "/jam-messages/m2/mark-sent"


async def test_a_post_discord_was_asked_for_twice_is_logged(caplog):
    api = FakeApi(due=[due_message()])
    rest = make_rest()
    rest.create_message.side_effect = _hikari_retries(2)

    with caplog.at_level(logging.WARNING, logger="smarter_dev.bot.jams"):
        await jams.post_due_messages(jams.JamApi(api), rest)

    [warning] = [r for r in caplog.records if r.name == "smarter_dev.bot.jams"]
    assert "jam message m1" in warning.getMessage()
    assert "asked again 2 time(s)" in warning.getMessage()
    assert "may be in the channel twice" in warning.getMessage()
    assert api.posts[-1][0] == "/jam-messages/m1/mark-sent"
    assert logging.getLogger("hikari.rest").handlers == []


async def test_a_retried_post_that_still_fails_is_logged_too(caplog):
    api = FakeApi(due=[due_message()])
    rest = make_rest()
    retried = _hikari_retries(3)

    async def create_message(*args, **kwargs):
        await retried()
        raise server_error()

    rest.create_message.side_effect = create_message

    with caplog.at_level(logging.WARNING, logger="smarter_dev.bot.jams"):
        await jams.post_due_messages(jams.JamApi(api), rest)

    assert any("asked again 3 time(s)" in r.getMessage() for r in caplog.records)
    assert api.posts[-1][0] == "/jam-messages/m1/mark-failed"


async def test_a_post_that_went_through_first_time_logs_no_retry(caplog):
    api = FakeApi(due=[due_message()])

    with caplog.at_level(logging.WARNING, logger="smarter_dev.bot.jams"):
        await jams.post_due_messages(jams.JamApi(api), make_rest())
        # Retries outside a jam post are nobody's duplicate.
        await _hikari_retries(1)()

    assert [r for r in caplog.records if r.name == "smarter_dev.bot.jams"] == []


async def test_a_lost_report_does_not_post_again_or_stop_the_rest():
    api = FakeApi(due=[due_message(id="m1"), due_message(id="m2")])
    api.fail_posts = {"m1/mark-sent"}
    rest = make_rest()

    assert await jams.post_due_messages(jams.JamApi(api), rest) == 2
    assert rest.create_message.await_count == 2
    assert [path for path, _ in api.posts[1:]] == ["/jam-messages/m2/mark-sent"]


# -- showcase tags -------------------------------------------------------------


async def test_a_new_showcase_post_gets_the_jam_and_year_tags():
    rest = make_rest()

    tagged = await jams.tag_showcase_post(
        jams.JamApi(FakeApi()), rest, showcase_post(applied_tag_ids=[40]), NOW
    )

    assert tagged is True
    rest.edit_channel.assert_awaited_once()
    assert rest.edit_channel.await_args.args == (700,)
    assert rest.edit_channel.await_args.kwargs["applied_tags"] == [31, 32, 40]


async def test_the_jam_tags_survive_the_five_tag_limit():
    rest = make_rest()

    await jams.tag_showcase_post(
        jams.JamApi(FakeApi()),
        rest,
        showcase_post(applied_tag_ids=[40, 41, 42, 43, 44]),
        NOW,
    )

    assert rest.edit_channel.await_args.kwargs["applied_tags"] == [31, 32, 40, 41, 42]


@pytest.mark.parametrize(
    "post",
    [
        showcase_post(parent_id=999),
        showcase_post(type=hikari.ChannelType.GUILD_PRIVATE_THREAD),
        showcase_post(created_at=NOW - timedelta(hours=1)),
        showcase_post(applied_tag_ids=[32, 31]),
    ],
    ids=["another forum", "private thread", "old post", "already tagged"],
)
async def test_other_threads_are_left_alone(post):
    rest = make_rest()

    assert not await jams.tag_showcase_post(jams.JamApi(FakeApi()), rest, post, NOW)
    rest.edit_channel.assert_not_awaited()


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 11, 30, 23, 59, tzinfo=UTC),
        datetime(2027, 1, 3, 0, 1, tzinfo=UTC),
    ],
    ids=["before the jam", "after submissions close"],
)
async def test_posts_outside_the_jam_are_not_entries(when):
    rest = make_rest()
    post = showcase_post(created_at=when)

    assert not await jams.tag_showcase_post(jams.JamApi(FakeApi()), rest, post, when)
    rest.edit_channel.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [guild_jam(jam=False), guild_jam(channels={"showcase": None})],
    ids=["no jam", "no showcase forum"],
)
async def test_nothing_is_tagged_without_a_jam_or_a_forum(body):
    rest = make_rest()

    assert not await jams.tag_showcase_post(
        jams.JamApi(FakeApi(body)), rest, showcase_post(), NOW
    )
    rest.edit_channel.assert_not_awaited()


async def test_only_the_tag_that_exists_is_applied():
    body = guild_jam()
    body["jam"]["year_tag_id"] = None
    rest = make_rest()

    await jams.tag_showcase_post(jams.JamApi(FakeApi(body)), rest, showcase_post(), NOW)

    assert rest.edit_channel.await_args.kwargs["applied_tags"] == [31]


async def test_a_refused_tag_edit_is_not_an_error():
    rest = make_rest()
    rest.edit_channel.side_effect = forbidden()

    assert not await jams.tag_showcase_post(
        jams.JamApi(FakeApi()), rest, showcase_post(), NOW
    )


# -- the Jammer role -----------------------------------------------------------


async def test_join_gives_the_role():
    rest = make_rest()

    reply = await jams.join_jam(jams.JamApi(FakeApi()), rest, GUILD, 1)

    rest.add_role_to_member.assert_awaited_once()
    assert rest.add_role_to_member.await_args.args == (GUILD, 1, ROLE)
    assert "Holiday Chatbot Jam" in reply
    assert "pinged" in reply
    assert "`/jam unsubscribe`" in reply


async def test_join_needs_a_jam_and_a_role():
    rest = make_rest()

    no_jam = await jams.join_jam(
        jams.JamApi(FakeApi(guild_jam(jam=False))), rest, GUILD, 1
    )
    no_role = await jams.join_jam(
        jams.JamApi(FakeApi(guild_jam(role_id=None))), rest, GUILD, 1
    )

    assert no_jam == jams.NO_JAM
    assert no_role == jams.NOT_SET_UP
    rest.add_role_to_member.assert_not_awaited()


async def test_join_says_so_when_discord_refuses():
    rest = make_rest()
    rest.add_role_to_member.side_effect = forbidden()

    reply = await jams.join_jam(jams.JamApi(FakeApi()), rest, GUILD, 1)

    assert "could not" in reply


async def test_leave_works_between_jams():
    rest = make_rest()

    reply = await jams.leave_jam(
        jams.JamApi(FakeApi(guild_jam(jam=False))), rest, GUILD, 1
    )

    assert rest.remove_role_from_member.await_args.args == (GUILD, 1, ROLE)
    assert "no longer" in reply


# -- teams ---------------------------------------------------------------------


async def make_team(fake=None, rest=None, limits=None, user=1, name="Elves", now=NOW):
    return await jams.create_team(
        jams.JamApi(fake or FakeApi()),
        rest or make_rest(),
        limits or jams.TeamLimits(),
        GUILD,
        user,
        name,
        bot_user_id=BOT_ID,
        now=now,
    )


async def test_team_makes_a_private_thread_members_cannot_invite_to():
    fake, rest = FakeApi(), make_rest()

    reply = await make_team(fake, rest, name="  Tinsel   Bots ")

    call = rest.create_thread.await_args
    assert call.args == (CHAT, hikari.ChannelType.GUILD_PRIVATE_THREAD, "Tinsel Bots")
    assert call.kwargs["invitable"] is False
    rest.add_thread_member.assert_awaited_once_with(THREAD, 1)
    welcome = rest.create_message.await_args
    assert welcome.args[0] == THREAD
    assert "up to 2 teammates" in welcome.args[1]
    assert "`/jam leave`" in welcome.args[1]
    assert reply == f"Your team thread is ready: <#{THREAD}>"


async def test_a_new_team_is_recorded_with_its_thread_and_maker():
    fake = FakeApi()

    await make_team(fake, name="Tinsel Bots")

    assert fake.posts == [
        (
            f"{JAM_API}/teams",
            {"name": "Tinsel Bots", "thread_id": str(THREAD), "user_id": "1"},
        )
    ]
    assert fake.teams[str(THREAD)]["member_ids"] == ["1"]


@pytest.mark.parametrize(
    ("body", "name", "expected"),
    [
        (guild_jam(jam=False), "Elves", jams.NO_JAM),
        (guild_jam(channels={"chat": None}), "Elves", jams.NOT_SET_UP),
        (guild_jam(), "   ", "1 to 80 characters"),
        (guild_jam(), "x" * 81, "1 to 80 characters"),
    ],
    ids=["no jam", "no chat channel", "blank name", "long name"],
)
async def test_team_refusals_make_no_thread(body, name, expected):
    rest = make_rest()

    reply = await make_team(FakeApi(body), rest, name=name)

    assert expected in reply
    rest.create_thread.assert_not_awaited()


async def test_no_team_before_the_jam_starts():
    fake, rest = FakeApi(), make_rest()
    start = datetime(2026, 12, 1, tzinfo=UTC)

    reply = await make_team(fake, rest, now=start - timedelta(seconds=1))

    assert "Teams open when **Holiday Chatbot Jam** starts" in reply
    assert f"<t:{int(start.timestamp())}:F>" in reply
    rest.create_thread.assert_not_awaited()
    assert fake.posts == []
    assert "ready" in await make_team(fake, rest, now=start)


async def test_a_solo_jam_has_no_teams():
    body = guild_jam()
    body["jam"]["max_team_size"] = 1
    rest = make_rest()

    reply = await make_team(FakeApi(body), rest)

    assert "solo jam" in reply
    rest.create_thread.assert_not_awaited()


async def test_a_member_on_a_team_cannot_make_another():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD + 1, 1)

    reply = await make_team(fake, rest)

    assert f"already on a team for this jam: <#{THREAD + 1}>" in reply
    assert "`/jam leave`" in reply
    rest.create_thread.assert_not_awaited()
    assert list(fake.teams) == [str(THREAD + 1)]


async def test_a_member_with_a_solo_entry_cannot_make_a_team():
    fake, rest = FakeApi(), make_rest()
    fake.entries["700"] = "solo:1"

    reply = await make_team(fake, rest)

    assert "already posted an entry on your own" in reply
    rest.create_thread.assert_not_awaited()


async def test_the_web_api_decides_when_teams_are_open():
    fake, rest = FakeApi(), make_rest()
    fake.refuse["members"] = "not_open"

    assert await make_team(fake, rest) == jams.NOT_OPEN
    rest.create_thread.assert_not_awaited()


async def test_team_says_so_when_discord_refuses():
    fake, rest = FakeApi(), make_rest()
    rest.create_thread.side_effect = forbidden()

    reply = await make_team(fake, rest)

    assert "could not make" in reply
    assert fake.teams == {}


async def test_team_is_still_made_if_the_welcome_fails():
    rest = make_rest()
    rest.create_message.side_effect = forbidden()

    reply = await make_team(rest=rest)

    assert f"<#{THREAD}>" in reply


async def test_a_thread_the_web_api_will_not_record_is_removed():
    """Two requests from one member can both pass the first check."""
    fake, rest, limits = FakeApi(), make_rest(), jams.TeamLimits()
    fake.refuse["teams"] = "on_a_team"

    reply = await make_team(fake, rest, limits)

    assert "already on a team" in reply
    rest.delete_channel.assert_awaited_once_with(THREAD)
    rest.add_thread_member.assert_not_awaited()
    rest.create_message.assert_not_awaited()
    assert limits.wait_seconds(GUILD, 1) == 0


async def test_a_thread_that_could_not_be_recorded_is_removed():
    fake, rest, limits = FakeApi(), make_rest(), jams.TeamLimits()
    fake.fail_posts.add("/jam/teams")

    reply = await make_team(fake, rest, limits)

    assert "could not make" in reply
    rest.delete_channel.assert_awaited_once_with(THREAD)
    rest.add_thread_member.assert_not_awaited()
    assert limits.wait_seconds(GUILD, 1) == 0


async def test_a_team_saved_by_a_call_that_failed_is_taken_back():
    """The web API can save the team and the reply still be lost."""
    fake, rest, limits = FakeApi(), make_rest(), jams.TeamLimits()
    fake.lose_replies.add("/jam/teams")

    reply = await make_team(fake, rest, limits)

    assert "could not make" in reply
    assert fake.teams == {}
    rest.delete_channel.assert_awaited_once_with(THREAD)
    assert limits.wait_seconds(GUILD, 1) == 0


async def test_a_thread_its_maker_could_not_join_is_removed():
    fake, rest, limits = FakeApi(), make_rest(), jams.TeamLimits()
    rest.add_thread_member.side_effect = forbidden()

    reply = await make_team(fake, rest, limits)

    assert "could not make" in reply
    rest.delete_channel.assert_awaited_once_with(THREAD)
    rest.create_message.assert_not_awaited()
    assert limits.wait_seconds(GUILD, 1) == 0
    # The record goes too, so the member is not left on a team with no thread.
    assert fake.posts[-1] == (f"{JAM_API}/teams/{THREAD}/leave", {"user_id": "1"})
    assert fake.teams == {}

    rest.delete_channel.side_effect = forbidden()
    assert "could not make" in await make_team(fake, rest, limits)


async def invite(rest, fake=None, **overrides) -> str:
    if fake is None:
        fake = FakeApi()
        fake.add_team(THREAD, 1)
    values = {
        "guild_id": GUILD,
        "channel_id": THREAD,
        "user_id": 1,
        "target_id": 2,
        "target_is_bot": False,
    }
    values.update(overrides)
    return await jams.invite_to_team(jams.JamApi(fake), rest, **values)


async def test_invite_adds_a_teammate():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1)

    reply = await invite(rest, fake)

    assert fake.posts == [
        (f"{JAM_API}/teams/{THREAD}/members", {"user_id": "2", "invited_by": "1"})
    ]
    rest.add_thread_member.assert_awaited_once_with(THREAD, 2)
    assert fake.teams[str(THREAD)]["member_ids"] == ["1", "2"]
    assert reply == "<@2> is now on the team."


async def test_invite_stops_at_the_team_size():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1, 3)

    assert "now on the team" in await invite(rest, fake, target_id=4)
    reply = await invite(rest, fake, target_id=5)

    assert "full" in reply and "3 people" in reply
    rest.add_thread_member.assert_awaited_once_with(THREAD, 4)


async def test_invite_refuses_a_bot_without_asking():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1)

    assert "bot cannot" in await invite(rest, fake, target_id=9, target_is_bot=True)
    assert fake.posts == []
    rest.add_thread_member.assert_not_awaited()


async def test_inviting_a_teammate_again_lets_them_back_into_the_thread():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1, 2)

    reply = await invite(rest, fake)

    assert reply == "<@2> is already on this team."
    rest.add_thread_member.assert_awaited_once_with(THREAD, 2)
    assert fake.teams[str(THREAD)]["member_ids"] == ["1", "2"]


async def test_invite_only_works_in_a_team_thread():
    rest = make_rest()

    reply = await invite(rest, FakeApi())

    assert "inside your team's thread" in reply
    rest.add_thread_member.assert_not_awaited()


async def test_only_a_team_member_can_invite():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1)

    reply = await invite(rest, fake, user_id=7)

    assert reply == "Only this team's members can add people to it."
    rest.add_thread_member.assert_not_awaited()


async def test_invite_refuses_someone_on_another_team():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1)
    fake.add_team(THREAD + 1, 2)

    reply = await invite(rest, fake)

    assert reply == "<@2> is already on another team for this jam."
    rest.add_thread_member.assert_not_awaited()


async def test_invite_refuses_someone_with_a_solo_entry():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1)
    fake.entries["700"] = "solo:2"

    reply = await invite(rest, fake)

    assert reply == "<@2> has already posted an entry on their own for this jam."
    rest.add_thread_member.assert_not_awaited()


async def test_a_team_with_an_entry_takes_no_new_members():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1)
    fake.entries["700"] = str(THREAD)

    assert await invite(rest, fake) == jams.TEAM_HAS_ENTRY
    rest.add_thread_member.assert_not_awaited()


async def test_invite_needs_an_open_jam():
    rest = make_rest()

    assert await invite(rest, FakeApi(guild_jam(jam=False))) == jams.NO_JAM
    rest.add_thread_member.assert_not_awaited()


async def test_a_teammate_discord_would_not_add_is_taken_off_the_roster():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1)
    rest.add_thread_member.side_effect = not_found()

    assert "could not add" in await invite(rest, fake)
    assert fake.posts[-1] == (f"{JAM_API}/teams/{THREAD}/leave", {"user_id": "2"})
    assert fake.teams[str(THREAD)]["member_ids"] == ["1"]


async def test_a_failed_re_add_leaves_the_roster_alone():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1, 2)
    rest.add_thread_member.side_effect = not_found()

    assert "could not add" in await invite(rest, fake)
    assert fake.teams[str(THREAD)]["member_ids"] == ["1", "2"]


async def leave(rest, fake, user=1) -> str:
    return await jams.leave_team(jams.JamApi(fake), rest, GUILD, user)


async def test_leave_takes_the_member_off_the_team_and_out_of_the_thread():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1, 2, name="Tinsel Bots")

    reply = await leave(rest, fake, user=2)

    assert fake.teams[str(THREAD)]["member_ids"] == ["1"]
    rest.remove_thread_member.assert_awaited_once_with(THREAD, 2)
    notice = rest.create_message.await_args
    assert notice.args == (THREAD, "<@2> left the team.")
    assert notice.kwargs["user_mentions"] is False
    rest.edit_channel.assert_not_awaited()
    assert reply == "You left **Tinsel Bots**. You can make or join another team."


async def test_a_team_its_last_member_leaves_is_closed():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1)

    reply = await leave(rest, fake)

    assert fake.teams == {}
    edit = rest.edit_channel.await_args
    assert edit.args == (THREAD,)
    assert edit.kwargs["archived"] is True and edit.kwargs["locked"] is True
    rest.create_message.assert_not_awaited()
    assert "You left **Elves**" in reply


async def test_leave_needs_a_team():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 2)

    assert await leave(rest, fake) == jams.NOT_ON_A_TEAM
    assert await leave(rest, FakeApi(guild_jam(jam=False))) == jams.NO_JAM
    assert rest.mock_calls == []


async def test_nobody_leaves_a_team_that_has_an_entry():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1, 2)
    fake.entries["700"] = str(THREAD)

    assert await leave(rest, fake) == jams.TEAM_HAS_ENTRY
    assert fake.teams[str(THREAD)]["member_ids"] == ["1", "2"]
    assert rest.mock_calls == []


async def test_leave_counts_even_if_discord_keeps_them_in_the_thread():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1, 2)
    rest.remove_thread_member.side_effect = forbidden()
    rest.create_message.side_effect = forbidden()

    assert "You left" in await leave(rest, fake)
    assert fake.teams[str(THREAD)]["member_ids"] == ["2"]


@pytest.mark.parametrize(
    "command", ["subscribe", "unsubscribe", "team", "invite", "leave"]
)
async def test_an_api_outage_is_not_called_a_missing_setup(command):
    fake = FakeApi()
    fake.fail_get = True
    api, rest, limits = jams.JamApi(fake), make_rest(), jams.TeamLimits()

    reply = await {
        "subscribe": lambda: jams.join_jam(api, rest, GUILD, 1),
        "unsubscribe": lambda: jams.leave_jam(api, rest, GUILD, 1),
        "team": lambda: jams.create_team(
            api, rest, limits, GUILD, 1, "Elves", bot_user_id=BOT_ID, now=NOW
        ),
        "invite": lambda: jams.invite_to_team(
            api,
            rest,
            guild_id=GUILD,
            channel_id=THREAD,
            user_id=1,
            target_id=2,
            target_is_bot=False,
        ),
        "leave": lambda: jams.leave_team(api, rest, GUILD, 1),
    }[command]()

    assert reply == jams.UNAVAILABLE
    assert reply != jams.NOT_SET_UP
    assert rest.mock_calls == []


# -- team limits ---------------------------------------------------------------


def threads_in_turn(rest) -> None:
    """Give each new team thread its own id."""
    ids = iter(range(THREAD, THREAD + 50))
    rest.create_thread.side_effect = lambda *a, **k: SimpleNamespace(id=next(ids))


async def test_a_member_makes_one_team_per_cooldown():
    now = [0.0]
    limits = jams.TeamLimits(clock=lambda: now[0])
    fake, rest = FakeApi(), make_rest()
    threads_in_turn(rest)

    first = await make_team(fake, rest, limits, name="Elves")
    assert "You left" in await leave(rest, fake)
    now[0] = jams.TEAM_COOLDOWN_SECONDS - 61
    second = await make_team(fake, rest, limits, name="More Elves")
    someone_else = await make_team(fake, rest, limits, user=2, name="Reindeer")

    assert "ready" in first and "ready" in someone_else
    assert "another in 2 minutes" in second
    assert rest.create_thread.await_count == 2

    now[0] = jams.TEAM_COOLDOWN_SECONDS
    assert "ready" in await make_team(fake, rest, limits, name="Elves 2")


async def test_a_team_that_was_not_made_does_not_start_the_cooldown():
    limits = jams.TeamLimits()
    fake, rest = FakeApi(), make_rest()
    rest.create_thread.side_effect = forbidden()

    assert "could not make" in await make_team(fake, rest, limits)
    rest.create_thread.side_effect = None

    assert "ready" in await make_team(fake, rest, limits)


async def test_two_teams_at_once_from_one_member_make_one_thread():
    limits = jams.TeamLimits()
    fake, rest = FakeApi(), make_rest()

    async def slow_create(*args, **kwargs):
        await asyncio.sleep(0)
        return SimpleNamespace(id=THREAD)

    rest.create_thread.side_effect = slow_create

    replies = await asyncio.gather(
        make_team(fake, rest, limits), make_team(fake, rest, limits)
    )

    assert rest.create_thread.await_count == 1
    assert sum("ready" in reply for reply in replies) == 1


async def test_a_guild_has_a_cap_on_open_team_threads():
    fake, rest, limits = FakeApi(), make_rest(), jams.TeamLimits()
    elsewhere = [team_thread(parent_id=SHOWCASE) for _ in range(500)]
    teams = [team_thread() for _ in range(jams.TEAM_THREADS_MAX)]
    rest.fetch_active_threads.return_value = [*elsewhere, *teams[1:]]

    assert "ready" in await make_team(fake, rest, limits, name="Last")
    rest.fetch_active_threads.assert_awaited_with(GUILD)

    rest.fetch_active_threads.return_value = [*elsewhere, *teams]
    reply = await make_team(fake, rest, limits, user=2, name="One too many")

    assert "already 100 team threads open" in reply
    assert rest.create_thread.await_count == 1


async def test_threads_members_open_by_hand_do_not_use_up_the_cap():
    rest = make_rest()
    by_hand = [
        team_thread(owner_id=7),
        team_thread(type=hikari.ChannelType.GUILD_PUBLIC_THREAD, owner_id=7),
        team_thread(type=hikari.ChannelType.GUILD_PUBLIC_THREAD),
    ]
    rest.fetch_active_threads.return_value = by_hand * jams.TEAM_THREADS_MAX

    reply = await make_team(rest=rest)

    assert "ready" in reply
    rest.create_thread.assert_awaited_once()


async def test_no_team_is_made_when_the_open_threads_cannot_be_counted():
    rest = make_rest()
    rest.fetch_active_threads.side_effect = server_error()

    reply = await make_team(rest=rest)

    assert "could not make" in reply
    rest.create_thread.assert_not_awaited()


# -- showcase entries ----------------------------------------------------------


async def record(fake, rest, **post) -> bool:
    return await jams.record_showcase_post(
        jams.JamApi(fake), rest, showcase_post(**post), NOW
    )


async def test_a_team_members_post_is_the_teams_entry():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1, 2)

    assert await record(fake, rest, owner_id=2) is True

    assert fake.posts == [
        (
            f"{JAM_API}/submissions",
            {
                "thread_id": "700",
                "author_id": "2",
                "posted_at": (NOW - timedelta(seconds=2)).isoformat(),
            },
        )
    ]
    assert fake.entries == {"700": str(THREAD)}
    notice = rest.create_message.await_args
    assert notice.args[0] == THREAD
    assert "Your team's entry is in: <#700>" in notice.args[1]
    assert notice.kwargs["user_mentions"] is False


async def test_a_post_from_someone_with_no_team_is_a_solo_entry():
    fake, rest = FakeApi(), make_rest()

    assert await record(fake, rest) is True

    assert fake.entries == {"700": "solo:1"}
    assert rest.mock_calls == []


async def test_a_second_post_from_a_team_stays_up_and_the_team_is_told():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1, 2)
    await record(fake, rest, owner_id=1)
    rest.reset_mock()

    assert await record(fake, rest, id=701, owner_id=2) is True

    assert fake.entries == {"700": str(THREAD), "701": str(THREAD)}
    notice = rest.create_message.await_args
    assert notice.args[0] == THREAD
    assert "<@2>, your post <#701> is up" in notice.args[1]
    assert "your team already has an entry" in notice.args[1]
    assert "<#700>" in notice.args[1]
    assert notice.kwargs["user_mentions"] == [2]
    assert notice.kwargs["mentions_everyone"] is False
    rest.delete_channel.assert_not_awaited()
    rest.create_dm_channel.assert_not_awaited()


async def test_a_second_solo_post_is_answered_by_dm():
    fake, rest = FakeApi(), make_rest()
    await record(fake, rest)

    assert await record(fake, rest, id=701) is True

    rest.create_dm_channel.assert_awaited_once_with(1)
    notice = rest.create_message.await_args
    assert notice.args[0] == DM
    assert "you already have an entry" in notice.args[1]
    assert "<#700>" in notice.args[1]
    rest.delete_channel.assert_not_awaited()


async def test_a_closed_dm_is_not_an_error():
    closed, refused = make_rest(), make_rest()
    closed.create_dm_channel.side_effect = forbidden()
    refused.create_message.side_effect = forbidden()

    for rest in (closed, refused):
        fake = FakeApi()
        fake.entries["600"] = "solo:1"
        assert await record(fake, rest) is True
        assert "700" in fake.entries


async def test_a_post_seen_twice_is_announced_once():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1)
    await record(fake, rest)

    assert await record(fake, rest) is False

    rest.create_message.assert_awaited_once()


@pytest.mark.parametrize(
    "post",
    [
        {"parent_id": CHAT},
        {"type": hikari.ChannelType.GUILD_PRIVATE_THREAD},
        {"created_at": NOW - jams.NEW_POST_WINDOW - timedelta(seconds=1)},
    ],
    ids=["another channel", "a private thread", "an old post"],
)
async def test_only_a_new_showcase_post_is_recorded(post):
    fake, rest = FakeApi(), make_rest()

    assert await record(fake, rest, **post) is False
    assert fake.posts == []


async def test_nothing_is_recorded_between_jams():
    fake, rest = FakeApi(guild_jam(jam=False)), make_rest()

    assert await record(fake, rest) is False
    assert fake.posts == []


async def test_a_deleted_showcase_post_is_forgotten():
    fake = FakeApi()
    fake.entries["700"] = "solo:1"
    api = jams.JamApi(fake)

    assert await jams.forget_showcase_post(api, GUILD, CHAT, 700) is False
    assert fake.posts == []
    assert await jams.forget_showcase_post(api, GUILD, SHOWCASE, 700) is True

    assert fake.posts == [(f"{JAM_API}/submissions/700/remove", {})]
    assert fake.entries == {}


# -- after the jam starts ------------------------------------------------------

CLOSE = datetime(2027, 1, 3, tzinfo=UTC)
VOTING_ENDS = datetime(2027, 1, 10, tzinfo=UTC)


def jam_work(**overrides) -> dict:
    """One jam as ``/jam-work`` lists it while it is open."""
    work = {
        "jam_id": "j1",
        "guild_id": str(GUILD),
        "name": "Holiday Chatbot Jam",
        "showcase_channel_id": str(SHOWCASE),
        "tag_ids": ["31", "32"],
        "starts_at": "2026-12-01T00:00:00+00:00",
        "submissions_close_at": CLOSE.isoformat(),
        "voting_ends_at": VOTING_ENDS.isoformat(),
        "known_posts": [],
        "find_missed_posts": True,
        "teams_to_close": None,
        "count_votes": False,
    }
    work.update(overrides)
    return work


def closing_work(*teams: tuple[str, int, str | None], **overrides) -> dict:
    return jam_work(
        teams_to_close=[
            {"name": name, "thread_id": str(thread), "entry_thread_id": entry}
            for name, thread, entry in teams
        ],
        **overrides,
    )


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def run_work(fake, rest, pacer=None, **kwargs) -> None:
    await jams.run_jam_work(
        jams.JamApi(fake), rest, pacer or jams.WorkPacer(), **kwargs
    )


def submissions(fake) -> list[dict]:
    return [body for path, body in fake.posts if path == f"{JAM_API}/submissions"]


def work_reports(fake) -> list[tuple[str, dict | None]]:
    return [post for post in fake.posts if post[0].startswith("/jam-work/")]


def notices(rest) -> dict[int, str]:
    return {call.args[0]: call.args[1] for call in rest.create_message.await_args_list}


async def test_a_missed_post_is_tagged_recorded_and_announced():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(THREAD, 1, 2)
    fake.work = [jam_work(known_posts=["700"])]
    made = NOW - timedelta(days=3)
    rest.fetch_active_threads.return_value = [
        showcase_post(id=700),
        showcase_post(id=701, owner_id=2, created_at=made),
    ]

    await run_work(fake, rest)

    rest.fetch_active_threads.assert_awaited_once_with(GUILD)
    rest.fetch_public_archived_threads.assert_called_once_with(SHOWCASE)
    assert submissions(fake) == [
        {"thread_id": "701", "author_id": "2", "posted_at": made.isoformat()}
    ]
    rest.edit_channel.assert_awaited_once()
    assert rest.edit_channel.await_args.args == (701,)
    assert rest.edit_channel.await_args.kwargs["applied_tags"] == [31, 32]
    assert "Your team's entry is in: <#701>" in notices(rest)[THREAD]
    assert work_reports(fake) == []


@pytest.mark.parametrize(
    "post",
    [
        {"parent_id": CHAT},
        {"type": hikari.ChannelType.GUILD_PRIVATE_THREAD},
        {"created_at": datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC)},
        {"created_at": CLOSE + timedelta(seconds=1)},
    ],
    ids=["another channel", "private thread", "before the jam", "after the close"],
)
async def test_only_posts_made_in_the_showcase_during_the_jam_are_picked_up(post):
    fake, rest = FakeApi(), make_rest()
    fake.work = [jam_work()]
    rest.fetch_active_threads.return_value = [showcase_post(id=701, **post)]

    await run_work(fake, rest)

    assert fake.posts == []
    rest.edit_channel.assert_not_awaited()


async def test_posts_made_at_the_first_and_last_moment_are_picked_up():
    fake, rest = FakeApi(), make_rest()
    fake.work = [jam_work()]
    rest.fetch_active_threads.return_value = [
        showcase_post(id=702, created_at=CLOSE),
        showcase_post(id=701, created_at=datetime(2026, 12, 1, tzinfo=UTC)),
    ]

    await run_work(fake, rest)

    # Oldest first, so the earlier post is the one that counts.
    assert [body["thread_id"] for body in submissions(fake)] == ["701", "702"]


async def test_an_archived_missed_post_is_recorded_but_not_reopened_to_tag_it():
    fake, rest = FakeApi(), make_rest()
    fake.work = [jam_work()]
    before_jam = datetime(2026, 11, 20, tzinfo=UTC)
    archived = [
        showcase_post(id=702, is_archived=True, archive_timestamp=NOW),
        showcase_post(
            id=650, is_archived=True, archive_timestamp=before_jam, created_at=NOW
        ),
        # Never reached: the list is newest first and 650 is from before the jam.
        showcase_post(id=703, is_archived=True, archive_timestamp=NOW),
    ]
    rest.fetch_public_archived_threads = Mock(
        side_effect=lambda channel: lazy(archived)
    )

    await run_work(fake, rest)

    assert [body["thread_id"] for body in submissions(fake)] == ["702"]
    rest.edit_channel.assert_not_awaited()


async def test_missed_posts_are_looked_for_once_per_wait():
    fake, rest, clock = FakeApi(), make_rest(), Clock()
    fake.work = [jam_work()]
    pacer = jams.WorkPacer(clock=clock)

    await run_work(fake, rest, pacer)
    clock.now += jams.WORK_EVERY_SECONDS - 1
    await run_work(fake, rest, pacer)

    assert rest.fetch_active_threads.await_count == 1

    clock.now += 1
    await run_work(fake, rest, pacer)

    assert rest.fetch_active_threads.await_count == 2


async def test_a_forum_that_cannot_be_read_is_tried_again_later(caplog):
    fake, rest, clock = FakeApi(), make_rest(), Clock()
    fake.work = [closing_work(("Elves", THREAD, None))]
    rest.fetch_active_threads.side_effect = server_error()
    pacer = jams.WorkPacer(clock=clock)

    with caplog.at_level(logging.WARNING, logger="smarter_dev.bot.jams"):
        await run_work(fake, rest, pacer)

    # A team is not told it has no entry while the entries cannot be checked.
    assert "could not read the showcase" in caplog.text
    rest.edit_channel.assert_not_awaited()
    assert fake.posts == []

    rest.fetch_active_threads.side_effect = None
    rest.fetch_channel = AsyncMock(return_value=team_thread(is_locked=False))
    clock.now += jams.WORK_EVERY_SECONDS
    await run_work(fake, rest, pacer)

    assert work_reports(fake) == [("/jam-work/j1/teams-closed", None)]


async def test_no_showcase_means_no_posts_to_look_for():
    fake, rest = FakeApi(), make_rest()
    fake.work = [jam_work(showcase_channel_id=None)]

    await run_work(fake, rest)

    rest.fetch_active_threads.assert_not_awaited()


async def test_closing_locks_each_team_thread_and_tells_the_team():
    fake, rest = FakeApi(), make_rest()
    fake.work = [closing_work(("Elves", 500, "700"), ("Reindeer", 501, None))]
    rest.fetch_channel = AsyncMock(return_value=team_thread(is_locked=False))
    order = Mock()
    order.attach_mock(rest.edit_channel, "lock")
    order.attach_mock(rest.create_message, "tell")

    await run_work(fake, rest)

    assert [call[0] for call in order.mock_calls] == ["lock", "tell", "lock", "tell"]
    for call, thread in zip(rest.edit_channel.await_args_list, (500, 501), strict=True):
        assert call.args == (thread,)
        assert call.kwargs["locked"] is True and call.kwargs["archived"] is False
    told = notices(rest)
    assert "Submissions for **Holiday Chatbot Jam** are closed" in told[500]
    assert "this thread is now locked" in told[500]
    assert "Your team's entry: <#700>" in told[500]
    assert "Your team did not post an entry." in told[501]
    assert f"Voting is open until <t:{int(VOTING_ENDS.timestamp())}:F>" in told[501]
    assert f"<#{SHOWCASE}>" in told[501]
    for call in rest.create_message.await_args_list:
        assert call.kwargs["user_mentions"] is False
        assert call.kwargs["mentions_everyone"] is False
    assert work_reports(fake) == [("/jam-work/j1/teams-closed", None)]


async def test_the_closing_notice_leaves_out_voting_that_is_not_coming():
    fake, rest = FakeApi(), make_rest()
    fake.work = [
        closing_work(("Elves", 500, None), voting_ends_at=None),
        closing_work(
            ("Old", 501, None),
            jam_id="j0",
            voting_ends_at="2020-01-10T00:00:00+00:00",
        ),
    ]
    rest.fetch_channel = AsyncMock(return_value=team_thread(is_locked=False))

    await run_work(fake, rest)

    told = notices(rest)
    assert "Voting" not in told[500] and "Voting" not in told[501]


async def test_a_team_already_locked_or_gone_is_not_told_again():
    fake, rest = FakeApi(), make_rest()
    fake.work = [closing_work(("Elves", 500, None), ("Gone", 501, None))]
    rest.fetch_channel = AsyncMock(
        side_effect=[team_thread(is_locked=True), not_found()]
    )

    await run_work(fake, rest)

    rest.edit_channel.assert_not_awaited()
    rest.create_message.assert_not_awaited()
    assert work_reports(fake) == [("/jam-work/j1/teams-closed", None)]


async def test_a_thread_deleted_as_it_is_locked_counts_as_done():
    fake, rest = FakeApi(), make_rest()
    fake.work = [closing_work(("Elves", 500, None))]
    rest.fetch_channel = AsyncMock(return_value=team_thread(is_locked=False))
    rest.edit_channel.side_effect = not_found()

    await run_work(fake, rest)

    rest.create_message.assert_not_awaited()
    assert work_reports(fake) == [("/jam-work/j1/teams-closed", None)]


@pytest.mark.parametrize("step", ["fetch_channel", "edit_channel"])
async def test_a_team_that_cannot_be_locked_keeps_the_closing_open(step, caplog):
    fake, rest, clock = FakeApi(), make_rest(), Clock()
    fake.work = [closing_work(("Elves", 500, None), ("Reindeer", 501, None))]
    rest.fetch_channel = AsyncMock(return_value=team_thread(is_locked=False))
    getattr(rest, step).side_effect = [forbidden(), team_thread(is_locked=False)]
    pacer = jams.WorkPacer(clock=clock)

    with caplog.at_level(logging.WARNING, logger="smarter_dev.bot.jams"):
        await run_work(fake, rest, pacer)

    # The other team is still closed; the closing is not reported as done.
    assert "team thread 500 (Elves)" in caplog.text
    assert list(notices(rest)) == [501]
    assert work_reports(fake) == []

    await run_work(fake, rest, pacer)

    assert rest.fetch_channel.await_count == 2


async def test_a_notice_that_cannot_be_posted_does_not_hold_up_the_closing():
    fake, rest = FakeApi(), make_rest()
    fake.work = [closing_work(("Elves", 500, None))]
    rest.fetch_channel = AsyncMock(return_value=team_thread(is_locked=False))
    rest.create_message.side_effect = forbidden()

    await run_work(fake, rest)

    rest.edit_channel.assert_awaited_once()
    assert work_reports(fake) == [("/jam-work/j1/teams-closed", None)]


async def test_a_post_found_at_the_close_is_named_as_the_teams_entry():
    fake, rest = FakeApi(), make_rest()
    fake.add_team(500, 1)
    fake.work = [closing_work(("Elves", 500, None))]
    rest.fetch_active_threads.return_value = [
        showcase_post(id=700, owner_id=1, created_at=CLOSE - timedelta(minutes=1))
    ]
    rest.fetch_channel = AsyncMock(return_value=team_thread(is_locked=False))
    api = jams.JamApi(fake)
    asked = api.jam_work

    async def ask_again():
        # The web API now knows the post, as it would after recording it.
        if "700" in fake.entries:
            fake.work = [closing_work(("Elves", 500, "700"), known_posts=["700"])]
        return await asked()

    api.jam_work = ask_again

    await jams.run_jam_work(api, rest, jams.WorkPacer())

    assert fake.gets.count("/jam-work") == 2
    closing = rest.create_message.await_args_list[-1]
    assert closing.args[0] == 500
    assert "Your team's entry: <#700>" in closing.args[1]
    assert "did not post" not in closing.args[1]
    assert work_reports(fake) == [("/jam-work/j1/teams-closed", None)]


async def test_closing_stops_if_the_jam_is_no_longer_closing_after_a_found_post():
    fake, rest = FakeApi(), make_rest()
    fake.work = [closing_work(("Elves", 500, None))]
    rest.fetch_active_threads.return_value = [
        showcase_post(id=700, owner_id=9, created_at=CLOSE - timedelta(minutes=1))
    ]
    api = jams.JamApi(fake)
    asked = api.jam_work

    async def ask_again():
        if fake.entries:
            fake.work = []
        return await asked()

    api.jam_work = ask_again

    await jams.run_jam_work(api, rest, jams.WorkPacer())

    rest.edit_channel.assert_awaited_once()  # the tag on the found post
    assert work_reports(fake) == []


def reaction(emoji: str, count: int) -> SimpleNamespace:
    return SimpleNamespace(emoji=emoji, count=count)


def voted_rest(posts: dict[int, dict[str, list[int]]], extra: dict | None = None):
    """A server where each post's first message has these reactors per emoji.

    ``extra`` adds to an emoji's shown count without a listed reactor, the
    way a super reaction does.
    """
    rest = make_rest()
    extra = extra or {}

    async def fetch_message(channel, message):
        assert channel == message
        if isinstance(posts.get(channel), Exception):
            raise posts[channel]
        return SimpleNamespace(
            reactions=[
                reaction(emoji, len(users) + extra.get((channel, emoji), 0))
                for emoji, users in posts[channel].items()
            ]
        )

    rest.fetch_message = AsyncMock(side_effect=fetch_message)
    rest.fetch_reactions_for_emoji = Mock(
        side_effect=lambda channel, message, emoji: lazy(
            SimpleNamespace(id=user) for user in posts[channel][emoji]
        )
    )
    return rest


def counting_work(*posts: str, **overrides) -> dict:
    return jam_work(
        known_posts=list(posts), find_missed_posts=False, count_votes=True, **overrides
    )


async def test_votes_are_every_reaction_and_the_people_behind_them():
    fake = FakeApi()
    fake.work = [counting_work("700", "701", "702")]
    rest = voted_rest(
        {
            700: {"🎄": [1, 2, 3], "⭐": [1, 2], "🔥": [4]},
            701: {"🎄": [5]},
            702: {},
        },
        extra={(701, "🎄"): 2},
    )

    await run_work(fake, rest)

    assert work_reports(fake) == [
        (
            "/jam-work/j1/votes",
            {
                "counts": [
                    {"thread_id": "700", "reactions": 6, "reactors": 4},
                    {"thread_id": "701", "reactions": 3, "reactors": 1},
                    {"thread_id": "702", "reactions": 0, "reactors": 0},
                ],
                "gone": [],
            },
        )
    ]
    rest.fetch_active_threads.assert_not_awaited()


async def test_a_deleted_post_is_reported_gone_and_a_deleted_first_message_is_zero():
    fake = FakeApi()
    fake.work = [counting_work("700", "701", "702")]
    rest = voted_rest({700: not_found(), 701: not_found(), 702: {"🎄": [1]}})
    rest.fetch_channel = AsyncMock(side_effect=[not_found(), showcase_post(id=701)])

    await run_work(fake, rest)

    [(_, body)] = work_reports(fake)
    assert body == {
        "counts": [
            {"thread_id": "701", "reactions": 0, "reactors": 0},
            {"thread_id": "702", "reactions": 1, "reactors": 1},
        ],
        "gone": ["700"],
    }


async def test_a_count_that_cannot_be_finished_saves_nothing(caplog):
    fake, clock = FakeApi(), Clock()
    fake.work = [counting_work("700", "701")]
    posts = {700: {"🎄": [1]}, 701: server_error()}
    rest = voted_rest(posts)
    pacer = jams.WorkPacer(clock=clock)

    with caplog.at_level(logging.WARNING, logger="smarter_dev.bot.jams"):
        await run_work(fake, rest, pacer)

    assert work_reports(fake) == []
    assert "jam work failed for j1" in caplog.text

    posts[701] = {"🎄": [1, 2]}
    await run_work(fake, rest, pacer)
    assert work_reports(fake) == []

    clock.now += jams.WORK_EVERY_SECONDS
    await run_work(fake, rest, pacer)

    [(_, body)] = work_reports(fake)
    assert [count["reactions"] for count in body["counts"]] == [1, 2]


async def test_a_refused_count_is_logged(caplog):
    fake = FakeApi()
    fake.work = [counting_work("700")]
    fake.work_reply = {"ok": False, "reason": "too_early"}

    with caplog.at_level(logging.WARNING, logger="smarter_dev.bot.jams"):
        await run_work(fake, voted_rest({700: {}}))

    assert "votes for jam j1 were refused: too_early" in caplog.text


async def test_one_jams_failure_does_not_stop_the_next(caplog):
    fake, rest = FakeApi(), make_rest()
    fake.work = [
        closing_work(("Elves", 500, None), jam_id="j0"),
        jam_work(jam_id="j1"),
    ]
    fake.fail_posts = {"/j0/teams-closed"}
    rest.fetch_channel = AsyncMock(return_value=team_thread(is_locked=False))
    rest.fetch_active_threads.side_effect = [[], [showcase_post(id=701)]]

    with caplog.at_level(logging.WARNING, logger="smarter_dev.bot.jams"):
        await run_work(fake, rest)

    assert "jam work failed for j0" in caplog.text
    assert [body["thread_id"] for body in submissions(fake)] == ["701"]


async def test_each_post_and_team_is_finished_even_through_a_shutdown():
    fake, rest = FakeApi(), make_rest()
    fake.work = [closing_work(("Elves", 500, None))]
    rest.fetch_active_threads.return_value = [
        showcase_post(id=700, created_at=CLOSE - timedelta(minutes=1))
    ]
    rest.fetch_channel = AsyncMock(return_value=team_thread(is_locked=False))
    shielded = []

    async def shield(work):
        shielded.append(work.__name__)
        return await work

    await run_work(fake, rest, shield=shield)

    assert shielded == ["_record_post", "_close_team"]


def test_work_from_the_web_api_is_read_with_its_times_and_ids():
    work = jams._parse_work(
        closing_work(("Elves", 500, "700"), known_posts=["700"], voting_ends_at=None)
    )

    assert (work.guild_id, work.showcase_channel_id) == (GUILD, SHOWCASE)
    assert work.submissions_close_at == CLOSE and work.voting_ends_at is None
    assert work.known_posts == frozenset({"700"})
    assert work.teams_to_close == (("Elves", 500, "700"),)
    assert jams._parse_work(jam_work()).teams_to_close is None


# -- the plugin ----------------------------------------------------------------


@pytest.fixture
def wired(monkeypatch):
    """The plugin attached to a fake bot with a fake API."""
    api = FakeApi()
    rest = make_rest()
    bot = SimpleNamespace(
        d={"api_client": api}, rest=rest, get_me=lambda: SimpleNamespace(id=BOT_ID)
    )
    monkeypatch.setattr(jams_plugin.plugin, "_app", bot)
    monkeypatch.setattr(jams_plugin, "_api", None)
    monkeypatch.setattr(jams_plugin, "_limits", jams.TeamLimits())
    monkeypatch.setattr(jams_plugin, "_pacer", jams.WorkPacer())
    return SimpleNamespace(api=api, rest=rest, bot=bot)


def make_ctx(wired, **options) -> SimpleNamespace:
    return SimpleNamespace(
        respond=AsyncMock(),
        guild_id=GUILD,
        channel_id=THREAD,
        user=SimpleNamespace(id=1),
        bot=wired.bot,
        options=SimpleNamespace(**options),
    )


def test_load_plugins_loads_the_jams_plugin():
    bot = Mock()
    bot.d = {}
    client.load_plugins(bot)
    loaded = [call.args[0] for call in bot.load_extensions.call_args_list]
    assert "smarter_dev.bot.plugins.jams" in loaded


def test_the_jam_command_has_its_five_subcommands():
    group = jams_plugin.jam_group
    assert group.name == "jam"
    assert {child.name for child in group.subcommands} == {
        "subscribe",
        "unsubscribe",
        "team",
        "invite",
        "leave",
    }


async def test_a_command_defers_then_answers_only_the_caller(wired):
    ctx = make_ctx(wired)

    await jams_plugin.subscribe_command.callback(ctx)

    first, second = ctx.respond.await_args_list
    assert first.args == (hikari.ResponseType.DEFERRED_MESSAGE_CREATE,)
    assert first.kwargs == {"flags": hikari.MessageFlag.EPHEMERAL}
    assert "Holiday Chatbot Jam" in second.args[0]
    wired.rest.add_role_to_member.assert_awaited_once()


async def test_a_command_in_a_dm_is_refused(wired):
    ctx = make_ctx(wired)
    ctx.guild_id = None

    await jams_plugin.team_command.callback(ctx)

    ctx.respond.assert_awaited_once()
    assert ctx.respond.await_args.kwargs == {"flags": hikari.MessageFlag.EPHEMERAL}
    wired.rest.create_thread.assert_not_awaited()


async def test_a_command_that_breaks_still_answers(wired):
    ctx = make_ctx(wired)
    wired.rest.remove_role_from_member.side_effect = RuntimeError("boom")

    await jams_plugin.unsubscribe_command.callback(ctx)

    assert "went wrong" in ctx.respond.await_args.args[0]


async def test_team_invite_and_leave_pass_who_and_where(wired):
    wired.api.body["jam"]["starts_at"] = "2000-01-01T00:00:00+00:00"

    await jams_plugin.team_command.callback(make_ctx(wired, name="Elves"))
    await jams_plugin.invite_command.callback(
        make_ctx(wired, user=SimpleNamespace(id=2, is_bot=False))
    )

    assert wired.rest.create_thread.await_args.args[2] == "Elves"
    assert wired.rest.add_thread_member.await_args_list[-1].args == (THREAD, 2)
    assert wired.api.posts[-1] == (
        f"{JAM_API}/teams/{THREAD}/members",
        {"user_id": "2", "invited_by": "1"},
    )

    ctx = make_ctx(wired)
    await jams_plugin.leave_command.callback(ctx)

    assert "You left **Elves**" in ctx.respond.await_args.args[0]
    assert wired.api.teams[str(THREAD)]["member_ids"] == ["2"]


async def test_team_waits_until_the_bot_knows_its_own_id(wired):
    wired.bot.get_me = lambda: None
    ctx = make_ctx(wired, name="Elves")

    await jams_plugin.team_command.callback(ctx)

    ctx.respond.assert_awaited_once()
    assert "still starting up" in ctx.respond.await_args.args[0]
    assert ctx.respond.await_args.kwargs == {"flags": hikari.MessageFlag.EPHEMERAL}
    wired.rest.create_thread.assert_not_awaited()


async def test_invite_and_leave_do_not_need_the_bots_own_id(wired):
    wired.bot.get_me = lambda: None
    wired.api.add_team(THREAD, 1)
    ctx = make_ctx(wired, user=SimpleNamespace(id=2, is_bot=False))

    await jams_plugin.invite_command.callback(ctx)

    assert "now on the team" in ctx.respond.await_args.args[0]


async def test_commands_say_try_again_without_the_bot_api(wired):
    wired.bot.d.clear()
    ctx = make_ctx(wired)

    await jams_plugin.subscribe_command.callback(ctx)

    assert ctx.respond.await_args.args == (jams.UNAVAILABLE,)


async def test_poll_posts_only_when_this_process_is_acting(wired, monkeypatch):
    wired.api.due = [due_message()]
    monkeypatch.setattr(leadership, "is_acting", lambda: False)

    await jams_plugin.poll_once()

    assert wired.api.posts == []
    monkeypatch.setattr(leadership, "is_acting", lambda: True)

    await jams_plugin.poll_once()

    wired.rest.create_message.assert_awaited_once()
    assert wired.api.posts[-1][0] == "/jam-messages/m1/mark-sent"


async def test_jam_work_runs_only_when_this_process_is_acting(wired, monkeypatch):
    wired.api.work = [jam_work()]
    wired.rest.fetch_active_threads.return_value = [showcase_post(id=701)]
    monkeypatch.setattr(leadership, "is_acting", lambda: False)

    await jams_plugin.work_once()

    assert wired.api.gets == [] and wired.api.posts == []
    monkeypatch.setattr(leadership, "is_acting", lambda: True)

    await jams_plugin.work_once()

    assert [path for path, _ in wired.api.posts] == [f"{JAM_API}/submissions"]


async def test_unloading_stops_both_loops(wired, monkeypatch):
    started = []

    async def forever():
        started.append(1)
        await asyncio.Event().wait()

    monkeypatch.setattr(jams_plugin, "poll_once", forever)
    monkeypatch.setattr(jams_plugin, "work_once", forever)
    monkeypatch.setattr(jams_plugin, "_task", None)
    monkeypatch.setattr(jams_plugin, "_work_task", None)
    await jams_plugin.on_started(None)
    await asyncio.sleep(0)
    tasks = [jams_plugin._task, jams_plugin._work_task]
    assert len(started) == 2

    jams_plugin.unload(SimpleNamespace(remove_plugin=lambda plugin: None))
    await asyncio.gather(*tasks, return_exceptions=True)

    assert all(task.cancelled() for task in tasks)
    assert jams_plugin._task is None and jams_plugin._work_task is None


async def test_a_new_thread_event_tags_a_showcase_post(wired):
    post = showcase_post(created_at=datetime.now(UTC))
    wired.api.body["jam"]["starts_at"] = (
        datetime.now(UTC) - timedelta(days=1)
    ).isoformat()
    wired.api.body["jam"]["submissions_close_at"] = (
        datetime.now(UTC) + timedelta(days=1)
    ).isoformat()

    await jams_plugin.on_thread_create(SimpleNamespace(thread=post))

    wired.rest.edit_channel.assert_awaited_once()
    assert wired.api.entries == {"700": "solo:1"}


async def test_a_thread_event_never_raises(wired):
    wired.rest.edit_channel.side_effect = RuntimeError("boom")
    wired.api.fail_posts.add("/jam/submissions")
    post = showcase_post(created_at=datetime.now(UTC))
    wired.api.body["jam"]["starts_at"] = "2000-01-01T00:00:00+00:00"
    wired.api.body["jam"]["submissions_close_at"] = "2999-01-01T00:00:00+00:00"

    await jams_plugin.on_thread_create(SimpleNamespace(thread=post))

    wired.rest.edit_channel.assert_awaited_once()
    assert wired.api.entries == {}


async def test_a_failed_tag_does_not_stop_the_entry_being_recorded(wired):
    wired.rest.edit_channel.side_effect = RuntimeError("boom")
    post = showcase_post(created_at=datetime.now(UTC))
    wired.api.body["jam"]["starts_at"] = "2000-01-01T00:00:00+00:00"
    wired.api.body["jam"]["submissions_close_at"] = "2999-01-01T00:00:00+00:00"

    await jams_plugin.on_thread_create(SimpleNamespace(thread=post))

    assert wired.api.entries == {"700": "solo:1"}


async def test_a_deleted_thread_event_forgets_the_entry(wired):
    wired.api.entries["700"] = "solo:1"
    event = SimpleNamespace(guild_id=GUILD, parent_id=SHOWCASE, thread_id=700)

    await jams_plugin.on_thread_delete(event)

    assert wired.api.entries == {}
    wired.api.fail_posts.add("/remove")
    await jams_plugin.on_thread_delete(event)
