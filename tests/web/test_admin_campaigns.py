"""Tests for the Skrift admin campaigns/challenges controller."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from litestar.datastructures import FormMultiDict
from sqlalchemy import select

from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.bot_admin.campaigns import (
    CampaignsAdminController,
    next_challenge_position,
    parse_campaign_datetime,
    read_campaign_form,
    validate_campaign_form,
    validate_challenge_fields,
)
from smarter_dev.web.crud import CampaignOperations
from smarter_dev.web.discord_admin_client import (
    DiscordGuildDetail,
    GuildNotFoundError,
)
from smarter_dev.web.models import Campaign, Challenge


async def _challenge_positions(db_session, campaign_id) -> dict[str, int]:
    """Read a campaign's challenges by direct query.

    The shared test session uses ``expire_on_commit=False``, so a challenge
    added through a fresh CRUD call is not reflected in an already-loaded
    ``Campaign.challenges`` collection; a direct query is the source of truth.
    """
    result = await db_session.execute(
        select(Challenge).where(Challenge.campaign_id == campaign_id)
    )
    return {c.title: c.order_position for c in result.scalars().all()}

_GUILD = "111111111111111111"
_CHANNEL = "222222222222222222"
_MODULE = "smarter_dev.web.bot_admin.campaigns"


def _guild_detail() -> DiscordGuildDetail:
    return DiscordGuildDetail(
        id=_GUILD,
        name="Alpha Guild",
        icon=None,
        owner_id="owner",
        member_count=42,
        description=None,
    )


def _admin_client() -> SimpleNamespace:
    return SimpleNamespace(
        get_guild=AsyncMock(return_value=_guild_detail()),
        get_announcement_channels=AsyncMock(return_value=[]),
    )


def _form(pairs: list[tuple[str, str]]) -> FormMultiDict:
    return FormMultiDict(pairs)


def _future() -> str:
    moment = datetime.now(timezone.utc) + timedelta(days=3)
    return moment.strftime("%Y-%m-%dT%H:%M")


async def _seed_campaign(
    db_session,
    *,
    title: str = "Campaign One",
    is_active: bool = True,
) -> Campaign:
    campaign = Campaign(
        guild_id=_GUILD,
        title=title,
        description="A campaign.",
        start_time=datetime.now(timezone.utc) + timedelta(days=1),
        release_cadence_hours=24,
        announcement_channels=[_CHANNEL],
        is_active=is_active,
        created_by="admin",
    )
    db_session.add(campaign)
    await db_session.commit()
    await db_session.refresh(campaign)
    return campaign


# --- pure: parse_campaign_datetime -------------------------------------------


def test_parse_campaign_datetime_assumes_utc():
    parsed = parse_campaign_datetime("2026-08-01T12:30")
    assert parsed == datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)


def test_parse_campaign_datetime_rejects_blank():
    with pytest.raises(ValueError):
        parse_campaign_datetime("")


# --- pure: read_campaign_form ------------------------------------------------


def test_read_campaign_form_extracts_scalars_and_channels():
    data = read_campaign_form(
        _form(
            [
                ("title", "  My Campaign  "),
                ("description", "  Do things  "),
                ("start_time", "2026-08-01T12:30"),
                ("release_cadence_hours", "48"),
                ("announcement_channels", "111"),
                ("announcement_channels", "222"),
                ("is_active", "on"),
            ]
        )
    )
    assert data["title"] == "My Campaign"
    assert data["description"] == "Do things"
    assert data["start_time"] == "2026-08-01T12:30"
    assert data["release_cadence_hours"] == "48"
    assert data["announcement_channels"] == ["111", "222"]
    assert data["is_active"] is True


def test_read_campaign_form_defaults_for_blank_form():
    data = read_campaign_form(_form([]))
    assert data["title"] == ""
    assert data["release_cadence_hours"] == "24"
    assert data["announcement_channels"] == []
    assert data["is_active"] is False


# --- pure: validate_campaign_form --------------------------------------------


def _valid_raw() -> dict:
    return {
        "title": "My Campaign",
        "description": "Do things",
        "start_time": _future(),
        "release_cadence_hours": "24",
        "announcement_channels": ["111", "  ", ""],
        "is_active": True,
        "scheduled_message_title": "",
        "scheduled_message_description": "",
        "scheduled_message_time": "",
    }


def test_validate_happy_path_cleans_channels_and_types():
    ok, errors, cleaned = validate_campaign_form(_valid_raw(), require_future_start=True)
    assert ok is True
    assert errors == []
    assert cleaned["announcement_channels"] == ["111"]
    assert cleaned["release_cadence_hours"] == 24
    assert cleaned["start_time"].tzinfo is not None
    assert cleaned["is_active"] is True


def test_validate_does_not_mutate_input():
    raw = _valid_raw()
    validate_campaign_form(raw, require_future_start=True)
    assert raw["announcement_channels"] == ["111", "  ", ""]


def test_validate_requires_title_description_start_and_channels():
    raw = {
        "title": "  ",
        "description": "",
        "start_time": "",
        "release_cadence_hours": "24",
        "announcement_channels": [],
    }
    ok, errors, _ = validate_campaign_form(raw, require_future_start=True)
    assert ok is False
    assert any("Title is required" in e for e in errors)
    assert any("Description is required" in e for e in errors)
    assert any("Start time is required" in e for e in errors)
    assert any("announcement channel" in e for e in errors)


def test_validate_rejects_out_of_range_cadence():
    raw = _valid_raw()
    raw["release_cadence_hours"] = "500"
    ok, errors, _ = validate_campaign_form(raw, require_future_start=True)
    assert ok is False
    assert any("between 1 and 168" in e for e in errors)


def test_validate_rejects_non_numeric_cadence():
    raw = _valid_raw()
    raw["release_cadence_hours"] = "abc"
    ok, errors, _ = validate_campaign_form(raw, require_future_start=True)
    assert ok is False
    assert any("Invalid release cadence" in e for e in errors)


def test_validate_create_rejects_past_start():
    raw = _valid_raw()
    raw["start_time"] = "2000-01-01T00:00"
    ok, errors, _ = validate_campaign_form(raw, require_future_start=True)
    assert ok is False
    assert any("must be in the future" in e for e in errors)


def test_validate_edit_allows_past_start():
    raw = _valid_raw()
    raw["start_time"] = "2000-01-01T00:00"
    ok, errors, _ = validate_campaign_form(raw, require_future_start=False)
    assert ok is True
    assert errors == []


def test_validate_scheduled_message_time_requires_title():
    raw = _valid_raw()
    raw["scheduled_message_time"] = _future()
    ok, errors, _ = validate_campaign_form(raw, require_future_start=True)
    assert ok is False
    assert any("Scheduled message title is required" in e for e in errors)


# --- pure: challenge helpers -------------------------------------------------


def test_validate_challenge_fields_requires_both():
    errors = validate_challenge_fields("", "  ")
    assert any("title is required" in e.lower() for e in errors)
    assert any("description is required" in e.lower() for e in errors)


def test_validate_challenge_fields_accepts_valid():
    assert validate_challenge_fields("Day 1", "Solve it") == []


def test_next_challenge_position_empty_is_one():
    assert next_challenge_position([]) == 1


def test_next_challenge_position_after_max():
    challenges = [SimpleNamespace(order_position=2), SimpleNamespace(order_position=5)]
    assert next_challenge_position(challenges) == 6


# --- controller: list --------------------------------------------------------


async def test_list_renders_campaigns(db_session):
    await _seed_campaign(db_session, title="Alpha")
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await CampaignsAdminController.campaigns_list.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD, page=1
        )

    assert response.template_name == "admin/bot/campaigns/list.html"
    assert response.context["active_page"] == "campaigns"
    assert [c.title for c in response.context["campaigns"]] == ["Alpha"]
    assert response.context["total_count"] == 1


async def test_list_guild_not_found_returns_404(db_session):
    client = SimpleNamespace(get_guild=AsyncMock(side_effect=GuildNotFoundError("x")))
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=client):
        response = await CampaignsAdminController.campaigns_list.fn(
            None, request=object(), db_session=db_session, guild_id="missing", page=1
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


# --- controller: create ------------------------------------------------------


async def test_create_get_renders_blank_form(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await CampaignsAdminController.campaign_create_form.fn(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/campaigns/create.html"
    assert response.context["form_data"] is None


async def _run_create(db_session, form):
    request = SimpleNamespace(form=AsyncMock(return_value=form))
    flash_success, flash_error = Mock(), Mock()
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_success", flash_success), patch(
        f"{_MODULE}.flash_error", flash_error
    ):
        response = await CampaignsAdminController.campaign_create.fn(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )
    return response, flash_success, flash_error


async def test_create_post_persists_and_redirects(db_session):
    response, flash_success, _ = await _run_create(
        db_session,
        _form(
            [
                ("title", "Bravo"),
                ("description", "The Bravo campaign"),
                ("start_time", _future()),
                ("release_cadence_hours", "24"),
                ("announcement_channels", _CHANNEL),
            ]
        ),
    )

    assert response.status_code in (302, 303, 307)
    assert response.url == f"/admin/bot/guilds/{_GUILD}/campaigns"
    flash_success.assert_called_once()
    campaigns, _ = await CampaignOperations(db_session).get_campaigns_by_guild(_GUILD)
    assert [c.title for c in campaigns] == ["Bravo"]


async def test_create_post_invalid_rerenders_400(db_session):
    response, _, _ = await _run_create(
        db_session,
        _form([("title", ""), ("description", "")]),
    )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/campaigns/create.html"
    assert response.context["errors"]
    assert response.context["form_data"]["title"] == ""
    campaigns, _ = await CampaignOperations(db_session).get_campaigns_by_guild(_GUILD)
    assert campaigns == []


async def test_create_post_duplicate_title_rerenders_400(db_session):
    await _seed_campaign(db_session, title="Dup")
    response, _, _ = await _run_create(
        db_session,
        _form(
            [
                ("title", "Dup"),
                ("description", "Another"),
                ("start_time", _future()),
                ("release_cadence_hours", "24"),
                ("announcement_channels", _CHANNEL),
            ]
        ),
    )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/campaigns/create.html"
    assert response.context["errors"]


# --- controller: edit --------------------------------------------------------


async def test_edit_get_renders_prefilled(db_session):
    campaign = await _seed_campaign(db_session, title="Editable")
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await CampaignsAdminController.campaign_edit_form.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
        )

    assert response.template_name == "admin/bot/campaigns/edit.html"
    assert response.context["campaign"].id == campaign.id


async def test_edit_get_missing_campaign_returns_404(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await CampaignsAdminController.campaign_edit_form.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=uuid4(),
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


async def test_edit_post_updates_and_redirects(db_session):
    campaign = await _seed_campaign(db_session, title="Before")
    request = SimpleNamespace(
        form=AsyncMock(
            return_value=_form(
                [
                    ("title", "After"),
                    ("description", "Updated"),
                    ("start_time", "2000-01-01T00:00"),
                    ("release_cadence_hours", "48"),
                    ("announcement_channels", _CHANNEL),
                    ("is_active", "on"),
                ]
            )
        )
    )
    flash_success = Mock()
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_success", flash_success):
        response = await CampaignsAdminController.campaign_edit.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
        )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    refreshed = await CampaignOperations(db_session).get_campaign_by_id(
        campaign.id, _GUILD
    )
    assert refreshed.title == "After"
    assert refreshed.release_cadence_hours == 48


async def test_edit_post_invalid_rerenders_400(db_session):
    campaign = await _seed_campaign(db_session, title="Keep")
    request = SimpleNamespace(
        form=AsyncMock(return_value=_form([("title", ""), ("description", "")]))
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await CampaignsAdminController.campaign_edit.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
        )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/campaigns/edit.html"
    assert response.context["errors"]


# --- controller: delete ------------------------------------------------------


async def test_delete_deactivates_campaign(db_session):
    campaign = await _seed_campaign(db_session, title="Doomed", is_active=True)
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await CampaignsAdminController.campaign_delete.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
        )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    refreshed = await CampaignOperations(db_session).get_campaign_by_id(
        campaign.id, _GUILD
    )
    assert refreshed.is_active is False


async def test_delete_missing_flashes_error(db_session):
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error):
        response = await CampaignsAdminController.campaign_delete.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=uuid4(),
        )

    assert response.status_code in (302, 303, 307)
    flash_error.assert_called_once()


# --- controller: challenges --------------------------------------------------


async def test_challenges_renders_list(db_session):
    campaign = await _seed_campaign(db_session, title="WithChallenges")
    await CampaignOperations(db_session).create_challenge(
        campaign_id=campaign.id,
        title="Day 1",
        description="Solve it",
        order_position=1,
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await CampaignsAdminController.campaign_challenges.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
        )

    assert response.template_name == "admin/bot/campaigns/challenges.html"
    assert [c.title for c in response.context["challenges"]] == ["Day 1"]


async def test_challenges_missing_campaign_returns_404(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await CampaignsAdminController.campaign_challenges.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=uuid4(),
        )

    assert response.status_code == 404


# --- controller: challenge create --------------------------------------------


async def test_challenge_create_get_renders_form(db_session):
    campaign = await _seed_campaign(db_session, title="Host")
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await CampaignsAdminController.challenge_create_form.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
        )

    assert response.template_name == "admin/bot/campaigns/challenge_create.html"
    assert response.context["form_data"] is None


async def test_challenge_create_post_persists_and_redirects(db_session):
    campaign = await _seed_campaign(db_session, title="Host2")
    await CampaignOperations(db_session).create_challenge(
        campaign_id=campaign.id, title="Day 1", description="First", order_position=1
    )
    request = SimpleNamespace(
        form=AsyncMock(
            return_value=_form(
                [("title", "Day 2"), ("description", "Second challenge")]
            )
        )
    )
    flash_success = Mock()
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_success", flash_success):
        response = await CampaignsAdminController.challenge_create.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
        )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    assert await _challenge_positions(db_session, campaign.id) == {
        "Day 1": 1,
        "Day 2": 2,
    }


async def test_challenge_create_post_invalid_rerenders_400(db_session):
    campaign = await _seed_campaign(db_session, title="Host3")
    request = SimpleNamespace(
        form=AsyncMock(return_value=_form([("title", ""), ("description", "")]))
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()):
        response = await CampaignsAdminController.challenge_create.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
        )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/campaigns/challenge_create.html"
    assert response.context["errors"]
    assert await _challenge_positions(db_session, campaign.id) == {}


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        CampaignsAdminController.campaigns_list,
        CampaignsAdminController.campaign_create_form,
        CampaignsAdminController.campaign_create,
        CampaignsAdminController.campaign_edit_form,
        CampaignsAdminController.campaign_edit,
        CampaignsAdminController.campaign_delete,
        CampaignsAdminController.campaign_challenges,
        CampaignsAdminController.challenge_create_form,
        CampaignsAdminController.challenge_create,
    ],
)
def test_routes_require_admin(handler):
    guards = handler.guards
    assert auth_guard in guards
    admin_guards = [
        g
        for g in guards
        if isinstance(g, Permission) and g.permission == "administrator"
    ]
    assert admin_guards, "route must require the administrator permission"


async def test_administrator_permission_denies_non_admin_and_allows_admin():
    guard = Permission("administrator")
    non_admin = SimpleNamespace(permissions={"view-drafts"}, roles=set())
    admin = SimpleNamespace(permissions={"administrator"}, roles=set())

    assert await guard.check(non_admin) is False
    assert await guard.check(admin) is True
