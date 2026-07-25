"""Tests for the Skrift admin per-guild feature-config controller.

Covers the audit-log, Advent-of-Code and attachment-filter pages ported from
the legacy ``smarter_dev.web.admin.views`` onto
``smarter_dev.web.bot_admin.guild_configs``.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.bot_admin.guild_configs import (
    DISCORD_MAX_TIMEOUT_SECONDS,
    ConfigFormError,
    GuildConfigsAdminController,
    InvalidChannelError,
    InvalidDomainError,
    InvalidRoleError,
    InvalidThresholdError,
    load_or_create_advent_config,
    load_or_create_attachment_config,
    load_or_create_audit_config,
    load_or_create_moderation_filter_config,
    parse_advent_of_code_form,
    parse_attachment_filter_form,
    parse_audit_log_form,
    parse_extensions,
    parse_moderation_filter_form,
    parse_positive_int,
    parse_role_ids,
    ThresholdCeiling,
    validate_channel_id,
    validate_role_id,
)
from smarter_dev.web.crud import (
    AdventOfCodeConfigOperations,
    AttachmentFilterConfigOperations,
    AuditLogConfigOperations,
    ModerationFilterConfigOperations,
)
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    DiscordChannel,
    DiscordGuildDetail,
    DiscordRole,
    GuildNotFoundError,
)
from smarter_dev.web.models import ModerationFilterConfig

_GUILD = "111111111111111111"
_TEXT_CHANNEL = "333333333333333333"
_FORUM_CHANNEL = "444444444444444444"
_CATEGORY_CHANNEL = "666666666666666666"
_STAFF_ROLE = "777777777777777777"
_MOD_PING_ROLE = "888888888888888888"

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
                DiscordChannel(id=_FORUM_CHANNEL, name="aoc", type=15, position=1),
                DiscordChannel(id="555", name="voice", type=2, position=2),
                DiscordChannel(
                    id=_CATEGORY_CHANNEL, name="staff", type=4, position=3
                ),
            ]
        ),
        get_guild_roles=AsyncMock(
            return_value=[
                DiscordRole(
                    id=_STAFF_ROLE,
                    name="Staff",
                    color=0,
                    position=5,
                    managed=False,
                    mentionable=True,
                ),
                DiscordRole(
                    id=_MOD_PING_ROLE,
                    name="Mods",
                    color=255,
                    position=4,
                    managed=False,
                    mentionable=True,
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


# --- moderation filter: pure id helpers --------------------------------------


def test_validate_role_id_blank_is_none():
    assert validate_role_id("") is None
    assert validate_role_id(None) is None
    assert validate_role_id("  ") is None


def test_validate_role_id_passes_snowflake():
    assert validate_role_id(f" {_MOD_PING_ROLE} ") == _MOD_PING_ROLE


def test_validate_role_id_rejects_non_digit():
    with pytest.raises(InvalidRoleError):
        validate_role_id("@everyone")


def test_parse_role_ids_splits_and_dedupes():
    parsed = parse_role_ids(f"{_STAFF_ROLE}, {_MOD_PING_ROLE}\n{_STAFF_ROLE}")
    assert parsed == [_STAFF_ROLE, _MOD_PING_ROLE]


def test_parse_role_ids_handles_blank():
    assert parse_role_ids("") == []
    assert parse_role_ids(None) == []


def test_parse_role_ids_rejects_non_digit_entry():
    with pytest.raises(InvalidRoleError):
        parse_role_ids(f"{_STAFF_ROLE}\nmoderators")


# --- moderation filter: pure threshold helper --------------------------------


def test_parse_positive_int_uses_default_when_blank():
    assert parse_positive_int("", "message_rate_threshold", 5) == 5
    assert parse_positive_int(None, "message_rate_threshold", 5) == 5


def test_parse_positive_int_parses_value():
    assert parse_positive_int(" 12 ", "message_rate_threshold", 5) == 12


def test_parse_positive_int_rejects_non_numeric():
    with pytest.raises(InvalidThresholdError) as excinfo:
        parse_positive_int("soon", "message_rate_threshold", 5)
    assert "message rate threshold" in str(excinfo.value)


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_parse_positive_int_rejects_non_positive(raw):
    with pytest.raises(InvalidThresholdError):
        parse_positive_int(raw, "mute_duration_seconds", 86400)


# --- moderation filter: parse_moderation_filter_form (pure) ------------------


def _moderation_form(**overrides: str) -> dict[str, str]:
    """A fully populated moderation-filter submission, with optional overrides."""
    form = {
        "content_filter_enabled": "on",
        "blocked_tlds": "gay, .XXX\nzip",
        "scam_link_domains": "T.ME\nhttps://wa.me/12345\nt.me",
        "invite_filter_enabled": "on",
        "invite_filter_exempt_category_ids": _CATEGORY_CHANNEL,
        "staff_exempt_role_ids": _STAFF_ROLE,
        "webhook_killer_enabled": "on",
        "mod_log_channel_id": _TEXT_CHANNEL,
        "spam_engine_enabled": "on",
        "message_rate_threshold": "7",
        "message_rate_window_seconds": "6",
        "channel_spread_threshold": "4",
        "channel_spread_window_seconds": "20",
        "duplicate_message_min_length": "25",
        "duplicate_message_window_seconds": "90",
        "mass_mention_window_seconds": "30",
        "warning_reoffense_window_seconds": "300",
        "mute_duration_seconds": "3600",
        "mod_alert_channel_id": _TEXT_CHANNEL,
        "mod_ping_role_id": _MOD_PING_ROLE,
        "scam_log_channel_id": _TEXT_CHANNEL,
    }
    form.update(overrides)
    return form


def test_parse_moderation_filter_form_happy():
    parsed = parse_moderation_filter_form(_moderation_form())

    assert parsed["content_filter_enabled"] is True
    assert parsed["invite_filter_enabled"] is True
    assert parsed["webhook_killer_enabled"] is True
    assert parsed["spam_engine_enabled"] is True
    assert parsed["blocked_tlds"] == [".gay", ".xxx", ".zip"]
    assert parsed["scam_link_domains"] == ["t.me", "wa.me"]
    assert parsed["invite_filter_exempt_category_ids"] == [_CATEGORY_CHANNEL]
    assert parsed["staff_exempt_role_ids"] == [_STAFF_ROLE]
    assert parsed["mod_log_channel_id"] == _TEXT_CHANNEL
    assert parsed["mod_alert_channel_id"] == _TEXT_CHANNEL
    assert parsed["scam_log_channel_id"] == _TEXT_CHANNEL
    assert parsed["mod_ping_role_id"] == _MOD_PING_ROLE
    assert parsed["message_rate_threshold"] == 7
    assert parsed["message_rate_window_seconds"] == 6
    assert parsed["channel_spread_threshold"] == 4
    assert parsed["channel_spread_window_seconds"] == 20
    assert parsed["duplicate_message_min_length"] == 25
    assert parsed["duplicate_message_window_seconds"] == 90
    assert parsed["mass_mention_window_seconds"] == 30
    assert parsed["warning_reoffense_window_seconds"] == 300
    assert parsed["mute_duration_seconds"] == 3600


def test_parse_moderation_filter_form_empty_submission_uses_model_defaults():
    parsed = parse_moderation_filter_form({})

    assert parsed["content_filter_enabled"] is False
    assert parsed["invite_filter_enabled"] is False
    assert parsed["webhook_killer_enabled"] is False
    assert parsed["spam_engine_enabled"] is False
    assert parsed["blocked_tlds"] == []
    # An emptied textarea means "block nothing"; the seeded platform list is a
    # row default, not a floor the form re-imposes on every save.
    assert parsed["scam_link_domains"] == []
    assert parsed["invite_filter_exempt_category_ids"] == []
    assert parsed["staff_exempt_role_ids"] == []
    assert parsed["mod_log_channel_id"] is None
    assert parsed["mod_alert_channel_id"] is None
    assert parsed["scam_log_channel_id"] is None
    assert parsed["mod_ping_role_id"] is None
    # Blank thresholds fall back to the legacy defaults carried by the model.
    assert parsed["message_rate_threshold"] == 5
    assert parsed["message_rate_window_seconds"] == 5
    assert parsed["channel_spread_threshold"] == 3
    assert parsed["channel_spread_window_seconds"] == 15
    assert parsed["duplicate_message_min_length"] == 15
    assert parsed["duplicate_message_window_seconds"] == 60
    assert parsed["mass_mention_window_seconds"] == 15
    assert parsed["warning_reoffense_window_seconds"] == 120
    assert parsed["mute_duration_seconds"] == 86400


def test_parse_moderation_filter_form_never_returns_unknown_keys():
    parsed = parse_moderation_filter_form(_moderation_form(surprise="boom"))
    assert "surprise" not in parsed


def test_parse_moderation_filter_form_rejects_bad_channel():
    with pytest.raises(InvalidChannelError):
        parse_moderation_filter_form(_moderation_form(mod_log_channel_id="nope"))


def test_parse_moderation_filter_form_rejects_bad_category():
    with pytest.raises(InvalidChannelError):
        parse_moderation_filter_form(
            _moderation_form(invite_filter_exempt_category_ids="general")
        )


def test_parse_moderation_filter_form_rejects_bad_role():
    with pytest.raises(InvalidRoleError):
        parse_moderation_filter_form(_moderation_form(mod_ping_role_id="Mods"))


def test_parse_moderation_filter_form_rejects_bad_threshold():
    with pytest.raises(InvalidThresholdError):
        parse_moderation_filter_form(
            _moderation_form(mute_duration_seconds="forever")
        )


# --- moderation filter: mute-duration ceiling (finding #4) --------------------


def test_parse_moderation_filter_form_rejects_mute_duration_beyond_discord_limit():
    """A 30-day mute is a 400 from Discord that kills all enforcement."""
    with pytest.raises(InvalidThresholdError, match="28 days"):
        parse_moderation_filter_form(
            _moderation_form(mute_duration_seconds="2592000")
        )


def test_parse_moderation_filter_form_accepts_mute_duration_at_discord_limit():
    parsed = parse_moderation_filter_form(
        _moderation_form(mute_duration_seconds=str(DISCORD_MAX_TIMEOUT_SECONDS))
    )

    assert parsed["mute_duration_seconds"] == DISCORD_MAX_TIMEOUT_SECONDS
    assert DISCORD_MAX_TIMEOUT_SECONDS == 2419200


def test_parse_moderation_filter_form_rejects_mute_duration_one_second_over():
    with pytest.raises(InvalidThresholdError):
        parse_moderation_filter_form(
            _moderation_form(
                mute_duration_seconds=str(DISCORD_MAX_TIMEOUT_SECONDS + 1)
            )
        )


def test_parse_moderation_filter_form_leaves_other_thresholds_unbounded():
    """Only mute duration hits Discord's timeout API; the rest are local windows."""
    parsed = parse_moderation_filter_form(
        _moderation_form(warning_reoffense_window_seconds="2592000")
    )

    assert parsed["warning_reoffense_window_seconds"] == 2592000


def test_parse_positive_int_without_maximum_accepts_large_values():
    assert parse_positive_int("2592000", "some_window_seconds", 60) == 2592000


def test_parse_positive_int_with_maximum_rejects_values_above_it():
    ceiling = ThresholdCeiling(limit=10, reason="the widget only counts to ten")

    with pytest.raises(InvalidThresholdError, match="only counts to ten"):
        parse_positive_int("11", "some_window_seconds", 60, maximum=ceiling)

    assert parse_positive_int("10", "some_window_seconds", 60, maximum=ceiling) == 10


# --- moderation filter: scam-link domain list ---------------------------------


def test_parse_scam_link_domains_strips_scheme_path_and_port():
    parsed = parse_moderation_filter_form(
        _moderation_form(
            scam_link_domains=(
                "https://t.me/joinchat/abc\n"
                "http://chat.whatsapp.com:8080/invite?x=1\n"
                "signal.me/#p/+15551234567"
            )
        )
    )

    assert parsed["scam_link_domains"] == [
        "t.me",
        "chat.whatsapp.com",
        "signal.me",
    ]


def test_parse_scam_link_domains_lowercases_and_deduplicates():
    parsed = parse_moderation_filter_form(
        _moderation_form(scam_link_domains="T.ME, t.me\nTelegram.Me\nt.me.")
    )

    assert parsed["scam_link_domains"] == ["t.me", "telegram.me"]


def test_parse_scam_link_domains_rejects_entries_that_are_not_hostnames():
    with pytest.raises(InvalidDomainError):
        parse_moderation_filter_form(_moderation_form(scam_link_domains="not a host"))


def test_parse_scam_link_domains_rejects_bare_labels_without_a_dot():
    with pytest.raises(InvalidDomainError):
        parse_moderation_filter_form(_moderation_form(scam_link_domains="telegram"))


def test_invalid_domain_error_is_a_config_form_error():
    assert issubclass(InvalidDomainError, ConfigFormError)


# --- moderation filter: loader ------------------------------------------------


async def test_load_or_create_moderation_filter_config_persists_default(db_session):
    config = await load_or_create_moderation_filter_config(db_session, _GUILD)

    assert config.guild_id == _GUILD
    assert config.content_filter_enabled is False
    assert config.spam_engine_enabled is False
    assert config.message_rate_threshold == 5
    persisted = await ModerationFilterConfigOperations().get_config(
        db_session, _GUILD
    )
    assert persisted is not None


# --- moderation filter: controller GET ---------------------------------------


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
    assert {r.id for r in response.context["guild_roles"]} == {
        _STAFF_ROLE,
        _MOD_PING_ROLE,
    }


async def test_moderation_get_role_fetch_error_degrades_to_empty(db_session):
    client = _admin_client()
    client.get_guild_roles = AsyncMock(side_effect=DiscordAdminError("boom"))
    with _patch_get(client):
        response = await _moderation_get()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.context["guild_roles"] == []


# --- moderation filter: controller POST --------------------------------------


def _moderation_post():
    return GuildConfigsAdminController.save_moderation_filter_config.fn


async def test_moderation_post_persists_and_redirects(db_session):
    request = SimpleNamespace(form=AsyncMock(return_value=_moderation_form()))
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success):
        response = await _moderation_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code in (302, 303, 307)
    assert response.url == f"/admin/bot/guilds/{_GUILD}/moderation-filter"

    saved = await ModerationFilterConfigOperations().get_config(db_session, _GUILD)
    assert saved.content_filter_enabled is True
    assert saved.spam_engine_enabled is True
    assert saved.blocked_tlds == [".gay", ".xxx", ".zip"]
    assert saved.scam_link_domains == ["t.me", "wa.me"]
    assert saved.staff_exempt_role_ids == [_STAFF_ROLE]
    assert saved.invite_filter_exempt_category_ids == [_CATEGORY_CHANNEL]
    assert saved.mod_ping_role_id == _MOD_PING_ROLE
    assert saved.mute_duration_seconds == 3600
    flash_success.assert_called_once()


async def test_moderation_post_invalid_threshold_flashes_error_and_does_not_save(
    db_session,
):
    request = SimpleNamespace(
        form=AsyncMock(return_value=_moderation_form(message_rate_threshold="lots"))
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


async def test_moderation_post_invalid_role_flashes_error_and_does_not_save(
    db_session,
):
    request = SimpleNamespace(
        form=AsyncMock(return_value=_moderation_form(staff_exempt_role_ids="staff"))
    )
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error):
        await _moderation_post()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    flash_error.assert_called_once()
    assert (
        await ModerationFilterConfigOperations().get_config(db_session, _GUILD)
        is None
    )


# --- moderation filter: template ----------------------------------------------


_TEMPLATES_DIR = Path(__file__).parents[2] / "templates"


def _render_moderation_template(config: SimpleNamespace) -> str:
    """Render the moderation-filter page against a stub admin base template."""
    stub_base = (
        "{% block title %}{% endblock %}{% block admin_content %}{% endblock %}"
    )
    environment = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"admin/base.html": stub_base}),
                FileSystemLoader(_TEMPLATES_DIR),
            ]
        ),
        autoescape=True,
    )
    environment.globals["site_name"] = lambda: "Smarter Dev"
    template = environment.get_template(
        "admin/bot/guild_configs/moderation_filter.html"
    )
    client = _admin_client()
    return template.render(
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
        guild_roles=client.get_guild_roles.return_value,
        active_page="moderation_filter",
        guild_id=_GUILD,
        flash_messages=[],
    )


def test_moderation_template_renders_saved_selections():
    config = SimpleNamespace(
        guild_id=_GUILD,
        content_filter_enabled=True,
        blocked_tlds=[".gay", ".xxx"],
        scam_link_domains=["t.me", "wa.me"],
        invite_filter_enabled=True,
        invite_filter_exempt_category_ids=[_CATEGORY_CHANNEL],
        staff_exempt_role_ids=[_STAFF_ROLE],
        webhook_killer_enabled=False,
        mod_log_channel_id=_TEXT_CHANNEL,
        spam_engine_enabled=True,
        message_rate_threshold=5,
        message_rate_window_seconds=5,
        channel_spread_threshold=3,
        channel_spread_window_seconds=15,
        duplicate_message_min_length=15,
        duplicate_message_window_seconds=60,
        mass_mention_window_seconds=15,
        warning_reoffense_window_seconds=120,
        mute_duration_seconds=86400,
        mod_alert_channel_id=None,
        mod_ping_role_id=_MOD_PING_ROLE,
        scam_log_channel_id=None,
    )

    html = _render_moderation_template(config)

    assert f'action="/admin/bot/guilds/{_GUILD}/moderation-filter"' in html
    # Channel picker keeps the saved selection.
    assert f'<option value="{_TEXT_CHANNEL}" selected>#general</option>' in html
    # Role picker keeps the saved selection.
    assert f'<option value="{_MOD_PING_ROLE}" selected>@Mods</option>' in html
    # List fields round-trip as newline-separated text the parser accepts back.
    assert ">.gay\n.xxx</textarea>" in html
    assert ">t.me\nwa.me</textarea>" in html
    assert f">{_STAFF_ROLE}</textarea>" in html
    # Every field the parser reads is present under its exact name.
    for field_name in (
        "content_filter_enabled",
        "blocked_tlds",
        "scam_link_domains",
        "invite_filter_enabled",
        "invite_filter_exempt_category_ids",
        "staff_exempt_role_ids",
        "webhook_killer_enabled",
        "mod_log_channel_id",
        "spam_engine_enabled",
        "message_rate_threshold",
        "message_rate_window_seconds",
        "channel_spread_threshold",
        "channel_spread_window_seconds",
        "duplicate_message_min_length",
        "duplicate_message_window_seconds",
        "mass_mention_window_seconds",
        "warning_reoffense_window_seconds",
        "mute_duration_seconds",
        "mod_alert_channel_id",
        "mod_ping_role_id",
        "scam_log_channel_id",
    ):
        assert f'name="{field_name}"' in html


def test_moderation_template_caps_mute_duration_at_discords_limit():
    """The browser and the help text must agree with the server-side ceiling."""
    html = _render_moderation_template(ModerationFilterConfig.get_defaults(_GUILD))

    mute_input_start = html.index('name="mute_duration_seconds"')
    mute_field = html[mute_input_start - 200 : mute_input_start + 400]

    assert f'max="{DISCORD_MAX_TIMEOUT_SECONDS}"' in mute_field
    assert "28 days" in mute_field


def test_moderation_template_seeds_scam_link_domains_from_config():
    html = _render_moderation_template(ModerationFilterConfig.get_defaults(_GUILD))

    assert 'name="scam_link_domains"' in html
    assert "t.me" in html


def test_moderation_template_survives_empty_pickers():
    config = ModerationFilterConfig.get_defaults(_GUILD)

    stub_base = (
        "{% block title %}{% endblock %}{% block admin_content %}{% endblock %}"
    )
    environment = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"admin/base.html": stub_base}),
                FileSystemLoader(_TEMPLATES_DIR),
            ]
        ),
        autoescape=True,
    )
    environment.globals["site_name"] = lambda: "Smarter Dev"
    html = environment.get_template(
        "admin/bot/guild_configs/moderation_filter.html"
    ).render(
        guild=_guild_detail(),
        config=config,
        text_channels=[],
        category_channels=[],
        guild_roles=[],
        active_page="moderation_filter",
        guild_id=_GUILD,
        flash_messages=[],
    )

    assert "No roles were returned by Discord." in html
    assert "No channel categories were returned by Discord." in html


def test_sidebar_links_the_moderation_filter_page():
    sidebar = (_TEMPLATES_DIR / "admin" / "bot" / "_sidebar.html").read_text()
    assert "/moderation-filter" in sidebar
    assert "active_page == 'moderation_filter'" in sidebar


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
