"""Tests for the code jam admin pages."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4

import jinja2
import pytest
from litestar.datastructures import FormMultiDict
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard

from smarter_dev.web.bot_admin.jams import MESSAGE_CONTENT_MAX
from smarter_dev.web.bot_admin.jams import JamsAdminController
from smarter_dev.web.bot_admin.jams import read_jam_form
from smarter_dev.web.bot_admin.jams import read_jam_message_form
from smarter_dev.web.bot_admin.jams import validate_jam_form
from smarter_dev.web.bot_admin.jams import validate_jam_message_form
from smarter_dev.web.discord_admin_client import DiscordGuildDetail
from smarter_dev.web.jams import store
from smarter_dev.web.models import Jam
from smarter_dev.web.models import JamMessage
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
    return env.get_template(response.template_name).render(**response.context)


async def _call(handler: str, db_session, *, form=None, discord=None, **path):
    """Run one controller handler; returns (response, flashes, discord, html)."""
    discord = discord or _Guild()
    flashes = SimpleNamespace(success=Mock(), error=Mock())
    request = SimpleNamespace(form=AsyncMock(return_value=_form(form or {})))
    with (
        patch(f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})),
        patch(f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=discord),
        patch(f"{_MODULE}.get_admin_discord_client", return_value=discord),
        patch(f"{_MODULE}.flash_success", flashes.success),
        patch(f"{_MODULE}.flash_error", flashes.error),
        patch(f"{_MODULE}.get_flash_messages", return_value=[]),
    ):
        response = await getattr(JamsAdminController, handler).fn(
            None, request=request, db_session=db_session, guild_id=GUILD, **path
        )
    html = _render(response) if hasattr(response, "template_name") else None
    return response, flashes, discord, html


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
    assert len(handlers) == 14
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
    assert len(discord.created("create_guild_channel")) == 5
    flashes.success.assert_called_once()
    flashes.error.assert_not_called()
    assert "created" in flashes.success.call_args.args[1]


async def test_create_keeps_the_jam_when_discord_setup_fails(db_session):
    discord = _Guild()
    discord.fail["create_guild_channel:jam-teams"] = discord_error(403)

    response, flashes, _, _ = await _call(
        "jam_create", db_session, form=_VALID_JAM, discord=discord
    )

    (jam,) = await _all_jams(db_session)
    assert _redirected(response, f"{_JAMS_URL}/{jam.id}")
    setup = await store.get_setup(db_session, GUILD)
    assert setup.missing_parts == ["teams"]
    assert setup.news_channel_id is not None
    message = flashes.error.call_args.args[1]
    assert "#jam-teams: the bot does not have the Manage Channels permission." in (
        message
    )
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


async def test_detail_before_any_setup_marks_everything_missing(db_session):
    jam = await _seed_jam(db_session, setup=False)

    response, _, _, html = await _call("jam_detail", db_session, jam_id=jam.id)

    assert all(part["id"] is None for part in response.context["discord_parts"])
    assert len(response.context["discord_parts"]) == 8
    assert html.count("Missing") == 8


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
