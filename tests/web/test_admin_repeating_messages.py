"""Tests for the Skrift admin repeating-messages controller."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from litestar.datastructures import FormMultiDict

from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.bot_admin.repeating_messages import (
    RepeatingMessagesAdminController,
    read_repeating_message_form,
    validate_repeating_message_form,
)
from smarter_dev.web.crud import RepeatingMessageOperations
from smarter_dev.web.discord_admin_client import (
    DiscordGuildDetail,
    GuildNotFoundError,
)
from smarter_dev.web.models import RepeatingMessage

_GUILD = "111111111111111111"
_CHANNEL = "222222222222222222"
_ROLE = "333333333333333333"
_MODULE = "smarter_dev.web.bot_admin.repeating_messages"
# Guild resolution is shared with the campaigns module, so the Discord client is
# patched where the shared helper actually looks it up.
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
        get_guild_roles=AsyncMock(return_value=[]),
    )


def _form(pairs: list[tuple[str, str]]) -> FormMultiDict:
    return FormMultiDict(pairs)


def _future() -> str:
    moment = datetime.now(timezone.utc) + timedelta(days=3)
    return moment.strftime("%Y-%m-%dT%H:%M")


async def _seed_message(
    db_session,
    *,
    guild_id: str = _GUILD,
    channel_id: str = _CHANNEL,
    content: str = "Hello world",
    interval_minutes: int = 60,
    is_active: bool = True,
) -> RepeatingMessage:
    message = RepeatingMessage(
        guild_id=guild_id,
        channel_id=channel_id,
        message_content=content,
        role_id=None,
        start_time=datetime.now(timezone.utc) + timedelta(days=1),
        interval_minutes=interval_minutes,
        is_active=is_active,
        created_by="admin",
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)
    return message


# --- pure: read_repeating_message_form ---------------------------------------


def test_read_form_strips_scalars():
    data = read_repeating_message_form(
        _form(
            [
                ("channel_id", "  123  "),
                ("message_content", "  Hello  "),
                ("role_id", "  456  "),
                ("start_time", "2026-08-01T12:30"),
                ("interval_minutes", "  30  "),
            ]
        )
    )
    assert data == {
        "channel_id": "123",
        "message_content": "Hello",
        "role_id": "456",
        "start_time": "2026-08-01T12:30",
        "interval_minutes": "30",
    }


def test_read_form_defaults_for_blank_form():
    data = read_repeating_message_form(_form([]))
    assert data == {
        "channel_id": "",
        "message_content": "",
        "role_id": "",
        "start_time": "",
        "interval_minutes": "",
    }


# --- pure: validate_repeating_message_form -----------------------------------


def _valid_raw() -> dict:
    return {
        "channel_id": _CHANNEL,
        "message_content": "Repeat me",
        "role_id": "",
        "start_time": _future(),
        "interval_minutes": "60",
    }


def test_validate_happy_path_types_and_collapses_optional():
    ok, errors, cleaned = validate_repeating_message_form(_valid_raw())
    assert ok is True
    assert errors == []
    assert cleaned["channel_id"] == _CHANNEL
    assert cleaned["message_content"] == "Repeat me"
    assert cleaned["role_id"] is None
    assert cleaned["interval_minutes"] == 60
    assert cleaned["start_time"].tzinfo is not None


def test_validate_keeps_optional_role():
    raw = _valid_raw()
    raw["role_id"] = _ROLE
    ok, _, cleaned = validate_repeating_message_form(raw)
    assert ok is True
    assert cleaned["role_id"] == _ROLE


def test_validate_does_not_mutate_input():
    raw = _valid_raw()
    validate_repeating_message_form(raw)
    assert raw["role_id"] == ""
    assert raw["interval_minutes"] == "60"


def test_validate_requires_channel_content_time_and_interval():
    ok, errors, _ = validate_repeating_message_form(
        {
            "channel_id": "",
            "message_content": "  ",
            "role_id": "",
            "start_time": "",
            "interval_minutes": "",
        }
    )
    assert ok is False
    assert any("Channel is required" in e for e in errors)
    assert any("Message content is required" in e for e in errors)
    assert any("Start time is required" in e for e in errors)
    assert any("Interval is required" in e for e in errors)


def test_validate_rejects_bad_time_format():
    raw = _valid_raw()
    raw["start_time"] = "not-a-date"
    ok, errors, _ = validate_repeating_message_form(raw)
    assert ok is False
    assert any("Invalid start time format" in e for e in errors)


def test_validate_rejects_non_numeric_interval():
    raw = _valid_raw()
    raw["interval_minutes"] = "soon"
    ok, errors, _ = validate_repeating_message_form(raw)
    assert ok is False
    assert any("whole number" in e for e in errors)


def test_validate_rejects_interval_below_one():
    raw = _valid_raw()
    raw["interval_minutes"] = "0"
    ok, errors, _ = validate_repeating_message_form(raw)
    assert ok is False
    assert any("at least 1 minute" in e for e in errors)


# --- controller: list --------------------------------------------------------


async def test_list_renders_messages(db_session):
    await _seed_message(db_session, content="Standup reminder")
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await RepeatingMessagesAdminController.repeating_messages_list.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
        )

    assert response.template_name == "admin/bot/repeating_messages/list.html"
    assert response.context["active_page"] == "repeating_messages"
    assert [m.message_content for m in response.context["messages"]] == [
        "Standup reminder"
    ]


async def test_list_guild_not_found_returns_404(db_session):
    client = SimpleNamespace(get_guild=AsyncMock(side_effect=GuildNotFoundError("x")))
    with patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=client):
        response = await RepeatingMessagesAdminController.repeating_messages_list.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id="missing",
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


# --- controller: create ------------------------------------------------------


async def test_create_get_renders_blank_form(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ):
        response = await (
            RepeatingMessagesAdminController.repeating_message_create_form.fn(
                None,
                request=object(),
                db_session=db_session,
                guild_id=_GUILD,
            )
        )

    assert response.template_name == "admin/bot/repeating_messages/create.html"
    assert response.context["form_data"] is None


async def _run_create(db_session, form):
    request = SimpleNamespace(form=AsyncMock(return_value=form))
    flash_success, flash_error = Mock(), Mock()
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_success", flash_success), patch(
        f"{_MODULE}.flash_error", flash_error
    ):
        response = await RepeatingMessagesAdminController.repeating_message_create.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=_GUILD,
        )
    return response, flash_success, flash_error


async def test_create_post_persists_and_redirects(db_session):
    response, flash_success, _ = await _run_create(
        db_session,
        _form(
            [
                ("channel_id", _CHANNEL),
                ("message_content", "Daily standup"),
                ("start_time", _future()),
                ("interval_minutes", "1440"),
            ]
        ),
    )

    assert response.status_code in (302, 303, 307)
    assert response.url == f"/admin/bot/guilds/{_GUILD}/repeating-messages"
    flash_success.assert_called_once()
    messages = await RepeatingMessageOperations(
        db_session
    ).get_guild_repeating_messages(_GUILD)
    assert [m.message_content for m in messages] == ["Daily standup"]
    assert messages[0].interval_minutes == 1440
    assert messages[0].next_send_time == messages[0].start_time


async def test_create_post_invalid_rerenders_400(db_session):
    response, _, _ = await _run_create(
        db_session,
        _form(
            [
                ("channel_id", ""),
                ("message_content", ""),
                ("start_time", ""),
                ("interval_minutes", ""),
            ]
        ),
    )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/repeating_messages/create.html"
    assert response.context["errors"]
    assert response.context["form_data"]["channel_id"] == ""
    messages = await RepeatingMessageOperations(
        db_session
    ).get_guild_repeating_messages(_GUILD)
    assert messages == []


# --- controller: edit --------------------------------------------------------


async def test_edit_get_renders_prefilled(db_session):
    message = await _seed_message(db_session, content="Editable")
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ):
        response = await (
            RepeatingMessagesAdminController.repeating_message_edit_form.fn(
                None,
                request=object(),
                db_session=db_session,
                guild_id=_GUILD,
                message_id=message.id,
            )
        )

    assert response.template_name == "admin/bot/repeating_messages/edit.html"
    assert response.context["message"].id == message.id


async def test_edit_get_missing_message_returns_404(db_session):
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ):
        response = await (
            RepeatingMessagesAdminController.repeating_message_edit_form.fn(
                None,
                request=object(),
                db_session=db_session,
                guild_id=_GUILD,
                message_id=uuid4(),
            )
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


async def test_edit_get_other_guild_message_returns_404(db_session):
    """A message owned by another guild must not be editable via this guild."""
    message = await _seed_message(db_session, guild_id="999999999999999999")
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ):
        response = await (
            RepeatingMessagesAdminController.repeating_message_edit_form.fn(
                None,
                request=object(),
                db_session=db_session,
                guild_id=_GUILD,
                message_id=message.id,
            )
        )

    assert response.status_code == 404


async def _run_edit(db_session, message_id, form):
    request = SimpleNamespace(form=AsyncMock(return_value=form))
    flash_success = Mock()
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_CAMPAIGNS_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.flash_success", flash_success):
        response = await RepeatingMessagesAdminController.repeating_message_edit.fn(
            None,
            request=request,
            db_session=db_session,
            guild_id=_GUILD,
            message_id=message_id,
        )
    return response, flash_success


async def test_edit_post_updates_and_redirects(db_session):
    message = await _seed_message(db_session, content="Before")
    response, flash_success = await _run_edit(
        db_session,
        message.id,
        _form(
            [
                ("channel_id", _CHANNEL),
                ("message_content", "After"),
                ("role_id", _ROLE),
                ("start_time", _future()),
                ("interval_minutes", "30"),
            ]
        ),
    )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    refreshed = await RepeatingMessageOperations(
        db_session
    ).get_repeating_message(message.id)
    assert refreshed.message_content == "After"
    assert refreshed.role_id == _ROLE
    assert refreshed.interval_minutes == 30


async def test_edit_post_invalid_rerenders_400(db_session):
    message = await _seed_message(db_session, content="Keep")
    response, _ = await _run_edit(
        db_session,
        message.id,
        _form(
            [
                ("channel_id", ""),
                ("message_content", ""),
                ("start_time", ""),
                ("interval_minutes", ""),
            ]
        ),
    )

    assert response.status_code == 400
    assert response.template_name == "admin/bot/repeating_messages/edit.html"
    assert response.context["errors"]
    refreshed = await RepeatingMessageOperations(
        db_session
    ).get_repeating_message(message.id)
    assert refreshed.message_content == "Keep"


# --- controller: toggle ------------------------------------------------------


async def test_toggle_flips_active_flag(db_session):
    message = await _seed_message(db_session, is_active=True)
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await RepeatingMessagesAdminController.repeating_message_toggle.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            message_id=message.id,
        )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    refreshed = await RepeatingMessageOperations(
        db_session
    ).get_repeating_message(message.id)
    assert refreshed.is_active is False


async def test_toggle_missing_flashes_error(db_session):
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error):
        response = await RepeatingMessagesAdminController.repeating_message_toggle.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            message_id=uuid4(),
        )

    assert response.status_code in (302, 303, 307)
    flash_error.assert_called_once()


# --- controller: delete ------------------------------------------------------


async def test_delete_removes_message(db_session):
    message = await _seed_message(db_session, content="Doomed")
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await RepeatingMessagesAdminController.repeating_message_delete.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            message_id=message.id,
        )

    assert response.status_code in (302, 303, 307)
    flash_success.assert_called_once()
    remaining = await RepeatingMessageOperations(
        db_session
    ).get_guild_repeating_messages(_GUILD)
    assert remaining == []


async def test_delete_missing_flashes_error(db_session):
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error):
        response = await RepeatingMessagesAdminController.repeating_message_delete.fn(
            None,
            request=object(),
            db_session=db_session,
            guild_id=_GUILD,
            message_id=uuid4(),
        )

    assert response.status_code in (302, 303, 307)
    flash_error.assert_called_once()


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        RepeatingMessagesAdminController.repeating_messages_list,
        RepeatingMessagesAdminController.repeating_message_create_form,
        RepeatingMessagesAdminController.repeating_message_create,
        RepeatingMessagesAdminController.repeating_message_edit_form,
        RepeatingMessagesAdminController.repeating_message_edit,
        RepeatingMessagesAdminController.repeating_message_toggle,
        RepeatingMessagesAdminController.repeating_message_delete,
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
