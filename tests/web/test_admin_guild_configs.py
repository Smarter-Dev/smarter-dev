"""Tests for the Skrift admin per-guild feature-config controller.

Covers the audit-log page ported from the legacy ``smarter_dev.web.admin.views``
onto ``smarter_dev.web.bot_admin.guild_configs``, and the Content Filters page,
which since #242 shows and saves only the content filter's own fields.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard

from smarter_dev.web.bot_admin.guild_configs import CONTENT_FILTER_FIELDS
from smarter_dev.web.bot_admin.guild_configs import GuildConfigsAdminController
from smarter_dev.web.bot_admin.guild_configs import InvalidChannelError
from smarter_dev.web.bot_admin.guild_configs import load_or_create_audit_config
from smarter_dev.web.bot_admin.guild_configs import (
    load_or_create_moderation_filter_config,
)
from smarter_dev.web.bot_admin.guild_configs import parse_audit_log_form
from smarter_dev.web.bot_admin.guild_configs import parse_extensions
from smarter_dev.web.bot_admin.guild_configs import parse_moderation_filter_form
from smarter_dev.web.bot_admin.guild_configs import validate_channel_id
from smarter_dev.web.crud import AuditLogConfigOperations
from smarter_dev.web.crud import ModerationFilterConfigOperations
from smarter_dev.web.discord_admin_client import DiscordAdminError
from smarter_dev.web.discord_admin_client import DiscordChannel
from smarter_dev.web.discord_admin_client import DiscordGuildDetail
from smarter_dev.web.discord_admin_client import GuildNotFoundError
from smarter_dev.web.models import ModerationFilterConfig
from tests.web.admin_template_rendering import admin_template_source
from tests.web.admin_template_rendering import render_admin_template

_GUILD = "111111111111111111"
_TEXT_CHANNEL = "333333333333333333"
_FORUM_CHANNEL = "444444444444444444"
_CATEGORY_CHANNEL = "666666666666666666"

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
    """A Discord client mock returning one channel of each interesting type."""
    return SimpleNamespace(
        get_guild=AsyncMock(return_value=_guild_detail()),
        get_guild_channels=AsyncMock(
            return_value=[
                DiscordChannel(id=_TEXT_CHANNEL, name="general", type=0, position=0),
                DiscordChannel(id=_FORUM_CHANNEL, name="forum", type=15, position=1),
                DiscordChannel(id="555", name="voice", type=2, position=2),
                DiscordChannel(
                    id=_CATEGORY_CHANNEL, name="staff", type=4, position=3
                ),
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


# --- load_or_create helpers --------------------------------------------------


async def test_load_or_create_audit_config_persists_default(db_session):
    config = await load_or_create_audit_config(db_session, _GUILD)
    assert config.guild_id == _GUILD
    assert config.log_member_join is True
    persisted = await AuditLogConfigOperations().get_config(db_session, _GUILD)
    assert persisted is not None


# --- controller: GET happy paths --------------------------------------------


def _audit_get():
    return GuildConfigsAdminController.audit_log_config.fn


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


async def test_audit_get_discord_error_returns_503(db_session):
    client = SimpleNamespace(
        get_guild=AsyncMock(side_effect=DiscordAdminError("upstream boom"))
    )
    with _patch_get(client):
        response = await _audit_get()(
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


# --- content filters: parse_moderation_filter_form (pure) -------------------


def _content_form(**overrides: str) -> dict[str, str]:
    """A fully populated Content Filters submission, with optional overrides."""
    form = {
        "content_filter_enabled": "on",
        "blocked_tlds": "gay, .XXX\nzip",
        "invite_filter_enabled": "on",
        "invite_filter_exempt_category_ids": _CATEGORY_CHANNEL,
        "webhook_killer_enabled": "on",
        "mod_log_channel_id": _TEXT_CHANNEL,
    }
    form.update(overrides)
    return form


#: What the old form also posted; none of it is the content filter's.
_RETIRED_FIELDS = {
    "scam_link_domains": "t.me",
    "staff_exempt_role_ids": "777777777777777777",
    "spam_engine_enabled": "on",
    "message_rate_threshold": "7",
    "mute_duration_seconds": "3600",
    "mod_alert_channel_id": _TEXT_CHANNEL,
    "mod_ping_role_id": "888888888888888888",
    "scam_log_channel_id": _TEXT_CHANNEL,
    "spam_ring_enabled": "on",
    "spam_ring_report_channel_id": _TEXT_CHANNEL,
}


def test_parse_moderation_filter_form_happy():
    parsed = parse_moderation_filter_form(_content_form())

    assert parsed == {
        "content_filter_enabled": True,
        "invite_filter_enabled": True,
        "webhook_killer_enabled": True,
        "blocked_tlds": [".gay", ".xxx", ".zip"],
        "invite_filter_exempt_category_ids": [_CATEGORY_CHANNEL],
        "mod_log_channel_id": _TEXT_CHANNEL,
    }


def test_parse_moderation_filter_form_empty_submission_turns_everything_off():
    parsed = parse_moderation_filter_form({})

    assert parsed == {
        "content_filter_enabled": False,
        "invite_filter_enabled": False,
        "webhook_killer_enabled": False,
        "blocked_tlds": [],
        "invite_filter_exempt_category_ids": [],
        "mod_log_channel_id": None,
    }


def test_parse_moderation_filter_form_reads_only_the_content_filters_fields():
    parsed = parse_moderation_filter_form(
        _content_form(**_RETIRED_FIELDS, surprise="boom")
    )
    assert set(parsed) == CONTENT_FILTER_FIELDS


def test_parse_moderation_filter_form_rejects_bad_channel():
    with pytest.raises(InvalidChannelError):
        parse_moderation_filter_form(_content_form(mod_log_channel_id="nope"))


def test_parse_moderation_filter_form_rejects_bad_category():
    with pytest.raises(InvalidChannelError):
        parse_moderation_filter_form(
            _content_form(invite_filter_exempt_category_ids="general")
        )


# --- content filters: loader -------------------------------------------------


async def test_load_or_create_moderation_filter_config_persists_default(db_session):
    config = await load_or_create_moderation_filter_config(db_session, _GUILD)

    assert config.guild_id == _GUILD
    assert config.content_filter_enabled is False
    persisted = await ModerationFilterConfigOperations().get_config(
        db_session, _GUILD
    )
    assert persisted is not None


# --- content filters: controller GET ------------------------------------------


def _moderation_get():
    return GuildConfigsAdminController.moderation_filter_config.fn


async def test_moderation_get_renders_form_with_pickers(db_session):
    with _patch_get(_admin_client()):
        response = await _moderation_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert (
        response.template_name
        == "admin/bot/guild_configs/moderation_filter.html"
    )
    assert response.context["active_page"] == "moderation_filter"
    assert response.context["config"].guild_id == _GUILD
    assert {c.id for c in response.context["text_channels"]} == {_TEXT_CHANNEL}
    assert {c.id for c in response.context["category_channels"]} == {
        _CATEGORY_CHANNEL
    }
    assert "guild_roles" not in response.context
    assert "policy_revision" not in response.context


# --- content filters: controller POST -----------------------------------------


def _moderation_post():
    return GuildConfigsAdminController.save_moderation_filter_config.fn


async def test_moderation_post_persists_only_the_content_filter_and_redirects(
    db_session,
):
    request = SimpleNamespace(
        form=AsyncMock(return_value=_content_form(**_RETIRED_FIELDS))
    )
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await _moderation_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code in (302, 303, 307)
    assert response.url == f"/admin/bot/guilds/{_GUILD}/moderation-filter"

    saved = await ModerationFilterConfigOperations().get_config(db_session, _GUILD)
    assert saved.content_filter_enabled is True
    assert saved.blocked_tlds == [".gay", ".xxx", ".zip"]
    assert saved.invite_filter_enabled is True
    assert saved.invite_filter_exempt_category_ids == [_CATEGORY_CHANNEL]
    assert saved.webhook_killer_enabled is True
    assert saved.mod_log_channel_id == _TEXT_CHANNEL
    for retired in _RETIRED_FIELDS:
        assert not hasattr(saved, retired), retired
    flash_success.assert_called_once()


async def test_moderation_post_invalid_channel_flashes_error_and_does_not_save(
    db_session,
):
    request = SimpleNamespace(
        form=AsyncMock(return_value=_content_form(mod_log_channel_id="lots"))
    )
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error):
        response = await _moderation_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/moderation-filter"
    flash_error.assert_called_once()
    assert (
        await ModerationFilterConfigOperations().get_config(db_session, _GUILD)
        is None
    )


# --- content filters: template ------------------------------------------------


def _render_moderation_template(config) -> str:
    """Render the Content Filters page against a stub admin base template."""
    return render_admin_template(
        "admin/bot/guild_configs/moderation_filter.html",
        guild=_guild_detail(),
        config=config,
        text_channels=[
            DiscordChannel(id=_TEXT_CHANNEL, name="general", type=0, position=0)
        ],
        category_channels=[
            DiscordChannel(
                id=_CATEGORY_CHANNEL, name="staff", type=4, position=3
            )
        ],
        active_page="moderation_filter",
        guild_id=_GUILD,
        flash_messages=[],
    )


def test_moderation_template_renders_saved_selections():
    config = SimpleNamespace(
        guild_id=_GUILD,
        content_filter_enabled=True,
        blocked_tlds=[".gay", ".xxx"],
        invite_filter_enabled=True,
        invite_filter_exempt_category_ids=[_CATEGORY_CHANNEL],
        webhook_killer_enabled=False,
        mod_log_channel_id=_TEXT_CHANNEL,
    )

    html = _render_moderation_template(config)

    assert f'action="/admin/bot/guilds/{_GUILD}/moderation-filter"' in html
    # Channel picker keeps the saved selection.
    assert f'<option value="{_TEXT_CHANNEL}" selected>#general</option>' in html
    # List fields round-trip as newline-separated text the parser accepts back.
    assert ">.gay\n.xxx</textarea>" in html
    # Every field the parser reads is present under its exact name, and
    # nothing else is.
    names = set(re.findall(r'name="([^"]+)"', html)) - {"csrf_token"}
    assert names == CONTENT_FILTER_FIELDS
    # The rest of moderation is on the Moderation page.
    assert f'href="/admin/bot/guilds/{_GUILD}/moderation"' in html


def test_moderation_template_survives_empty_pickers():
    config = ModerationFilterConfig.get_defaults(_GUILD)

    html = render_admin_template(
        "admin/bot/guild_configs/moderation_filter.html",
        guild=_guild_detail(),
        config=config,
        text_channels=[],
        category_channels=[],
        active_page="moderation_filter",
        guild_id=_GUILD,
        flash_messages=[],
    )

    assert "No channel categories were returned by Discord." in html


def test_sidebar_links_the_moderation_filter_page():
    sidebar = admin_template_source("admin/bot/_sidebar.html")
    assert "/moderation-filter" in sidebar
    assert "active_page == 'moderation_filter'" in sidebar
    assert "/attachment-filter" not in sidebar


async def test_the_old_attachment_filter_url_redirects_to_the_moderation_page():
    response = await GuildConfigsAdminController.attachment_filter_config.fn(
        None, guild_id=_GUILD
    )
    assert response.url == f"/admin/bot/guilds/{_GUILD}/moderation"


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        GuildConfigsAdminController.audit_log_config,
        GuildConfigsAdminController.save_audit_log_config,
        GuildConfigsAdminController.attachment_filter_config,
        GuildConfigsAdminController.moderation_filter_config,
        GuildConfigsAdminController.save_moderation_filter_config,
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
