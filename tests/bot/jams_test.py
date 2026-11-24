"""The bot's side of code jams: scheduled posts, showcase tags, role, teams."""

from __future__ import annotations

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
TEAMS = 13
SHOWCASE = 14
ROLE = 20
THREAD = 500


def guild_jam(*, jam: bool = True, **overrides) -> dict:
    body = {
        "channels": {"news": "11", "chat": "12", "teams": "13", "showcase": "14"},
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


class FakeApi:
    """The web API as the bot sees it: one guild's jam and a queue of claims."""

    def __init__(self, body: dict | None = None, due: list[dict] | None = None):
        self.body = guild_jam() if body is None else body
        self.due = list(due or [])
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict | None]] = []
        self.fail_get = False
        self.fail_posts: set[str] = set()

    async def get(self, path, **_):
        self.gets.append(path)
        if self.fail_get:
            raise RuntimeError("api down")
        return SimpleNamespace(json=lambda: self.body)

    async def post(self, path, json_data=None):
        if any(path.endswith(suffix) for suffix in self.fail_posts):
            raise RuntimeError("api down")
        self.posts.append((path, json_data))
        if path == "/jam-messages/claim":
            due, self.due = self.due, []
            return SimpleNamespace(json=lambda: {"messages": due})
        return SimpleNamespace(json=lambda: {"success": True})


def make_rest(**members) -> Mock:
    rest = Mock()
    for name in (
        "create_message",
        "edit_channel",
        "add_role_to_member",
        "remove_role_from_member",
        "add_thread_member",
    ):
        setattr(rest, name, AsyncMock())
    rest.create_thread = AsyncMock(return_value=SimpleNamespace(id=THREAD))
    rest.fetch_channel = AsyncMock(
        return_value=SimpleNamespace(
            type=hikari.ChannelType.GUILD_PRIVATE_THREAD, parent_id=TEAMS
        )
    )
    rest.fetch_thread_members = AsyncMock(
        return_value=[
            SimpleNamespace(user_id=uid) for uid in members.get("ids", [BOT_ID, 1])
        ]
    )
    return rest


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
        "type": hikari.ChannelType.GUILD_PUBLIC_THREAD,
        "created_at": NOW - timedelta(seconds=2),
        "applied_tag_ids": [],
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


async def test_team_makes_a_private_thread_only_the_bot_can_add_to():
    rest = make_rest()

    reply = await jams.create_team(
        jams.JamApi(FakeApi()), rest, GUILD, 1, "  Tinsel   Bots "
    )

    call = rest.create_thread.await_args
    assert call.args == (TEAMS, hikari.ChannelType.GUILD_PRIVATE_THREAD, "Tinsel Bots")
    assert call.kwargs["invitable"] is False
    rest.add_thread_member.assert_awaited_once_with(THREAD, 1)
    welcome = rest.create_message.await_args
    assert welcome.args[0] == THREAD
    assert "up to 2 teammates" in welcome.args[1]
    assert reply == f"Your team thread is ready: <#{THREAD}>"


@pytest.mark.parametrize(
    ("body", "name", "expected"),
    [
        (guild_jam(jam=False), "Elves", jams.NO_JAM),
        (guild_jam(channels={"teams": None}), "Elves", jams.NOT_SET_UP),
        (guild_jam(), "   ", "1 to 80 characters"),
        (guild_jam(), "x" * 81, "1 to 80 characters"),
    ],
    ids=["no jam", "no teams channel", "blank name", "long name"],
)
async def test_team_refusals_make_no_thread(body, name, expected):
    rest = make_rest()

    reply = await jams.create_team(jams.JamApi(FakeApi(body)), rest, GUILD, 1, name)

    assert expected in reply
    rest.create_thread.assert_not_awaited()


async def test_a_solo_jam_has_no_teams():
    body = guild_jam()
    body["jam"]["max_team_size"] = 1
    rest = make_rest()

    reply = await jams.create_team(jams.JamApi(FakeApi(body)), rest, GUILD, 1, "Elves")

    assert "solo jam" in reply
    rest.create_thread.assert_not_awaited()


async def test_team_says_so_when_discord_refuses():
    rest = make_rest()
    rest.create_thread.side_effect = forbidden()

    reply = await jams.create_team(jams.JamApi(FakeApi()), rest, GUILD, 1, "Elves")

    assert "could not make" in reply


async def test_team_is_still_made_if_the_welcome_fails():
    rest = make_rest()
    rest.create_message.side_effect = forbidden()

    reply = await jams.create_team(jams.JamApi(FakeApi()), rest, GUILD, 1, "Elves")

    assert f"<#{THREAD}>" in reply


async def invite(rest, body=None, **overrides) -> str:
    values = {
        "guild_id": GUILD,
        "channel_id": THREAD,
        "bot_user_id": BOT_ID,
        "target_id": 2,
        "target_is_bot": False,
    }
    values.update(overrides)
    return await jams.invite_to_team(jams.JamApi(FakeApi(body)), rest, **values)


async def test_invite_adds_a_teammate():
    rest = make_rest(ids=[BOT_ID, 1])

    reply = await invite(rest)

    rest.add_thread_member.assert_awaited_once_with(THREAD, 2)
    assert reply == "<@2> is now on the team."


async def test_invite_stops_at_the_team_size_and_does_not_count_the_bot():
    almost = make_rest(ids=[BOT_ID, 1, 3])
    full = make_rest(ids=[BOT_ID, 1, 3, 4])

    assert "now on the team" in await invite(almost)
    reply = await invite(full)

    assert "full" in reply and "3 people" in reply
    full.add_thread_member.assert_not_awaited()


async def test_invite_refuses_bots_and_people_already_on_the_team():
    rest = make_rest(ids=[BOT_ID, 1, 2])

    assert "bot cannot" in await invite(rest, target_id=9, target_is_bot=True)
    assert "already" in await invite(rest)
    rest.add_thread_member.assert_not_awaited()


@pytest.mark.parametrize(
    "channel",
    [
        SimpleNamespace(type=hikari.ChannelType.GUILD_TEXT, parent_id=None),
        SimpleNamespace(type=hikari.ChannelType.GUILD_PRIVATE_THREAD, parent_id=999),
        SimpleNamespace(type=hikari.ChannelType.GUILD_PUBLIC_THREAD, parent_id=TEAMS),
    ],
    ids=["a channel", "a private thread elsewhere", "a public thread"],
)
async def test_invite_only_works_in_a_team_thread(channel):
    rest = make_rest()
    rest.fetch_channel.return_value = channel

    reply = await invite(rest)

    assert "inside your team's thread" in reply
    rest.add_thread_member.assert_not_awaited()


async def test_invite_needs_an_open_jam():
    rest = make_rest()

    assert await invite(rest, guild_jam(jam=False)) == jams.NO_JAM
    rest.add_thread_member.assert_not_awaited()


async def test_invite_says_so_when_the_members_cannot_be_read_or_added():
    unreadable = make_rest()
    unreadable.fetch_thread_members.side_effect = server_error()
    gone = make_rest()
    gone.add_thread_member.side_effect = not_found()

    assert "could not read" in await invite(unreadable)
    unreadable.add_thread_member.assert_not_awaited()
    assert "could not add" in await invite(gone)


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


def test_the_jam_command_has_its_four_subcommands():
    group = jams_plugin.jam_group
    assert group.name == "jam"
    assert {child.name for child in group.subcommands} == {
        "join",
        "leave",
        "team",
        "invite",
    }


async def test_a_command_defers_then_answers_only_the_caller(wired):
    ctx = make_ctx(wired)

    await jams_plugin.join_command.callback(ctx)

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

    await jams_plugin.leave_command.callback(ctx)

    assert "went wrong" in ctx.respond.await_args.args[0]


async def test_team_and_invite_pass_their_options(wired):
    await jams_plugin.team_command.callback(make_ctx(wired, name="Elves"))
    await jams_plugin.invite_command.callback(
        make_ctx(wired, user=SimpleNamespace(id=2, is_bot=False))
    )

    assert wired.rest.create_thread.await_args.args[2] == "Elves"
    assert wired.rest.add_thread_member.await_args_list[-1].args == (THREAD, 2)


async def test_commands_answer_not_set_up_without_the_bot_api(wired):
    wired.bot.d.clear()
    ctx = make_ctx(wired)

    await jams_plugin.join_command.callback(ctx)

    assert ctx.respond.await_args.args == (jams.NOT_SET_UP,)


async def test_poll_posts_only_when_this_process_is_acting(wired, monkeypatch):
    wired.api.due = [due_message()]
    monkeypatch.setattr(leadership, "is_acting", lambda: False)

    await jams_plugin.poll_once()

    assert wired.api.posts == []
    monkeypatch.setattr(leadership, "is_acting", lambda: True)

    await jams_plugin.poll_once()

    wired.rest.create_message.assert_awaited_once()
    assert wired.api.posts[-1][0] == "/jam-messages/m1/mark-sent"


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


async def test_a_thread_event_never_raises(wired):
    wired.rest.edit_channel.side_effect = RuntimeError("boom")
    post = showcase_post(created_at=datetime.now(UTC))
    wired.api.body["jam"]["starts_at"] = "2000-01-01T00:00:00+00:00"
    wired.api.body["jam"]["submissions_close_at"] = "2999-01-01T00:00:00+00:00"

    await jams_plugin.on_thread_create(SimpleNamespace(thread=post))
