"""Tests for the Skrift admin scheduled-messages controller."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from litestar.datastructures import FormMultiDict

from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.bot_admin.scheduled_messages import (
    ScheduledMessagesAdminController,
    read_scheduled_message_form,
    validate_scheduled_message_form,
)
from smarter_dev.web.crud import ScheduledMessageOperations
from smarter_dev.web.discord_admin_client import (
    DiscordGuildDetail,
    GuildNotFoundError,
)
from smarter_dev.web.models import Campaign, ScheduledMessage

_GUILD = "111111111111111111"
_CHANNEL = "222222222222222222"
_MODULE = "smarter_dev.web.bot_admin.scheduled_messages"
# The controller shares guild resolution with the campaigns module, so the
# Discord client is patched where those helpers actually look it up.
_CAMPAIGNS_MODULE = "smarter_dev.web.bot_admin.campaigns"


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


async def _seed_campaign(db_session, *, title: str = "Campaign One") -> Campaign:
    campaign = Campaign(
        guild_id=_GUILD,
        title=title,
        description="A campaign.",
        start_time=datetime.now(timezone.utc) + timedelta(days=1),
        release_cadence_hours=24,
        announcement_channels=[_CHANNEL],
        is_active=True,
        created_by="admin",
    )
    db_session.add(campaign)
    await db_session.commit()
    await db_session.refresh(campaign)
    return campaign


async def _seed_message(
    db_session,
    campaign_id,
    *,
    title: str = "Kickoff",
    is_sent: bool = False,
) -> ScheduledMessage:
    message = ScheduledMessage(
        campaign_id=campaign_id,
        title=title,
        description="Body text",
        announcement_channel_message=None,
        scheduled_time=datetime.now(timezone.utc) + timedelta(days=2),
        is_sent=is_sent,
        created_by="admin",
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)
    return message


# --- pure: read_scheduled_message_form ---------------------------------------


def test_read_form_strips_scalars():
    data = read_scheduled_message_form(
        _form(
            [
                ("title", "  Launch  "),
                ("description", "  Hello  "),
                ("announcement_channel_message", "  Announce  "),
                ("scheduled_time", "2026-08-01T12:30"),
            ]
        )
    )
    assert data == {
        "title": "Launch",
        "description": "Hello",
        "announcement_channel_message": "Announce",
        "scheduled_time": "2026-08-01T12:30",
    }


def test_read_form_defaults_for_blank_form():
    data = read_scheduled_message_form(_form([]))
    assert data == {
        "title": "",
        "description": "",
        "announcement_channel_message": "",
        "scheduled_time": "",
    }


# --- pure: validate_scheduled_message_form -----------------------------------


def _valid_raw() -> dict:
    return {
        "title": "Launch",
        "description": "Hello world",
        "announcement_channel_message": "",
        "scheduled_time": _future(),
    }


def test_validate_happy_path_types_and_collapses_optional():
    ok, errors, cleaned = validate_scheduled_message_form(
        _valid_raw(), require_future=True
    )
    assert ok is True
    assert errors == []
    assert cleaned["title"] == "Launch"
    assert cleaned["description"] == "Hello world"
    assert cleaned["announcement_channel_message"] is None
    assert cleaned["scheduled_time"].tzinfo is not None


def test_validate_keeps_optional_announcement_message():
    raw = _valid_raw()
    raw["announcement_channel_message"] = "Campaign channel copy"
    ok, _, cleaned = validate_scheduled_message_form(raw, require_future=True)
    assert ok is True
    assert cleaned["announcement_channel_message"] == "Campaign channel copy"


def test_validate_does_not_mutate_input():
    raw = _valid_raw()
    validate_scheduled_message_form(raw, require_future=True)
    assert raw["announcement_channel_message"] == ""


def test_validate_requires_title_description_and_time():
    ok, errors, _ = validate_scheduled_message_form(
        {"title": "  ", "description": "", "scheduled_time": ""},
        require_future=True,
    )
    assert ok is False
    assert any("Title is required" in e for e in errors)
    assert any("Description is required" in e for e in errors)
    assert any("Scheduled time is required" in e for e in errors)


def test_validate_rejects_bad_time_format():
    raw = _valid_raw()
    raw["scheduled_time"] = "not-a-date"
    ok, errors, _ = validate_scheduled_message_form(raw, require_future=True)
    assert ok is False
    assert any("Invalid scheduled time format" in e for e in errors)


def test_validate_create_rejects_past_time():
    raw = _valid_raw()
    raw["scheduled_time"] = "2000-01-01T00:00"
    ok, errors, _ = validate_scheduled_message_form(raw, require_future=True)
    assert ok is False
    assert any("must be in the future" in e for e in errors)


def test_validate_sent_message_allows_past_time():
    raw = _valid_raw()
    raw["scheduled_time"] = "2000-01-01T00:00"
    ok, errors, _ = validate_scheduled_message_form(raw, require_future=False)
    assert ok is True
    assert errors == []


# --- controller: list --------------------------------------------------------


async def test_list_renders_messages(db_session):
    campaign = await _seed_campaign(db_session)
    await _seed_message(db_session, campaign.id, title="Kickoff")
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await ScheduledMessagesAdminController.scheduled_messages_list.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
        )

    assert response.template_name == "admin/bot/scheduled_messages/list.html"
    assert response.context["active_page"] == "campaigns"
    assert [m.title for m in response.context["scheduled_messages"]] == ["Kickoff"]


async def test_list_guild_not_found_returns_404(db_session):
    campaign = await _seed_campaign(db_session)
    client = SimpleNamespace(get_guild=AsyncMock(side_effect=GuildNotFoundError("x")))
    with patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=client):
        response = await ScheduledMessagesAdminController.scheduled_messages_list.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id="missing",
            campaign_id=campaign.id,
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


async def test_list_missing_campaign_returns_404(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ):
        response = await ScheduledMessagesAdminController.scheduled_messages_list.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=uuid4(),
        )

    assert response.status_code == 404


# --- controller: create ------------------------------------------------------


async def test_create_get_renders_blank_form(db_session):
    campaign = await _seed_campaign(db_session)
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ):
        response = await (
            ScheduledMessagesAdminController.scheduled_message_create_form.fn(
                None,
                request=object(),
                db_session=db_session,
                guild_id=_GUILD,
                campaign_id=campaign.id,
            )
        )

    assert response.template_name == "admin/bot/scheduled_messages/create.html"
    assert response.context["form_data"] is None


async def _run_create(db_session, campaign_id, form):
    request = SimpleNamespace(form=AsyncMock(return_value=form))
    flash_success, flash_error = Mock(), Mock()
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_success", flash_success), patch(
        f"{_MODULE}.flash_error", flash_error
    ):
        response = await ScheduledMessagesAdminController.scheduled_message_create.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign_id,
        )
    return response, flash_success, flash_error


async def test_create_post_persists_and_redirects(db_session):
    campaign = await _seed_campaign(db_session)
    response, flash_success, _ = await _run_create(
        db_session,
        campaign.id,
        _form(
            [
                ("title", "Launch Day"),
                ("description", "The launch"),
                ("scheduled_time", _future()),
            ]
        ),
    )

    assert response.status_code in (302, 303, 307)
    assert response.url == (
        f"/admin/bot/guilds/{_GUILD}/campaigns/{campaign.id}/scheduled-messages"
    )
    flash_success.assert_called_once()
    messages = await ScheduledMessageOperations(
        db_session
    ).get_scheduled_messages_by_campaign(campaign.id)
    assert [m.title for m in messages] == ["Launch Day"]


async def test_create_post_invalid_rerenders_400(db_session):
    campaign = await _seed_campaign(db_session)
    response, _, _ = await _run_create(
        db_session,
        campaign.id,
        _form([("title", ""), ("description", ""), ("scheduled_time", "")]),
    )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/scheduled_messages/create.html"
    assert response.context["errors"]
    assert response.context["form_data"]["title"] == ""
    messages = await ScheduledMessageOperations(
        db_session
    ).get_scheduled_messages_by_campaign(campaign.id)
    assert messages == []


async def test_create_post_missing_campaign_returns_404(db_session):
    response, _, _ = await _run_create(
        db_session,
        uuid4(),
        _form(
            [
                ("title", "Launch"),
                ("description", "Body"),
                ("scheduled_time", _future()),
            ]
        ),
    )

    assert response.status_code == 404


# --- controller: edit --------------------------------------------------------


async def test_edit_get_renders_prefilled(db_session):
    campaign = await _seed_campaign(db_session)
    message = await _seed_message(db_session, campaign.id, title="Editable")
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ):
        response = await (
            ScheduledMessagesAdminController.scheduled_message_edit_form.fn(
                None,
                request=object(),
                db_session=db_session,
                guild_id=_GUILD,
                campaign_id=campaign.id,
                message_id=message.id,
            )
        )

    assert response.template_name == "admin/bot/scheduled_messages/edit.html"
    assert response.context["scheduled_message"].id == message.id


async def test_edit_get_missing_message_returns_404(db_session):
    campaign = await _seed_campaign(db_session)
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ):
        response = await (
            ScheduledMessagesAdminController.scheduled_message_edit_form.fn(
                None,
                request=object(),
                db_session=db_session,
                guild_id=_GUILD,
                campaign_id=campaign.id,
                message_id=uuid4(),
            )
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


async def _run_edit(db_session, campaign_id, message_id, form):
    request = SimpleNamespace(form=AsyncMock(return_value=form))
    flash_success = Mock()
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_success", flash_success):
        response = await ScheduledMessagesAdminController.scheduled_message_edit.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign_id,
            message_id=message_id,
        )
    return response, flash_success


async def test_edit_post_updates_and_redirects(db_session):
    campaign = await _seed_campaign(db_session)
    message = await _seed_message(db_session, campaign.id, title="Before")
    response, flash_success = await _run_edit(
        db_session,
        campaign.id,
        message.id,
        _form(
            [
                ("title", "After"),
                ("description", "Updated body"),
                ("announcement_channel_message", "Now with a channel copy"),
                ("scheduled_time", _future()),
            ]
        ),
    )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    refreshed = await ScheduledMessageOperations(
        db_session
    ).get_scheduled_message_by_id(message.id, campaign.id)
    assert refreshed.title == "After"
    assert refreshed.announcement_channel_message == "Now with a channel copy"


async def test_edit_post_invalid_rerenders_400(db_session):
    campaign = await _seed_campaign(db_session)
    message = await _seed_message(db_session, campaign.id, title="Keep")
    response, _ = await _run_edit(
        db_session,
        campaign.id,
        message.id,
        _form([("title", ""), ("description", ""), ("scheduled_time", "")]),
    )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/scheduled_messages/edit.html"
    assert response.context["errors"]
    refreshed = await ScheduledMessageOperations(
        db_session
    ).get_scheduled_message_by_id(message.id, campaign.id)
    assert refreshed.title == "Keep"


async def test_edit_post_sent_message_allows_past_time(db_session):
    campaign = await _seed_campaign(db_session)
    message = await _seed_message(
        db_session, campaign.id, title="AlreadySent", is_sent=True
    )
    response, flash_success = await _run_edit(
        db_session,
        campaign.id,
        message.id,
        _form(
            [
                ("title", "Corrected"),
                ("description", "Fixed body"),
                ("scheduled_time", "2000-01-01T00:00"),
            ]
        ),
    )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    refreshed = await ScheduledMessageOperations(
        db_session
    ).get_scheduled_message_by_id(message.id, campaign.id)
    assert refreshed.title == "Corrected"


# --- controller: delete ------------------------------------------------------


async def test_delete_removes_message(db_session):
    campaign = await _seed_campaign(db_session)
    message = await _seed_message(db_session, campaign.id, title="Doomed")
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await ScheduledMessagesAdminController.scheduled_message_delete.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
            message_id=message.id,
        )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    remaining = await ScheduledMessageOperations(
        db_session
    ).get_scheduled_messages_by_campaign(campaign.id)
    assert remaining == []


async def test_delete_missing_flashes_error(db_session):
    campaign = await _seed_campaign(db_session)
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error):
        response = await ScheduledMessagesAdminController.scheduled_message_delete.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            campaign_id=campaign.id,
            message_id=uuid4(),
        )

    assert response.status_code in (302, 303, 307)
    flash_error.assert_called_once()


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        ScheduledMessagesAdminController.scheduled_messages_list,
        ScheduledMessagesAdminController.scheduled_message_create_form,
        ScheduledMessagesAdminController.scheduled_message_create,
        ScheduledMessagesAdminController.scheduled_message_edit_form,
        ScheduledMessagesAdminController.scheduled_message_edit,
        ScheduledMessagesAdminController.scheduled_message_delete,
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
