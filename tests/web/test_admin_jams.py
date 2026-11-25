"""Tests for the code jam admin pages."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4

import httpx
import jinja2
import pytest
from litestar.datastructures import FormMultiDict
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from skrift.forms.core import csrf_field as skrift_csrf_field
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.bot_admin.jams import MESSAGE_CONTENT_MAX
from smarter_dev.web.bot_admin.jams import JamsAdminController
from smarter_dev.web.bot_admin.jams import read_jam_form
from smarter_dev.web.bot_admin.jams import read_jam_message_form
from smarter_dev.web.bot_admin.jams import validate_jam_form
from smarter_dev.web.bot_admin.jams import validate_jam_message_form
from smarter_dev.web.discord_admin_client import DiscordGuildDetail
from smarter_dev.web.jams import results
from smarter_dev.web.jams import store
from smarter_dev.web.jams import teams
from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamMessage
from smarter_dev.web.models import JamSubmission
from smarter_dev.web.models import JamTeam
from smarter_dev.web.models import JamTeamMember
from tests.web.jam_helpers import GUILD
from tests.web.jam_helpers import START
from tests.web.jam_helpers import FakeDiscord
from tests.web.jam_helpers import discord_error
from tests.web.jam_helpers import make_jam
from tests.web.jam_helpers import make_message
from tests.web.jam_helpers import make_setup

_MODULE = "smarter_dev.web.bot_admin.jams"
_CAMPAIGNS_MODULE = "smarter_dev.web.bot_admin.campaigns"
_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
_JAMS_URL = f"/admin/bot/guilds/{GUILD}/jams"

_VALID_JAM = {
    "name": "Holiday Chatbot Jam",
    "tag_name": "Holiday Jam",
    "max_team_size": "4",
    "starts_at": "2026-12-01T00:00",
    "submissions_close_at": "2027-01-03T23:59",
    "voting_ends_at": "2027-01-10T23:59",
    "winners_at": "2027-01-11T18:00",
}
_VALID_MESSAGE = {
    "channel_key": "news",
    "content": "One week left!",
    "send_at": "2026-12-27T15:00",
    "mention_role": "1",
}


class _Guild(FakeDiscord):
    async def get_guild(self, guild_id: str) -> DiscordGuildDetail:
        return DiscordGuildDetail(
            id=GUILD,
            name="Alpha Guild",
            icon=None,
            owner_id="owner",
            member_count=42,
            description=None,
        )


def _form(values: dict) -> FormMultiDict:
    return FormMultiDict(list(values.items()))


@jinja2.pass_context
def _csrf_field(context) -> str:
    """The app's ``csrf_field`` global: Skrift's own, fed the page's request."""
    return skrift_csrf_field(context["request"])


def _render(response) -> str:
    """Render a handler's template for real, so a broken template fails."""
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.DictLoader(
                    {"admin/base.html": "{% block admin_content %}{% endblock %}"}
                ),
                jinja2.FileSystemLoader(_TEMPLATES),
            ]
        ),
        undefined=jinja2.StrictUndefined,
        autoescape=True,
    )
    env.globals["site_name"] = lambda: "Smarter Dev"
    env.globals["csrf_field"] = _csrf_field
    # Litestar hands every template the request; the session holds the token.
    request = SimpleNamespace(session={})
    return env.get_template(response.template_name).render(
        request=request, **response.context
    )


@contextmanager
def _patched(discord, flashes, csrf=True):
    """The controller with its admin shell, Discord and Skrift calls faked."""
    with (
        patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=discord),
        patch(f"{_MODULE}.get_admin_discord_client", return_value=discord),
        patch(f"{_MODULE}.flash_success", flashes.success),
        patch(f"{_MODULE}.flash_error", flashes.error),
        patch(f"{_MODULE}.get_flash_messages", return_value=[]),
        patch(f"{_MODULE}.verify_csrf", new=AsyncMock(return_value=csrf)),
    ):
        yield


async def _run(handler: str, db_session, *, form=None, **path):
    request = SimpleNamespace(form=AsyncMock(return_value=_form(form or {})))
    return await getattr(JamsAdminController, handler).fn(
        None, request=request, db_session=db_session, guild_id=GUILD, **path
    )


async def _call(
    handler: str, db_session, *, form=None, discord=None, csrf=True, **path
):
    """Run one controller handler; returns (response, flashes, discord, html).

    ``csrf=False`` is a POST whose CSRF token Skrift refuses.
    """
    discord = discord or _Guild()
    flashes = SimpleNamespace(success=Mock(), error=Mock())
    with _patched(discord, flashes, csrf):
        response = await _run(handler, db_session, form=form, **path)
    html = _render(response) if hasattr(response, "template_name") else None
    return response, flashes, discord, html


async def _call_twice_at_once(handler: str, db_session, engine, discord, **kwargs):
    """The same request from two sessions, overlapping; returns both responses."""
    flashes = SimpleNamespace(success=Mock(), error=Mock())
    async with AsyncSession(engine, expire_on_commit=False) as other:
        with _patched(discord, flashes):
            return await asyncio.gather(
                _run(handler, db_session, **kwargs), _run(handler, other, **kwargs)
            )


def _redirected(response, path: str) -> bool:
    return response.status_code in (302, 303, 307) and response.url == path


async def _seed_jam(db_session, *, setup=True, **overrides) -> Jam:
    jam = make_jam(**overrides)
    db_session.add(jam)
    if setup:
        db_session.add(make_setup())
    await db_session.commit()
    return jam


async def _seed_message(db_session, jam: Jam, **overrides) -> JamMessage:
    message = make_message(jam, **overrides)
    db_session.add(message)
    await db_session.commit()
    return message


async def _all_jams(db_session) -> list[Jam]:
    return await store.list_jams(db_session, GUILD)


# -- guards -------------------------------------------------------------------


def test_every_route_needs_an_administrator():
    handlers = [
        value
        for value in vars(JamsAdminController).values()
        if hasattr(value, "fn") and hasattr(value, "guards")
    ]
    assert len(handlers) == 17
    for handler in handlers:
        assert handler.guards[0] is auth_guard
        assert isinstance(handler.guards[1], Permission)
        assert handler.guards[1].permission == "administrator"


# -- pure: jam form -----------------------------------------------------------


def test_valid_jam_form_is_cleaned():
    ok, errors, cleaned = validate_jam_form(read_jam_form(_form(_VALID_JAM)))

    assert ok and errors == []
    assert cleaned["tag_name"] == "Holiday Jam"
    assert cleaned["max_team_size"] == 4
    assert cleaned["starts_at"] == datetime(2026, 12, 1, tzinfo=UTC)
    assert cleaned["winners_at"] == datetime(2027, 1, 11, 18, tzinfo=UTC)


def test_optional_dates_may_be_blank():
    data = {**_VALID_JAM, "voting_ends_at": "", "winners_at": ""}

    ok, _, cleaned = validate_jam_form(read_jam_form(_form(data)))

    assert ok
    assert cleaned["voting_ends_at"] is None and cleaned["winners_at"] is None


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"name": " "}, "Name is required"),
        ({"name": "x" * 101}, "Name must be 100 characters or fewer"),
        ({"tag_name": ""}, "Showcase tag is required"),
        ({"tag_name": "x" * 21}, "Showcase tag must be 20 characters or fewer"),
        ({"tag_name": "2026"}, "Showcase tag cannot be only digits"),
        ({"max_team_size": "four"}, "Team size must be a whole number"),
        ({"max_team_size": "0"}, "Team size must be between 1 and 20"),
        ({"max_team_size": "21"}, "Team size must be between 1 and 20"),
        ({"starts_at": ""}, "Start time is required"),
        ({"submissions_close_at": ""}, "Submission deadline is required"),
        ({"starts_at": "soon"}, "Start time is not a valid date and time"),
        (
            {"submissions_close_at": "2026-12-01T00:00"},
            "Submission deadline must be after the start time",
        ),
        (
            {"voting_ends_at": "2027-01-03T23:59"},
            "Voting end must be after the submission deadline",
        ),
        (
            {"winners_at": "2027-01-10T23:59"},
            "Winners date must be after the voting end",
        ),
        (
            {"voting_ends_at": "", "winners_at": "2027-01-03T00:00"},
            "Winners date must be after the submission deadline",
        ),
    ],
)
def test_jam_form_errors(change, error):
    ok, errors, _ = validate_jam_form(read_jam_form(_form({**_VALID_JAM, **change})))

    assert not ok
    assert error in errors


def test_a_20_character_tag_is_allowed():
    ok, _, _ = validate_jam_form({**_VALID_JAM, "tag_name": "x" * 20})
    assert ok


# -- pure: message form -------------------------------------------------------


def test_valid_message_form_is_cleaned():
    ok, errors, cleaned = validate_jam_message_form(
        read_jam_message_form(_form(_VALID_MESSAGE))
    )

    assert ok and errors == []
    assert cleaned == {
        "channel_key": "news",
        "content": "One week left!",
        "send_at": datetime(2026, 12, 27, 15, tzinfo=UTC),
        "mention_role": True,
    }


def test_an_unticked_ping_is_false():
    data = {k: v for k, v in _VALID_MESSAGE.items() if k != "mention_role"}

    _, _, cleaned = validate_jam_message_form(read_jam_message_form(_form(data)))

    assert cleaned["mention_role"] is False


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"channel_key": "showcase"}, "Choose one of the jam's channels"),
        ({"channel_key": ""}, "Choose one of the jam's channels"),
        ({"content": "  "}, "Message is required"),
        (
            {"content": "x" * (MESSAGE_CONTENT_MAX + 1)},
            f"Message must be {MESSAGE_CONTENT_MAX} characters or fewer",
        ),
        ({"send_at": ""}, "Send time is required"),
        ({"send_at": "noon"}, "Send time is not a valid date and time"),
    ],
)
def test_message_form_errors(change, error):
    ok, errors, _ = validate_jam_message_form(
        read_jam_message_form(_form({**_VALID_MESSAGE, **change}))
    )

    assert not ok
    assert error in errors


# -- list and showcase forum --------------------------------------------------


async def test_list_shows_jams_and_the_forum_choice(db_session):
    await _seed_jam(db_session)
    discord = _Guild()
    forum = discord.add_channel("showcase", 15)
    discord.add_channel("general", 0)

    response, _, _, html = await _call("jams_list", db_session, discord=discord)

    assert response.template_name == "admin/bot/jams/list.html"
    assert [f.id for f in response.context["forums"]] == [forum]
    assert "Holiday Chatbot Jam" in html
    assert "Holiday Jam, 2026" in html
    assert "Setup not finished" in html
    assert f'value="{forum}"' in html and "#showcase" in html


async def test_list_renders_with_no_jams_and_no_forums(db_session):
    _, _, _, html = await _call("jams_list", db_session)

    assert "No jams yet." in html
    assert "Showcase forum" not in html


async def test_list_shows_ready_once_everything_exists(db_session):
    await _seed_jam(db_session, showcase_tag_id="1", year_tag_id="2")

    _, _, _, html = await _call("jams_list", db_session)

    assert "Ready" in html and "Setup not finished" not in html


async def test_choosing_an_existing_forum_saves_it(db_session):
    jam = await _seed_jam(db_session, showcase_tag_id="1", year_tag_id="2")
    discord = _Guild()
    forum = discord.add_channel("showcase", 15)

    response, flashes, _, _ = await _call(
        "jams_showcase_forum", db_session, discord=discord, form={"channel_id": forum}
    )

    assert _redirected(response, _JAMS_URL)
    flashes.success.assert_called_once()
    assert (await store.get_setup(db_session, GUILD)).showcase_channel_id == forum
    await db_session.refresh(jam)
    assert jam.missing_tags == ["Holiday Jam", "2026"]


async def test_a_channel_that_is_not_a_forum_here_is_refused(db_session):
    await _seed_jam(db_session)
    discord = _Guild()
    text = discord.add_channel("general", 0)

    for channel_id in (text, "123456"):
        response, flashes, _, _ = await _call(
            "jams_showcase_forum",
            db_session,
            discord=discord,
            form={"channel_id": channel_id},
        )
        assert _redirected(response, _JAMS_URL)
        flashes.error.assert_called_once()
    assert (await store.get_setup(db_session, GUILD)).showcase_channel_id == "14"


async def test_blank_forum_choice_goes_back_to_a_new_forum(db_session):
    await _seed_jam(db_session)

    await _call("jams_showcase_forum", db_session, form={"channel_id": ""})

    setup = await store.get_setup(db_session, GUILD)
    assert setup.showcase_channel_id is None


# -- create -------------------------------------------------------------------


async def test_create_form_renders(db_session):
    response, _, _, html = await _call("jam_create_form", db_session)

    assert response.template_name == "admin/bot/jams/form.html"
    assert 'name="tag_name"' in html and "Create Jam" in html


async def test_create_saves_the_jam_and_sets_up_discord(db_session):
    response, flashes, discord, _ = await _call(
        "jam_create", db_session, form=_VALID_JAM
    )

    (jam,) = await _all_jams(db_session)
    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    assert (jam.name, jam.tag_name, jam.max_team_size) == (
        "Holiday Chatbot Jam",
        "Holiday Jam",
        4,
    )
    setup = await store.get_setup(db_session, GUILD)
    assert setup.missing_parts == [] and jam.missing_tags == []
    assert len(discord.created("create_guild_channel")) == 4
    flashes.success.assert_called_once()
    flashes.error.assert_not_called()
    assert "created" in flashes.success.call_args.args[1]


async def test_create_keeps_the_jam_when_discord_setup_fails(db_session):
    discord = _Guild()
    discord.fail["create_guild_channel:jam-chat"] = discord_error(403)

    response, flashes, _, _ = await _call(
        "jam_create", db_session, form=_VALID_JAM, discord=discord
    )

    (jam,) = await _all_jams(db_session)
    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    setup = await store.get_setup(db_session, GUILD)
    assert setup.missing_parts == ["chat"]
    assert setup.news_channel_id is not None
    message = flashes.error.call_args.args[1]
    assert (
        "#jam-chat: the bot needs the Manage Channels and Create Private Threads "
        "permissions."
    ) in message
    assert ".." not in message


async def test_create_rejects_an_invalid_form(db_session):
    response, _, discord, html = await _call(
        "jam_create", db_session, form={**_VALID_JAM, "tag_name": ""}
    )

    assert response.status_code == 400
    assert "Showcase tag is required" in html
    assert 'value="Holiday Chatbot Jam"' in html
    assert await _all_jams(db_session) == []
    assert discord.calls == []


async def test_create_refuses_overlapping_dates(db_session):
    await _seed_jam(db_session, name="Earlier Jam")

    response, _, discord, html = await _call(
        "jam_create",
        db_session,
        form={**_VALID_JAM, "name": "Second", "starts_at": "2026-12-20T00:00"},
    )

    assert response.status_code == 400
    assert "These dates overlap “Earlier Jam”" in html
    assert "one jam at a time" in html
    assert len(await _all_jams(db_session)) == 1
    assert discord.calls == []


async def test_a_later_jam_can_reuse_the_same_tag(db_session):
    """A yearly jam keeps one tag; only the year tag is new."""
    first, _, discord, _ = await _call("jam_create", db_session, form=_VALID_JAM)
    discord.calls.clear()
    next_year = {
        **_VALID_JAM,
        "name": "Holiday Chatbot Jam 2027",
        "starts_at": "2027-12-01T00:00",
        "submissions_close_at": "2028-01-03T23:59",
        "voting_ends_at": "",
        "winners_at": "",
    }

    response, flashes, _, _ = await _call(
        "jam_create", db_session, form=next_year, discord=discord
    )

    assert response.status_code != 400
    newer, older = await _all_jams(db_session)
    assert newer.showcase_tag_id == older.showcase_tag_id
    assert newer.year_tag_id != older.year_tag_id
    (tag_call,) = discord.created("set_forum_tags")
    assert [tag["name"] for tag in tag_call[2]] == ["Holiday Jam", "2026", "2027"]
    assert discord.created("create_guild_channel") == []
    flashes.error.assert_not_called()


# -- detail -------------------------------------------------------------------


async def test_detail_shows_setup_and_messages(db_session):
    jam = await _seed_jam(db_session)
    await _seed_message(db_session, jam, mention_role=True)

    response, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    assert response.template_name == "admin/bot/jams/detail.html"
    assert response.context["setup_finished"] is False
    assert "Finish setup" in html
    assert "“Holiday Jam” showcase tag" in html and "“2026” showcase tag" in html
    assert "#jam-news" in html and "pings @Jammer" in html
    assert "The jam is open!" in html


async def _seed_team(db_session, jam: Jam, thread_id: str, *members: str, name: str):
    team = JamTeam(jam_id=jam.id, name=name, thread_id=thread_id, created_by="1")
    for member in members:
        team.members.append(JamTeamMember(jam_id=jam.id, user_id=member))
    db_session.add(team)
    await db_session.commit()
    return team


async def test_detail_lists_teams_members_and_entries(db_session):
    jam = await _seed_jam(db_session)
    elves = await _seed_team(db_session, jam, "500", "2", "1", name="Tinsel <b>Bots")
    await _seed_team(db_session, jam, "501", "3", name="Reindeer")
    db_session.add_all(
        [
            JamSubmission(
                jam_id=jam.id, thread_id="700", author_id="1", team_id=elves.id
            ),
            JamSubmission(
                jam_id=jam.id, thread_id="701", author_id="2", team_id=elves.id
            ),
            JamSubmission(jam_id=jam.id, thread_id="702", author_id="9"),
        ]
    )
    await db_session.commit()

    response, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    assert "Teams (2)" in html
    assert "Tinsel &lt;b&gt;Bots" in html and "<b>Bots" not in html
    assert "<code>1</code>, <code>2</code>" in html
    assert f"https://discord.com/channels/{GUILD}/500" in html
    assert html.count("None yet") == 1
    # Two entries: the team's first post and the solo one. The third is extra.
    assert "Entries (2)" in html
    assert html.count("Extra post") == 1
    assert "The entry is <code>700</code>" in html
    assert "Solo" in html
    assert html.count("Remove entry") == 3
    assert f"{_JAMS_URL}/{jam.id}/entries/701/remove" in html
    assert response.context["entry_team_ids"] == {elves.id}


async def test_detail_with_no_teams_or_entries_says_so(db_session):
    jam = await _seed_jam(db_session)

    _, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    assert "Teams (0)" in html and "No teams yet." in html
    assert "Entries (0)" in html and "No entries yet." in html


async def test_remove_entry_forgets_the_post_and_frees_the_team(db_session):
    jam = await _seed_jam(db_session)
    elves = await _seed_team(db_session, jam, "500", "1", "2", name="Elves")
    db_session.add(
        JamSubmission(jam_id=jam.id, thread_id="700", author_id="1", team_id=elves.id)
    )
    await db_session.commit()

    response, flashes, discord, _ = await _call(
        "jam_entry_remove", db_session, jam_id=jam.id, thread_id="700"
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    assert "still in Discord" in flashes.success.call_args.args[1]
    assert discord.calls == []
    assert await teams.list_entries(db_session, jam.id) == []
    assert len(await teams.list_teams(db_session, jam.id)) == 1
    # The record is kept, so the bot does not find the post and record it again.
    [kept] = await teams.list_removed(db_session, jam.id)
    assert kept.thread_id == "700"

    _, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    assert "Entries (0)" in html and "Removed post" in html
    assert f"{_JAMS_URL}/{jam.id}/entries/700/restore" in html
    assert html.count("None yet") == 1


async def test_restore_entry_counts_the_post_again(db_session):
    jam = await _seed_jam(db_session)
    db_session.add(JamSubmission(jam_id=jam.id, thread_id="700", author_id="9"))
    await db_session.commit()
    await teams.remove_entry(db_session, jam, "700")

    response, flashes, _, _ = await _call(
        "jam_entry_restore", db_session, jam_id=jam.id, thread_id="700"
    )
    _, again, _, _ = await _call(
        "jam_entry_restore", db_session, jam_id=jam.id, thread_id="700"
    )
    _, missing, _, _ = await _call(
        "jam_entry_restore", db_session, jam_id=uuid4(), thread_id="700"
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    assert "counts again" in flashes.success.call_args.args[1]
    assert "not found" in again.error.call_args.args[1]
    assert "not found" in missing.error.call_args.args[1]
    assert len(await teams.list_entries(db_session, jam.id)) == 1
    _, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)
    assert "Entries (1)" in html and "Removed post" not in html


async def test_restore_says_when_the_author_has_joined_a_team(db_session):
    jam = await _seed_jam(db_session)
    db_session.add(JamSubmission(jam_id=jam.id, thread_id="700", author_id="9"))
    await db_session.commit()
    await teams.remove_entry(db_session, jam, "700")
    made = await teams.create_team(
        db_session,
        jam,
        name="Elves",
        thread_id="500",
        user_id="9",
        now=store.as_utc(jam.starts_at),
    )
    assert made.refusal is None

    _, flashes, _, _ = await _call(
        "jam_entry_restore", db_session, jam_id=jam.id, thread_id="700"
    )

    assert "joined a team" in flashes.error.call_args.args[1]
    assert await teams.list_entries(db_session, jam.id) == []


async def test_restore_without_a_valid_csrf_token_keeps_the_post_removed(db_session):
    jam = await _seed_jam(db_session)
    db_session.add(JamSubmission(jam_id=jam.id, thread_id="700", author_id="9"))
    await db_session.commit()
    await teams.remove_entry(db_session, jam, "700")

    await _call(
        "jam_entry_restore", db_session, jam_id=jam.id, thread_id="700", csrf=False
    )

    assert await teams.list_entries(db_session, jam.id) == []


# -- results ------------------------------------------------------------------


async def _seed_voted_jam(db_session, *, counted: bool):
    """A jam whose voting ended an hour ago, with a team entry and two solo ones."""
    now = datetime.now(UTC)
    jam = await _seed_jam(
        db_session,
        starts_at=now - timedelta(days=40),
        submissions_close_at=now - timedelta(days=8),
        voting_ends_at=now - timedelta(hours=1),
    )
    elves = await _seed_team(db_session, jam, "500", "1", "2", name="Elves <i>")
    db_session.add_all(
        [
            JamSubmission(
                jam_id=jam.id, thread_id="700", author_id="1", team_id=elves.id
            ),
            JamSubmission(jam_id=jam.id, thread_id="701", author_id="8"),
            JamSubmission(jam_id=jam.id, thread_id="702", author_id="9"),
            JamSubmission(jam_id=jam.id, thread_id="703", author_id="9"),
        ]
    )
    await db_session.commit()
    if counted:
        refusal = await results.save_votes(
            db_session,
            jam.id,
            [
                results.VoteCount("700", 12, 5),
                results.VoteCount("701", 12, 9),
                results.VoteCount("702", 3, 3),
                results.VoteCount("703", 99, 99),
            ],
            [],
        )
        assert refusal is None
    return jam


def _results_panel(html: str) -> str:
    start = html.index('<div class="sk-panel-head">Results</div>')
    return html[start : html.index('<div class="sk-panel-head">Scheduled Messages')]


async def test_detail_ranks_the_entries_once_votes_are_counted(db_session):
    jam = await _seed_voted_jam(db_session, counted=True)

    response, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    rows = response.context["results"]
    # Same reactions: the entry more people reacted to is first. The extra
    # post 703 is not ranked, whatever its count.
    assert [
        (row["rank"], row["thread_id"], row["reactions"], row["reactors"], row["tied"])
        for row in rows
    ] == [(1, "701", 12, 9, False), (2, "700", 12, 5, False), (3, "702", 3, 3, False)]
    panel = _results_panel(html)
    assert "<th>Reactions</th>" in panel and "<th>People who reacted</th>" in panel
    assert panel.index("<code>701</code>") < panel.index("<code>700</code>")
    assert "<code>703</code>" not in panel
    assert "Elves &lt;i&gt;" in panel and "<i>" not in panel
    assert f"https://discord.com/channels/{GUILD}/701" in panel
    assert "Tied" not in panel.split("</table>")[0]
    assert f'action="{_JAMS_URL}/{jam.id}/recount"' in panel
    assert "Counted " in panel


async def test_detail_marks_entries_level_on_both_counts_as_tied(db_session):
    jam = await _seed_voted_jam(db_session, counted=False)
    await results.save_votes(
        db_session,
        jam.id,
        [
            results.VoteCount("700", 4, 4),
            results.VoteCount("701", 4, 4),
            results.VoteCount("702", 1, 1),
        ],
        [],
    )

    response, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    assert [(row["rank"], row["tied"]) for row in response.context["results"]] == [
        (1, True),
        (1, True),
        (3, False),
    ]
    assert _results_panel(html).split("</table>")[0].count("Tied") == 2


async def test_detail_says_when_the_votes_will_be_counted(db_session):
    jam = await _seed_jam(db_session, voting_ends_at=START + timedelta(days=40))
    unset = await _seed_jam(
        db_session,
        setup=False,
        starts_at=START + timedelta(days=365),
        submissions_close_at=START + timedelta(days=398),
    )

    response, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)
    _, _, _, unset_html = await _call("jam_detail", db_session, jam_id=unset.id)

    assert response.context["results"] is None
    panel = _results_panel(html)
    assert "counts the votes when voting ends, 2027-01-10 00:00 UTC" in panel
    assert "recount" not in panel and "<table" not in panel
    assert "Voting end is not set" in _results_panel(unset_html)
    assert "Team threads locked" in html and "Not yet" in html


async def test_detail_says_the_count_is_under_way_after_voting_ends(db_session):
    jam = await _seed_voted_jam(db_session, counted=False)

    _, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    panel = _results_panel(html)
    assert "The bot is counting the votes" in panel
    assert "recount" not in panel and "<table" not in panel


async def test_detail_says_votes_wait_for_the_team_threads(db_session):
    now = datetime.now(UTC)
    jam = await _seed_jam(
        db_session,
        starts_at=now - timedelta(days=40),
        submissions_close_at=now - timedelta(days=2),
        voting_ends_at=now - timedelta(hours=1),
    )

    _, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    panel = _results_panel(html)
    assert "has not finished locking the team threads" in panel
    assert "7 days after submissions closed" in panel

    await results.mark_teams_closed(db_session, jam.id)
    _, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    assert "The bot is counting the votes" in _results_panel(html)
    assert "Not yet" not in html


async def test_a_counted_jam_with_no_entries_says_so(db_session):
    now = datetime.now(UTC)
    jam = await _seed_jam(
        db_session,
        starts_at=now - timedelta(days=40),
        submissions_close_at=now - timedelta(days=8),
        voting_ends_at=now - timedelta(hours=1),
    )
    await results.save_votes(db_session, jam.id, [], [])

    _, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    assert "No entries to rank." in _results_panel(html)


async def test_recount_drops_the_counts_so_the_bot_counts_again(db_session):
    jam = await _seed_voted_jam(db_session, counted=True)

    response, flashes, discord, _ = await _call(
        "jam_recount", db_session, jam_id=jam.id
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    assert "count the votes again" in flashes.success.call_args.args[1]
    assert discord.calls == []
    [work] = await results.list_work(db_session)
    assert work.count_votes is True
    rows = await teams.list_entries(db_session, jam.id)
    assert {row.submission.reaction_count for row in rows} == {None}
    _, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)
    assert "The bot is counting the votes" in _results_panel(html)


async def test_recount_without_a_valid_csrf_token_keeps_the_counts(db_session):
    jam = await _seed_voted_jam(db_session, counted=True)

    await _call("jam_recount", db_session, jam_id=jam.id, csrf=False)

    assert await results.list_work(db_session) == []
    [first, *_] = await results.list_results(db_session, jam.id)
    assert (first.thread_id, first.reactions) == ("701", 12)


async def test_recount_of_an_unknown_jam_goes_back_to_the_list(db_session):
    response, flashes, _, _ = await _call("jam_recount", db_session, jam_id=uuid4())

    assert _redirected(response, _JAMS_URL)
    flashes.error.assert_called_once()


async def test_remove_entry_only_reaches_the_jams_own_posts(db_session):
    jam = await _seed_jam(db_session)
    other = await _seed_jam(
        db_session,
        setup=False,
        starts_at=START + timedelta(days=365),
        submissions_close_at=START + timedelta(days=398),
    )
    db_session.add(JamSubmission(jam_id=other.id, thread_id="700", author_id="1"))
    await db_session.commit()

    _, flashes, _, _ = await _call(
        "jam_entry_remove", db_session, jam_id=jam.id, thread_id="700"
    )
    _, missing, _, _ = await _call(
        "jam_entry_remove", db_session, jam_id=uuid4(), thread_id="700"
    )

    assert flashes.error.call_args.args[1] == "Entry not found."
    assert missing.error.call_args.args[1] == "Entry not found."
    assert len(await teams.list_entries(db_session, other.id)) == 1


async def test_detail_before_any_setup_marks_everything_missing(db_session):
    jam = await _seed_jam(db_session, setup=False)

    response, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    assert all(part["id"] is None for part in response.context["discord_parts"])
    assert len(response.context["discord_parts"]) == 7
    assert html.count("Missing") == 7


async def test_detail_marks_a_stuck_message(db_session):
    jam = await _seed_jam(db_session, showcase_tag_id="1", year_tag_id="2")
    stuck = await _seed_message(
        db_session,
        jam,
        status="sending",
        claimed_at=datetime.now(UTC) - timedelta(minutes=11),
    )
    await _seed_message(db_session, jam, status="sending", claimed_at=datetime.now(UTC))

    response, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    assert response.context["stuck_ids"] == {stuck.id}
    assert response.context["setup_finished"] is True
    assert "Check setup" in html
    assert html.count("Send now") == 1


async def test_detail_of_a_missing_or_foreign_jam_is_404(db_session):
    foreign = await _seed_jam(db_session, guild_id="222222222222222222", setup=False)

    for jam_id in (uuid4(), foreign.id):
        response, _, _, _ = await _call("jam_detail", db_session, jam_id=jam_id)
        assert response.status_code == 404


# -- edit ---------------------------------------------------------------------


async def test_edit_form_is_prefilled(db_session):
    jam = await _seed_jam(db_session)

    _, _, _, html = await _call("jam_edit_form", db_session, jam_id=jam.id)

    assert 'value="Holiday Jam"' in html
    assert 'value="2026-12-01T00:00"' in html
    assert "Save Jam" in html


async def test_edit_saves_and_touches_nothing_in_discord(db_session):
    jam = await _seed_jam(db_session, showcase_tag_id="1", year_tag_id="2")

    response, flashes, discord, _ = await _call(
        "jam_edit",
        db_session,
        jam_id=jam.id,
        form={**_VALID_JAM, "name": "Renamed", "max_team_size": "3"},
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    await db_session.refresh(jam)
    assert (jam.name, jam.max_team_size) == ("Renamed", 3)
    assert (jam.showcase_tag_id, jam.year_tag_id) == ("1", "2")
    assert discord.calls == []
    flashes.error.assert_not_called()


async def test_edit_does_not_clash_with_itself(db_session):
    jam = await _seed_jam(db_session)

    response, _, _, _ = await _call(
        "jam_edit", db_session, jam_id=jam.id, form=_VALID_JAM
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")


async def test_edit_refuses_dates_that_overlap_another_jam(db_session):
    await _seed_jam(db_session, name="Earlier Jam")
    later = await _seed_jam(
        db_session,
        setup=False,
        name="Later",
        starts_at=START + timedelta(days=100),
        submissions_close_at=START + timedelta(days=120),
    )

    response, _, _, html = await _call(
        "jam_edit", db_session, jam_id=later.id, form=_VALID_JAM
    )

    assert response.status_code == 400
    assert "These dates overlap “Earlier Jam”" in html
    await db_session.refresh(later)
    assert later.name == "Later"


@pytest.mark.parametrize(
    ("change", "kept", "cleared"),
    [
        ({"tag_name": "Winter Jam"}, "year_tag_id", "showcase_tag_id"),
        (
            {
                "starts_at": "2027-01-01T00:00",
                "submissions_close_at": "2027-01-03T23:59",
            },
            "showcase_tag_id",
            "year_tag_id",
        ),
    ],
)
async def test_edit_that_changes_a_tag_asks_for_setup_again(
    db_session, change, kept, cleared
):
    jam = await _seed_jam(db_session, showcase_tag_id="1", year_tag_id="2")

    _, flashes, discord, _ = await _call(
        "jam_edit", db_session, jam_id=jam.id, form={**_VALID_JAM, **change}
    )

    await db_session.refresh(jam)
    assert getattr(jam, cleared) is None
    assert getattr(jam, kept) is not None
    assert "Finish setup" in flashes.error.call_args.args[1]
    assert discord.calls == []


async def test_edit_changing_only_the_tags_case_keeps_it(db_session):
    jam = await _seed_jam(db_session, showcase_tag_id="1", year_tag_id="2")

    await _call(
        "jam_edit",
        db_session,
        jam_id=jam.id,
        form={**_VALID_JAM, "tag_name": "holiday jam"},
    )

    await db_session.refresh(jam)
    assert jam.showcase_tag_id == "1"


# -- setup and delete ---------------------------------------------------------


async def test_setup_button_creates_what_is_missing(db_session):
    jam = await _seed_jam(db_session, setup=False)

    response, flashes, discord, _ = await _call("jam_setup", db_session, jam_id=jam.id)

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    await db_session.refresh(jam)
    assert jam.missing_tags == []
    assert (await store.get_setup(db_session, GUILD)).missing_parts == []
    flashes.success.assert_called_once()

    discord.calls.clear()
    await _call("jam_setup", db_session, jam_id=jam.id, discord=discord)
    assert discord.calls == []


async def test_setup_for_a_missing_jam_redirects_to_the_list(db_session):
    response, flashes, discord, _ = await _call("jam_setup", db_session, jam_id=uuid4())

    assert _redirected(response, _JAMS_URL)
    flashes.error.assert_called_once()
    assert discord.calls == []


async def test_delete_removes_the_jam_and_leaves_discord(db_session):
    jam = await _seed_jam(db_session)
    await _seed_message(db_session, jam)

    response, _, discord, _ = await _call("jam_delete", db_session, jam_id=jam.id)

    assert _redirected(response, _JAMS_URL)
    assert await _all_jams(db_session) == []
    assert await store.list_messages(db_session, jam.id) == []
    assert await store.get_setup(db_session, GUILD) is not None
    assert discord.calls == []


# -- messages -----------------------------------------------------------------


async def test_schedule_a_message(db_session):
    jam = await _seed_jam(db_session)

    response, _, _, _ = await _call(
        "jam_message_create", db_session, jam_id=jam.id, form=_VALID_MESSAGE
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    (message,) = await store.list_messages(db_session, jam.id)
    assert (message.channel_key, message.content, message.mention_role) == (
        "news",
        "One week left!",
        True,
    )
    assert message.status == "pending"


async def test_an_invalid_message_rerenders_the_jam_page(db_session):
    jam = await _seed_jam(db_session)

    response, _, _, html = await _call(
        "jam_message_create",
        db_session,
        jam_id=jam.id,
        form={**_VALID_MESSAGE, "send_at": ""},
    )

    assert response.status_code == 400
    assert "Send time is required" in html
    assert "One week left!" in html
    assert await store.list_messages(db_session, jam.id) == []


async def test_message_edit_form_and_save(db_session):
    jam = await _seed_jam(db_session)
    message = await _seed_message(db_session, jam)
    path = {"jam_id": jam.id, "message_id": message.id}

    _, _, _, html = await _call("jam_message_edit_form", db_session, **path)
    assert "The jam is open!" in html and "Save Message" in html

    response, _, _, _ = await _call(
        "jam_message_edit", db_session, form=_VALID_MESSAGE, **path
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    await db_session.refresh(message)
    assert (message.content, message.status) == ("One week left!", "pending")


async def test_editing_a_failed_message_queues_it_again(db_session):
    jam = await _seed_jam(db_session)
    message = await _seed_message(db_session, jam, status="failed", error="Too late")
    path = {"jam_id": jam.id, "message_id": message.id}

    _, _, _, html = await _call("jam_message_edit_form", db_session, **path)
    assert "Too late" in html

    await _call("jam_message_edit", db_session, form=_VALID_MESSAGE, **path)

    await db_session.refresh(message)
    assert (message.status, message.error) == ("pending", None)


async def test_saving_an_edit_never_requeues_a_claimed_message(db_session):
    jam = await _seed_jam(db_session)
    message = await _seed_message(db_session, jam)
    await store.claim_due_messages(db_session, START + timedelta(minutes=1))

    response, flashes, _, _ = await _call(
        "jam_message_edit",
        db_session,
        form=_VALID_MESSAGE,
        jam_id=jam.id,
        message_id=message.id,
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    flashes.error.assert_called_once()
    await db_session.refresh(message)
    assert (message.status, message.content) == ("sending", "The jam is open!")


@pytest.mark.parametrize("status", ["sending", "sent"])
async def test_a_sent_or_claimed_message_has_no_edit_form(db_session, status):
    jam = await _seed_jam(db_session)
    message = await _seed_message(db_session, jam, status=status)

    response, flashes, _, _ = await _call(
        "jam_message_edit_form", db_session, jam_id=jam.id, message_id=message.id
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    flashes.error.assert_called_once()


async def test_an_invalid_message_edit_rerenders(db_session):
    jam = await _seed_jam(db_session)
    message = await _seed_message(db_session, jam)

    response, _, _, html = await _call(
        "jam_message_edit",
        db_session,
        jam_id=jam.id,
        message_id=message.id,
        form={**_VALID_MESSAGE, "content": ""},
    )

    assert response.status_code == 400
    assert "Message is required" in html
    await db_session.refresh(message)
    assert message.content == "The jam is open!"


async def test_a_message_is_only_reachable_through_its_own_jam(db_session):
    jam = await _seed_jam(db_session)
    other = await _seed_jam(
        db_session,
        setup=False,
        starts_at=START + timedelta(days=100),
        submissions_close_at=START + timedelta(days=120),
    )
    message = await _seed_message(db_session, jam)
    path = {"jam_id": other.id, "message_id": message.id}

    response, _, _, _ = await _call("jam_message_edit_form", db_session, **path)
    assert response.status_code == 404
    response, _, _, _ = await _call(
        "jam_message_edit", db_session, form=_VALID_MESSAGE, **path
    )
    assert response.status_code == 404
    _, flashes, _, _ = await _call("jam_message_delete", db_session, **path)
    flashes.error.assert_called_once()
    _, flashes, _, _ = await _call("jam_message_retry", db_session, **path)
    flashes.error.assert_called_once()

    await db_session.refresh(message)
    assert message.content == "The jam is open!"


async def test_send_now_queues_a_failed_message(db_session):
    jam = await _seed_jam(db_session)
    message = await _seed_message(db_session, jam, status="failed", error="nope")

    response, flashes, _, _ = await _call(
        "jam_message_retry", db_session, jam_id=jam.id, message_id=message.id
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    flashes.success.assert_called_once()
    await db_session.refresh(message)
    assert message.status == "pending"


async def test_send_now_refuses_a_pending_message(db_session):
    jam = await _seed_jam(db_session)
    message = await _seed_message(db_session, jam)

    _, flashes, _, _ = await _call(
        "jam_message_retry", db_session, jam_id=jam.id, message_id=message.id
    )

    flashes.error.assert_called_once()
    await db_session.refresh(message)
    assert message.send_at.replace(tzinfo=UTC) == START


async def test_delete_a_message(db_session):
    jam = await _seed_jam(db_session)
    message = await _seed_message(db_session, jam)

    response, flashes, _, _ = await _call(
        "jam_message_delete", db_session, jam_id=jam.id, message_id=message.id
    )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    flashes.success.assert_called_once()
    assert await store.list_messages(db_session, jam.id) == []


# -- CSRF ---------------------------------------------------------------------

_POSTS = [
    "jams_showcase_forum",
    "jam_create",
    "jam_edit",
    "jam_setup",
    "jam_delete",
    "jam_message_create",
    "jam_message_edit",
    "jam_message_retry",
    "jam_message_delete",
    "jam_entry_remove",
    "jam_entry_restore",
    "jam_recount",
]


def test_the_csrf_list_covers_every_post_route():
    posts = {
        name
        for name, value in vars(JamsAdminController).items()
        if hasattr(value, "fn") and "POST" in value.http_methods
    }
    assert posts == set(_POSTS)


@pytest.mark.parametrize("handler", _POSTS)
async def test_a_post_without_a_valid_csrf_token_changes_nothing(db_session, handler):
    jam = await _seed_jam(db_session, setup=False)
    message = await _seed_message(db_session, jam, status="failed", error="nope")
    path = {}
    if handler not in ("jams_showcase_forum", "jam_create"):
        path["jam_id"] = jam.id
    if handler in ("jam_message_edit", "jam_message_retry", "jam_message_delete"):
        path["message_id"] = message.id
    if handler in ("jam_entry_remove", "jam_entry_restore"):
        path["thread_id"] = "700"
    db_session.add(JamSubmission(jam_id=jam.id, thread_id="700", author_id="1"))
    await db_session.commit()
    form = {**_VALID_JAM, **_VALID_MESSAGE, "name": "Changed", "channel_id": "14"}

    response, flashes, discord, _ = await _call(
        handler, db_session, form=form, csrf=False, **path
    )

    back = _JAMS_URL if "jam_id" not in path else f"{_JAMS_URL}/{jam.id}"
    assert _redirected(response, back)
    flashes.error.assert_called_once()
    assert "session expired" in flashes.error.call_args.args[1]
    flashes.success.assert_not_called()
    assert discord.calls == []
    assert [found.name for found in await _all_jams(db_session)] == [jam.name]
    assert await store.get_setup(db_session, GUILD) is None
    [kept] = await store.list_messages(db_session, jam.id)
    assert (kept.status, kept.content) == ("failed", message.content)
    assert len(await teams.list_entries(db_session, jam.id)) == 1


async def test_every_form_carries_skrifts_csrf_token(db_session):
    jam = await _seed_jam(db_session, voting_ends_at=START, votes_counted_at=START)
    message = await _seed_message(db_session, jam, status="failed", error="nope")
    db_session.add(JamSubmission(jam_id=jam.id, thread_id="700", author_id="1"))
    db_session.add(
        JamSubmission(jam_id=jam.id, thread_id="701", author_id="2", removed_at=START)
    )
    await db_session.commit()
    discord = _Guild()
    discord.add_channel("old-showcase", 15)
    pages = [
        await _call("jams_list", db_session, discord=discord),
        await _call("jam_create_form", db_session),
        await _call("jam_detail", db_session, jam_id=jam.id),
        await _call(
            "jam_message_edit_form", db_session, jam_id=jam.id, message_id=message.id
        ),
    ]

    forms = 0
    for _, _, _, html in pages:
        opened = html.count('<form method="post"')
        assert opened, html[:300]
        assert html.count('<input type="hidden" name="_csrf" value="') == opened
        forms += opened
    # list 1, create/edit 1, detail 8 (delete, setup, remove entry, restore
    # entry, count again, retry, delete, new), edit 1
    assert forms == 11


# -- two requests at once -----------------------------------------------------


class _SlowGuild(_Guild):
    """A guild whose every answer lets another request run first."""

    async def get_guild_channels(self, guild_id):
        await asyncio.sleep(0)
        return await super().get_guild_channels(guild_id)

    async def get_guild_roles(self, guild_id):
        await asyncio.sleep(0)
        return await super().get_guild_roles(guild_id)

    async def create_guild_channel(self, guild_id, payload, *, reason):
        await asyncio.sleep(0)
        return await super().create_guild_channel(guild_id, payload, reason=reason)

    async def create_guild_role(self, guild_id, name, *, reason):
        await asyncio.sleep(0)
        return await super().create_guild_role(guild_id, name, reason=reason)

    async def get_forum_tags(self, channel_id):
        await asyncio.sleep(0)
        return await super().get_forum_tags(channel_id)

    async def set_forum_tags(self, channel_id, tags, *, reason):
        await asyncio.sleep(0)
        return await super().set_forum_tags(channel_id, tags, reason=reason)


async def test_two_setup_runs_at_once_create_everything_once(db_session, test_engine):
    jam = await _seed_jam(db_session, setup=False)
    discord = _SlowGuild()

    await _call_twice_at_once(
        "jam_setup", db_session, test_engine, discord, jam_id=jam.id
    )

    assert len(discord.created("create_guild_channel")) == 4
    assert len(discord.created("create_guild_role")) == 1
    names = [channel.name for channel in discord.channels]
    assert len(names) == len(set(names)) == 4
    setup = await store.get_setup(db_session, GUILD)
    tags = [tag["name"] for tag in discord.tags[setup.showcase_channel_id]]
    assert sorted(tags) == ["2026", "Holiday Jam"]


async def test_a_double_clicked_create_makes_one_jam(db_session, test_engine):
    discord = _SlowGuild()

    responses = await _call_twice_at_once(
        "jam_create", db_session, test_engine, discord, form=_VALID_JAM
    )

    assert len(await _all_jams(db_session)) == 1
    assert sorted(response.status_code for response in responses)[-1] == 400
    assert len(discord.created("create_guild_channel")) == 4
    assert len(discord.created("create_guild_role")) == 1


# -- second review round ------------------------------------------------------


class _Unreachable(_Guild):
    async def get_guild(self, guild_id: str) -> DiscordGuildDetail:
        raise httpx.ConnectError("no route to Discord")


@pytest.mark.parametrize(
    ("handler", "kwargs"),
    [("jams_list", {}), ("jam_create", {"form": _VALID_JAM})],
)
async def test_an_unreachable_discord_is_a_503_page_not_a_crash(
    db_session, handler, kwargs
):
    # The error page is drawn by the shared campaigns helper.
    with (
        patch(f"{_CAMPAIGNS_MODULE}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{_CAMPAIGNS_MODULE}.get_flash_messages", return_value=[]),
    ):
        response, _, _, _ = await _call(
            handler, db_session, discord=_Unreachable(), **kwargs
        )

    assert response.status_code == 503
    assert "could not be reached" in response.context["error"]
    assert await _all_jams(db_session) == []


async def test_an_edit_that_lost_to_the_bot_says_nothing_was_saved(db_session):
    jam = await _seed_jam(db_session)
    message = await _seed_message(db_session, jam)

    with patch(f"{_MODULE}.store.save_message_edit", new=AsyncMock(return_value=False)):
        response, flashes, _, _ = await _call(
            "jam_message_edit",
            db_session,
            form=_VALID_MESSAGE,
            jam_id=jam.id,
            message_id=message.id,
        )

    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    flashes.error.assert_called_once()
    assert "Nothing was saved" in flashes.error.call_args.args[1]
    flashes.success.assert_not_called()


async def test_delete_waits_for_a_setup_run_on_the_same_server(db_session, test_engine):
    jam = await _seed_jam(db_session)
    async with AsyncSession(test_engine, expire_on_commit=False) as other:
        async with store.guild_lock(other, GUILD):
            delete = asyncio.create_task(_call("jam_delete", db_session, jam_id=jam.id))
            await asyncio.sleep(0.05)
            assert not delete.done()
        response, flashes, _, _ = await delete

    assert _redirected(response, _JAMS_URL)
    flashes.success.assert_called_once()
    assert await _all_jams(db_session) == []
