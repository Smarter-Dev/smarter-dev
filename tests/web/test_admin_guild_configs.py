"""Tests for the Skrift admin per-guild feature-config controller.

Covers the audit-log, Advent-of-Code and attachment-filter pages ported from
the legacy ``smarter_dev.web.admin.views`` onto
``smarter_dev.web.bot_admin.guild_configs``.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.bot_admin.guild_configs import (
    GuildConfigsAdminController,
    InvalidChannelError,
    load_or_create_advent_config,
    load_or_create_attachment_config,
    load_or_create_audit_config,
    parse_advent_of_code_form,
    parse_attachment_filter_form,
    parse_audit_log_form,
    parse_extensions,
    validate_channel_id,
)
from smarter_dev.web.crud import (
    AdventOfCodeConfigOperations,
    AttachmentFilterConfigOperations,
    AuditLogConfigOperations,
)
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    DiscordChannel,
    DiscordGuildDetail,
    GuildNotFoundError,
)

_GUILD = "111111111111111111"
_TEXT_CHANNEL = "333333333333333333"
_FORUM_CHANNEL = "444444444444444444"

_MODULE = "smarter_dev.web.bot_admin.guild_configs"
_SQUADS_MODULE = "smarter_dev.web.bot_admin.squads"


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
    """A Discord client mock returning one text channel and one forum channel."""
    return SimpleNamespace(
        get_guild=AsyncMock(return_value=_guild_detail()),
        get_guild_channels=AsyncMock(
            return_value=[
                DiscordChannel(id=_TEXT_CHANNEL, name="general", type=0, position=0),
                DiscordChannel(id=_FORUM_CHANNEL, name="aoc", type=15, position=1),
                DiscordChannel(id="555", name="voice", type=2, position=2),
            ]
        ),
    )


@contextmanager
def _patch_get(client: SimpleNamespace):
    """Patch every collaborator a GET handler reaches, across both modules.

    ``fetch_guild_or_error`` lives in the squads module, so its
    ``get_admin_discord_client``/``get_admin_context`` must be patched there;
    the guild-configs module resolves channels and flash messages itself.
    """
    with patch(
        f"{_SQUADS_MODULE}.get_admin_discord_client", return_value=client
    ), patch(
        f"{_SQUADS_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=client
    ), patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_flash_messages", return_value=[]
    ):
        yield


# --- validate_channel_id (pure) ---------------------------------------------


def test_validate_channel_id_blank_is_none():
    assert validate_channel_id("") is None
    assert validate_channel_id(None) is None
    assert validate_channel_id("   ") is None


def test_validate_channel_id_passes_snowflake():
    assert validate_channel_id(" 12345 ") == "12345"


def test_validate_channel_id_rejects_non_digit():
    with pytest.raises(InvalidChannelError):
        validate_channel_id("not-a-snowflake")


# --- parse_extensions (pure) -------------------------------------------------


def test_parse_extensions_adds_dots_lowercases_and_dedupes():
    parsed = parse_extensions("PNG\n.jpg, jpg\n.PNG")
    assert parsed == [".png", ".jpg"]


def test_parse_extensions_handles_blank():
    assert parse_extensions("") == []
    assert parse_extensions(None) == []


# --- parse_audit_log_form (pure) ---------------------------------------------


def test_parse_audit_log_form_channel_and_toggles():
    parsed = parse_audit_log_form(
        {
            "audit_channel_id": _TEXT_CHANNEL,
            "log_member_join": "on",
            "log_message_delete": "on",
        }
    )
    assert parsed["audit_channel_id"] == _TEXT_CHANNEL
    assert parsed["log_member_join"] is True
    assert parsed["log_message_delete"] is True
    # Unchecked boxes are absent from the submission → False.
    assert parsed["log_member_leave"] is False
    assert parsed["log_role_change"] is False


def test_parse_audit_log_form_blank_channel_disables():
    parsed = parse_audit_log_form({"audit_channel_id": ""})
    assert parsed["audit_channel_id"] is None


def test_parse_audit_log_form_rejects_bad_channel():
    with pytest.raises(InvalidChannelError):
        parse_audit_log_form({"audit_channel_id": "garbage"})


# --- parse_advent_of_code_form (pure) ----------------------------------------


def test_parse_advent_of_code_form_happy():
    parsed = parse_advent_of_code_form(
        {"forum_channel_id": _FORUM_CHANNEL, "is_active": "on"}
    )
    assert parsed == {"forum_channel_id": _FORUM_CHANNEL, "is_active": True}


def test_parse_advent_of_code_form_defaults_inactive():
    parsed = parse_advent_of_code_form({})
    assert parsed == {"forum_channel_id": None, "is_active": False}


def test_parse_advent_of_code_form_rejects_bad_channel():
    with pytest.raises(InvalidChannelError):
        parse_advent_of_code_form({"forum_channel_id": "nope"})


# --- parse_attachment_filter_form (pure) -------------------------------------


def test_parse_attachment_filter_form_happy():
    parsed = parse_attachment_filter_form(
        {
            "is_active": "on",
            "warn_message": "  careful  ",
            "delete_message": "",
            "ignored_extensions": ".png, jpg",
            "warn_extensions": "zip\n.rar",
        }
    )
    assert parsed["is_active"] is True
    assert parsed["warn_message"] == "careful"
    assert parsed["delete_message"] is None
    assert parsed["ignored_extensions"] == [".png", ".jpg"]
    assert parsed["warn_extensions"] == [".zip", ".rar"]


# --- load_or_create helpers --------------------------------------------------


async def test_load_or_create_audit_config_persists_default(db_session):
    config = await load_or_create_audit_config(db_session, _GUILD)
    assert config.guild_id == _GUILD
    assert config.log_member_join is True
    persisted = await AuditLogConfigOperations().get_config(db_session, _GUILD)
    assert persisted is not None


async def test_load_or_create_advent_config_persists_default(db_session):
    config = await load_or_create_advent_config(db_session, _GUILD)
    assert config.guild_id == _GUILD
    assert config.is_active is False
    persisted = await AdventOfCodeConfigOperations().get_config(db_session, _GUILD)
    assert persisted is not None


async def test_load_or_create_attachment_config_persists_default(db_session):
    config = await load_or_create_attachment_config(db_session, _GUILD)
    assert config.guild_id == _GUILD
    assert config.ignored_extensions == []
    persisted = await AttachmentFilterConfigOperations().get_config(
        db_session, _GUILD
    )
    assert persisted is not None


# --- controller: GET happy paths --------------------------------------------


def _audit_get():
    return GuildConfigsAdminController.audit_log_config.fn


def _advent_get():
    return GuildConfigsAdminController.advent_of_code_config.fn


def _attachment_get():
    return GuildConfigsAdminController.attachment_filter_config.fn


async def test_audit_get_renders_form_with_text_channels(db_session):
    with _patch_get(_admin_client()):
        response = await _audit_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/guild_configs/audit_logs.html"
    assert response.context["guild"].name == "Alpha Guild"
    assert response.context["active_page"] == "audit_logs"
    # Only text/news channels (types 0, 5) reach the picker.
    channel_ids = {c.id for c in response.context["channels"]}
    assert channel_ids == {_TEXT_CHANNEL}


async def test_advent_get_renders_form_with_forum_channels(db_session):
    with _patch_get(_admin_client()):
        response = await _advent_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/guild_configs/advent_of_code.html"
    assert response.context["active_page"] == "advent_of_code"
    channel_ids = {c.id for c in response.context["forum_channels"]}
    assert channel_ids == {_FORUM_CHANNEL}
    assert response.context["threads"] == []


async def test_attachment_get_renders_form(db_session):
    with _patch_get(_admin_client()):
        response = await _attachment_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert (
        response.template_name
        == "admin/bot/guild_configs/attachment_filter.html"
    )
    assert response.context["active_page"] == "attachment_filter"
    assert response.context["config"].guild_id == _GUILD


# --- controller: GET failure paths ------------------------------------------


async def test_audit_get_guild_not_found_returns_404(db_session):
    client = SimpleNamespace(
        get_guild=AsyncMock(side_effect=GuildNotFoundError("nope"))
    )
    with _patch_get(client):
        response = await _audit_get()(
            None, request=object(), db_session=db_session, guild_id="missing"
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


async def test_advent_get_discord_error_returns_503(db_session):
    client = SimpleNamespace(
        get_guild=AsyncMock(side_effect=DiscordAdminError("upstream boom"))
    )
    with _patch_get(client):
        response = await _advent_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code == 503
    assert response.context["error_code"] == 503


async def test_audit_get_channel_fetch_error_degrades_to_empty(db_session):
    client = SimpleNamespace(
        get_guild=AsyncMock(return_value=_guild_detail()),
        get_guild_channels=AsyncMock(side_effect=DiscordAdminError("boom")),
    )
    with _patch_get(client):
        response = await _audit_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.context["channels"] == []


# --- controller: POST audit log ---------------------------------------------


def _audit_post():
    return GuildConfigsAdminController.save_audit_log_config.fn


async def test_audit_post_persists_and_redirects(db_session):
    request = SimpleNamespace(
        form=AsyncMock(
            return_value={
                "audit_channel_id": _TEXT_CHANNEL,
                "log_member_join": "on",
                "log_message_edit": "on",
            }
        )
    )
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await _audit_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code in (302, 303, 307)
    assert response.url == f"/admin/bot/guilds/{_GUILD}/audit-logs"

    saved = await AuditLogConfigOperations().get_config(db_session, _GUILD)
    assert saved.audit_channel_id == _TEXT_CHANNEL
    assert saved.log_member_join is True
    assert saved.log_message_edit is True
    assert saved.log_member_leave is False
    flash_success.assert_called_once()


async def test_audit_post_invalid_channel_flashes_error_and_does_not_save(
    db_session,
):
    request = SimpleNamespace(
        form=AsyncMock(return_value={"audit_channel_id": "garbage"})
    )
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error):
        response = await _audit_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/audit-logs"
    flash_error.assert_called_once()
    assert await AuditLogConfigOperations().get_config(db_session, _GUILD) is None


# --- controller: POST advent of code ----------------------------------------


def _advent_post():
    return GuildConfigsAdminController.save_advent_of_code_config.fn


async def test_advent_post_persists_and_redirects(db_session):
    request = SimpleNamespace(
        form=AsyncMock(
            return_value={
                "forum_channel_id": _FORUM_CHANNEL,
                "is_active": "on",
            }
        )
    )
    with patch(f"{_MODULE}.flash_success", Mock()):
        response = await _advent_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/advent-of-code"
    saved = await AdventOfCodeConfigOperations().get_config(db_session, _GUILD)
    assert saved.forum_channel_id == _FORUM_CHANNEL
    assert saved.is_active is True


async def test_advent_post_invalid_channel_flashes_error(db_session):
    request = SimpleNamespace(
        form=AsyncMock(return_value={"forum_channel_id": "nope"})
    )
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error):
        await _advent_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    flash_error.assert_called_once()
    assert (
        await AdventOfCodeConfigOperations().get_config(db_session, _GUILD) is None
    )


# --- controller: POST attachment filter -------------------------------------


def _attachment_post():
    return GuildConfigsAdminController.save_attachment_filter_config.fn


async def test_attachment_post_persists_and_redirects(db_session):
    request = SimpleNamespace(
        form=AsyncMock(
            return_value={
                "is_active": "on",
                "ignored_extensions": ".png\n.jpg",
                "warn_extensions": ".zip",
                "warn_message": "heads up",
                "delete_message": "",
            }
        )
    )
    with patch(f"{_MODULE}.flash_success", Mock()):
        response = await _attachment_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/attachment-filter"
    saved = await AttachmentFilterConfigOperations().get_config(db_session, _GUILD)
    assert saved.is_active is True
    assert saved.ignored_extensions == [".png", ".jpg"]
    assert saved.warn_extensions == [".zip"]
    assert saved.warn_message == "heads up"
    assert saved.delete_message is None


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        GuildConfigsAdminController.audit_log_config,
        GuildConfigsAdminController.save_audit_log_config,
        GuildConfigsAdminController.advent_of_code_config,
        GuildConfigsAdminController.save_advent_of_code_config,
        GuildConfigsAdminController.attachment_filter_config,
        GuildConfigsAdminController.save_attachment_filter_config,
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
