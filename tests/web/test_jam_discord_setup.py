"""Tests for creating a guild's jam channels, role and showcase tags."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC
from datetime import datetime

import httpx

from smarter_dev.web.jams.discord_setup import CATEGORY_NAME
from smarter_dev.web.jams.discord_setup import FORUM_TAG_LIMIT
from smarter_dev.web.jams.discord_setup import run_jam_setup
from smarter_dev.web.models import JamDiscordSetup
from tests.web.jam_helpers import BOT_USER
from tests.web.jam_helpers import GUILD
from tests.web.jam_helpers import FakeDiscord
from tests.web.jam_helpers import discord_error
from tests.web.jam_helpers import make_jam

_TEXT, _CATEGORY, _NEWS, _FORUM = 0, 4, 5, 15


def _empty_setup() -> JamDiscordSetup:
    return JamDiscordSetup(guild_id=GUILD)


async def test_fresh_guild_gets_everything():
    discord, setup, jam = FakeDiscord(), _empty_setup(), make_jam()

    report = await run_jam_setup(discord, setup, jam)

    assert report.ok, report.errors
    assert not setup.missing_parts
    assert not jam.missing_tags
    by_name = {channel.name: channel for channel in discord.channels}
    assert by_name[CATEGORY_NAME].type == _CATEGORY
    for name in ("jam-news", "jam-chat"):
        assert by_name[name].type == _TEXT
        assert by_name[name].parent_id == setup.category_id
    assert by_name["jam-showcase"].type == _FORUM
    assert [role.name for role in discord.roles] == ["Jammer"]
    tags = discord.tags[setup.showcase_channel_id]
    assert [(tag["name"], tag["moderated"]) for tag in tags] == [
        ("Holiday Jam", True),
        ("2026", True),
    ]
    assert jam.showcase_tag_id == tags[0]["id"]
    assert jam.year_tag_id == tags[1]["id"]
    assert len(report.created) == 7
    assert report.adopted == []


async def test_only_the_news_channel_is_read_only():
    discord, setup = FakeDiscord(), _empty_setup()

    await run_jam_setup(discord, setup)

    payloads = {
        call[1]["name"]: call[1] for call in discord.created("create_guild_channel")
    }
    assert payloads["jam-news"]["permission_overwrites"] == [
        {"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 11)},
        {"id": BOT_USER, "type": 1, "allow": str(1 << 10 | 1 << 11), "deny": "0"},
    ]
    assert "permission_overwrites" not in payloads["jam-showcase"]
    chat_denies = int(payloads["jam-chat"]["permission_overwrites"][0]["deny"])
    assert not chat_denies & (1 << 11)


async def test_only_the_bot_opens_private_threads_in_the_chat_channel():
    discord, setup = FakeDiscord(), _empty_setup()

    await run_jam_setup(discord, setup)

    payloads = {
        call[1]["name"]: call[1] for call in discord.created("create_guild_channel")
    }
    threads = str(1 << 36)
    assert payloads["jam-chat"]["permission_overwrites"] == [
        {"id": GUILD, "type": 0, "allow": "0", "deny": threads},
        {"id": BOT_USER, "type": 1, "allow": threads, "deny": "0"},
    ]


async def test_a_refused_chat_channel_names_the_thread_permission():
    discord, setup = FakeDiscord(), _empty_setup()
    discord.fail["create_guild_channel:jam-chat"] = discord_error(403)

    report = await run_jam_setup(discord, setup)

    assert report.errors == [
        "#jam-chat: the bot needs the Manage Channels and Create Private Threads "
        "permissions"
    ]
    assert setup.chat_channel_id is None
    assert setup.showcase_channel_id is not None


async def test_second_run_creates_nothing():
    discord, setup, jam = FakeDiscord(), _empty_setup(), make_jam()
    await run_jam_setup(discord, setup, jam)
    discord.calls.clear()

    report = await run_jam_setup(discord, setup, jam)

    assert report.ok
    assert report.created == [] and report.adopted == []
    assert discord.calls == []


async def test_without_a_jam_no_tags_are_touched():
    discord, setup = FakeDiscord(), _empty_setup()

    report = await run_jam_setup(discord, setup)

    assert report.ok
    assert discord.created("set_forum_tags") == []


async def test_existing_pieces_are_adopted_not_duplicated():
    discord, setup, jam = FakeDiscord(), _empty_setup(), make_jam()
    category = discord.add_channel("code jams", _CATEGORY)
    chat = discord.add_channel("jam-chat", _TEXT, category)
    role = discord.add_role("jammer")

    report = await run_jam_setup(discord, setup, jam)

    assert report.ok
    assert setup.category_id == category
    assert setup.chat_channel_id == chat
    assert setup.role_id == role
    assert sorted(report.adopted) == ["#jam-chat", "@Jammer role", "Code Jams category"]
    assert discord.created("create_guild_role") == []
    assert len(discord.created("create_guild_channel")) == 2


async def test_same_named_channel_outside_the_category_is_not_adopted():
    discord, setup = FakeDiscord(), _empty_setup()
    stray = discord.add_channel("jam-chat", _TEXT, None)

    await run_jam_setup(discord, setup)

    assert setup.chat_channel_id != stray


async def test_both_tags_already_in_the_forum_are_reused():
    """A yearly jam keeps its tag: nothing is created when both exist."""
    discord, jam = FakeDiscord(), make_jam(tag_name="holiday jam")
    setup = _empty_setup()
    await run_jam_setup(discord, setup)
    forum = setup.showcase_channel_id
    jam_tag = discord.add_tag(forum, "Holiday Jam")
    year_tag = discord.add_tag(forum, "2026")
    discord.calls.clear()

    report = await run_jam_setup(discord, setup, jam)

    assert report.ok
    assert (jam.showcase_tag_id, jam.year_tag_id) == (jam_tag, year_tag)
    assert discord.created("set_forum_tags") == []
    assert len(report.adopted) == 2


async def test_creating_a_tag_keeps_the_forums_other_tags():
    discord, setup, jam = FakeDiscord(), _empty_setup(), make_jam()
    await run_jam_setup(discord, setup)
    forum = setup.showcase_channel_id
    other = discord.add_tag(forum, "Open to Contributors")
    year_tag = discord.add_tag(forum, "2026")

    report = await run_jam_setup(discord, setup, jam)

    assert report.ok
    sent = discord.created("set_forum_tags")[0][2]
    assert [tag.get("id") for tag in sent] == [other, year_tag, None]
    assert sent[2] == {"name": "Holiday Jam", "moderated": True}
    assert jam.year_tag_id == year_tag
    assert report.adopted == ["“2026” showcase tag"]


async def test_year_tag_follows_the_start_year():
    discord, setup = FakeDiscord(), _empty_setup()
    jam = make_jam(starts_at=datetime(2027, 12, 31, 23, 0, tzinfo=UTC))

    await run_jam_setup(discord, setup, jam)

    names = [tag["name"] for tag in discord.tags[setup.showcase_channel_id]]
    assert names == ["Holiday Jam", "2027"]


async def test_a_deleted_tag_is_made_again():
    discord, setup, jam = FakeDiscord(), _empty_setup(), make_jam()
    await run_jam_setup(discord, setup, jam)
    forum = setup.showcase_channel_id
    discord.tags[forum] = [t for t in discord.tags[forum] if t["name"] == "2026"]
    old = jam.showcase_tag_id

    report = await run_jam_setup(discord, setup, jam)

    assert report.ok
    assert jam.showcase_tag_id not in (None, old)


async def test_a_full_forum_is_reported_and_left_alone():
    discord, setup, jam = FakeDiscord(), _empty_setup(), make_jam()
    await run_jam_setup(discord, setup)
    forum = setup.showcase_channel_id
    for index in range(FORUM_TAG_LIMIT - 1):
        discord.add_tag(forum, f"tag {index}")
    discord.calls.clear()

    report = await run_jam_setup(discord, setup, jam)

    assert len(report.errors) == 2
    assert "19 of the 20 tags" in report.errors[0]
    assert discord.created("set_forum_tags") == []
    assert jam.missing_tags == ["Holiday Jam", "2026"]


async def test_a_showcase_forum_chosen_by_the_admin_is_kept():
    discord, jam = FakeDiscord(), make_jam()
    existing = discord.add_channel("showcase", _FORUM, "another-category")
    setup = JamDiscordSetup(guild_id=GUILD, showcase_channel_id=existing)

    report = await run_jam_setup(discord, setup, jam)

    assert report.ok
    assert setup.showcase_channel_id == existing
    assert [tag["name"] for tag in discord.tags[existing]] == ["Holiday Jam", "2026"]
    assert "jam-showcase" not in [channel.name for channel in discord.channels]


async def test_a_stored_channel_that_was_deleted_is_made_again():
    discord, setup = FakeDiscord(), _empty_setup()
    await run_jam_setup(discord, setup)
    gone = setup.chat_channel_id
    discord.channels = [c for c in discord.channels if c.id != gone]

    report = await run_jam_setup(discord, setup)

    assert report.created == ["#jam-chat"]
    assert setup.chat_channel_id != gone


async def test_a_same_named_role_with_permissions_is_refused():
    """Members give themselves this role, so it must grant nothing."""
    discord, setup = FakeDiscord(), _empty_setup()
    discord.add_role("Jammer", permissions=1 << 3)

    report = await run_jam_setup(discord, setup)

    assert setup.role_id is None
    assert any("grants permissions" in error for error in report.errors)
    assert discord.created("create_guild_role") == []
    # The rest of setup still ran.
    assert setup.missing_parts == ["role"]


async def test_a_managed_same_named_role_is_refused():
    discord, setup = FakeDiscord(), _empty_setup()
    discord.add_role("Jammer", managed=True)

    report = await run_jam_setup(discord, setup)

    assert setup.role_id is None
    assert not report.ok


async def test_one_failed_channel_keeps_the_rest_and_can_be_finished():
    discord, setup, jam = FakeDiscord(), _empty_setup(), make_jam()
    discord.fail["create_guild_channel:jam-chat"] = discord_error(403)

    report = await run_jam_setup(discord, setup, jam)

    assert report.errors == [
        "#jam-chat: the bot needs the Manage Channels and Create Private Threads "
        "permissions"
    ]
    assert setup.chat_channel_id is None
    assert setup.news_channel_id and setup.showcase_channel_id and setup.role_id
    assert not jam.missing_tags

    discord.fail.clear()
    discord.calls.clear()
    report = await run_jam_setup(discord, setup, jam)

    assert report.ok
    assert report.created == ["#jam-chat"]
    assert len(discord.calls) == 1


async def test_no_category_means_no_channels():
    discord, setup, jam = FakeDiscord(), _empty_setup(), make_jam()
    discord.fail["create_guild_channel"] = discord_error(403)

    report = await run_jam_setup(discord, setup, jam)

    assert setup.category_id is None
    assert discord.created("create_guild_channel") == []
    assert setup.role_id is not None
    # Both tags are reported: there is no forum to put them in.
    assert sum("showcase tag" in error for error in report.errors) == 2


async def test_unreadable_guild_stops_before_creating_anything():
    discord, setup = FakeDiscord(), _empty_setup()
    discord.fail["get_guild_channels"] = discord_error(None)

    report = await run_jam_setup(discord, setup, make_jam())

    assert report.errors == [
        "Could not read the server's channels and roles: Discord could not be reached"
    ]
    assert discord.calls == []


async def test_rate_limit_and_other_statuses_read_plainly():
    discord, setup = FakeDiscord(), _empty_setup()
    discord.fail["create_guild_role"] = discord_error(429)
    discord.fail["create_guild_channel:jam-news"] = discord_error(500)

    report = await run_jam_setup(discord, setup)

    assert "@Jammer role: Discord is rate limiting the bot; try again in a minute" in (
        report.errors
    )
    assert "#jam-news: Discord refused the request (HTTP 500)" in report.errors


async def test_tag_write_failure_leaves_ids_empty():
    discord, setup, jam = FakeDiscord(), _empty_setup(), make_jam()
    discord.fail["set_forum_tags"] = discord_error(403)

    report = await run_jam_setup(discord, setup, jam)

    assert jam.showcase_tag_id is None and jam.year_tag_id is None
    assert len(report.errors) == 2


async def test_a_network_failure_is_reported_not_raised():
    discord, setup = FakeDiscord(), _empty_setup()
    discord.fail["create_guild_role"] = httpx.ConnectError("no route to host")

    report = await run_jam_setup(discord, setup, make_jam())

    assert "@Jammer role: Discord could not be reached" in report.errors
    assert setup.role_id is None
    # The rest of the setup still ran.
    assert setup.category_id and setup.news_channel_id


async def test_an_unreachable_discord_stops_before_creating_anything():
    discord, setup = FakeDiscord(), _empty_setup()
    discord.fail["get_guild_channels"] = httpx.ReadTimeout("timed out")

    report = await run_jam_setup(discord, setup, make_jam())

    assert report.errors == [
        "Could not read the server's channels and roles: Discord could not be reached"
    ]
    assert discord.calls == []


async def test_news_turned_into_an_announcement_channel_is_kept():
    discord, setup, jam = FakeDiscord(), _empty_setup(), make_jam()
    await run_jam_setup(discord, setup, jam)
    news_id = setup.news_channel_id
    index = next(i for i, c in enumerate(discord.channels) if c.id == news_id)
    discord.channels[index] = replace(discord.channels[index], type=_NEWS)
    discord.calls.clear()

    report = await run_jam_setup(discord, setup, jam)

    assert report.ok, report.errors
    assert setup.news_channel_id == news_id
    assert discord.calls == []


async def test_an_announcement_jam_news_is_adopted_by_name():
    discord, setup = FakeDiscord(), _empty_setup()
    category = discord.add_channel(CATEGORY_NAME, _CATEGORY)
    news = discord.add_channel("jam-news", _NEWS, category)

    report = await run_jam_setup(discord, setup, make_jam())

    assert report.ok, report.errors
    assert setup.news_channel_id == news
    assert "#jam-news" in report.adopted
    assert [c.name for c in discord.channels].count("jam-news") == 1


async def test_only_news_may_be_an_announcement_channel():
    discord, setup = FakeDiscord(), _empty_setup()
    category = discord.add_channel(CATEGORY_NAME, _CATEGORY)
    stray = discord.add_channel("jam-chat", _NEWS, category)

    await run_jam_setup(discord, setup, make_jam())

    assert setup.chat_channel_id != stray
