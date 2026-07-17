"""Tests for the Skrift admin per-guild bytes-config controller."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.bot_admin.bytes_config import (
    BytesConfigAdminController,
    load_or_create_config,
    parse_bytes_config_form,
)
from smarter_dev.web.crud import BytesConfigOperations, NotFoundError
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    DiscordGuildDetail,
    GuildNotFoundError,
)

_GUILD = "111111111111111111"

_MODULE = "smarter_dev.web.bot_admin.bytes_config"


def _guild_detail() -> DiscordGuildDetail:
    return DiscordGuildDetail(
        id=_GUILD,
        name="Alpha Guild",
        icon=None,
        owner_id="owner",
        member_count=42,
        description=None,
    )


# --- form parsing (pure) -----------------------------------------------------


def test_parse_bytes_config_form_scalars():
    parsed = parse_bytes_config_form(
        {
            "starting_balance": "250",
            "daily_amount": "15",
            "max_transfer": "5000",
            "transfer_cooldown_hours": "3",
        }
    )
    assert parsed["starting_balance"] == 250
    assert parsed["daily_amount"] == 15
    assert parsed["max_transfer"] == 5000
    assert parsed["transfer_cooldown_hours"] == 3
    # No streak/role fields → those keys are omitted entirely.
    assert "streak_bonuses" not in parsed
    assert "role_rewards" not in parsed


def test_parse_bytes_config_form_defaults_when_missing():
    parsed = parse_bytes_config_form({})
    assert parsed["starting_balance"] == 100
    assert parsed["daily_amount"] == 10
    assert parsed["max_transfer"] == 1000
    assert parsed["transfer_cooldown_hours"] == 0


def test_parse_bytes_config_form_streak_and_role_maps():
    parsed = parse_bytes_config_form(
        {
            "starting_balance": "100",
            "daily_amount": "10",
            "max_transfer": "1000",
            "transfer_cooldown_hours": "0",
            "streak_8_bonus": "2",
            "streak_16_bonus": "4",
            "streak_bad_bonus": "9",  # non-numeric day → skipped
            "role_reward_999": "50",
            "role_reward_888": "",  # blank → skipped
        }
    )
    assert parsed["streak_bonuses"] == {8: 2, 16: 4}
    assert parsed["role_rewards"] == {"999": 50}


def test_parse_bytes_config_form_rejects_non_integer_scalar():
    with pytest.raises(ValueError):
        parse_bytes_config_form({"starting_balance": "not-a-number"})


# --- load_or_create_config ---------------------------------------------------


async def test_load_or_create_config_creates_default(db_session):
    config = await load_or_create_config(db_session, _GUILD)

    assert config.guild_id == _GUILD
    assert config.starting_balance == 100
    # Persisted: a fresh read finds it.
    persisted = await BytesConfigOperations().get_config(db_session, _GUILD)
    assert persisted.guild_id == _GUILD


async def test_load_or_create_config_returns_existing(db_session):
    await BytesConfigOperations().create_config(
        db_session, _GUILD, starting_balance=777
    )
    await db_session.commit()

    config = await load_or_create_config(db_session, _GUILD)
    assert config.starting_balance == 777


# --- controller: GET ---------------------------------------------------------


def _get_fn():
    return BytesConfigAdminController.bytes_config.fn


def _post_fn():
    return BytesConfigAdminController.save_bytes_config.fn


async def test_bytes_config_get_renders_form(db_session):
    client = SimpleNamespace(get_guild=AsyncMock(return_value=_guild_detail()))
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=client
    ), patch(
        f"{_MODULE}.get_flash_messages", return_value=[]
    ):
        response = await _get_fn()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/bytes_config/form.html"
    assert response.context["guild"].name == "Alpha Guild"
    assert response.context["config"].guild_id == _GUILD
    assert response.context["active_page"] == "bytes"


async def test_bytes_config_get_guild_not_found_returns_404(db_session):
    client = SimpleNamespace(
        get_guild=AsyncMock(side_effect=GuildNotFoundError("nope"))
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=client):
        response = await _get_fn()(
            None, request=object(), db_session=db_session, guild_id="missing"
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"
    assert response.context["error_code"] == 404


async def test_bytes_config_get_discord_error_returns_503(db_session):
    client = SimpleNamespace(
        get_guild=AsyncMock(side_effect=DiscordAdminError("upstream boom"))
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=client):
        response = await _get_fn()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code == 503
    assert response.context["error_code"] == 503


# --- controller: POST --------------------------------------------------------


async def test_bytes_config_post_persists_and_redirects(db_session):
    request = SimpleNamespace(
        form=AsyncMock(
            return_value={
                "starting_balance": "500",
                "daily_amount": "25",
                "max_transfer": "2000",
                "transfer_cooldown_hours": "1",
                "streak_8_bonus": "3",
            }
        )
    )
    flash_success = Mock()
    with patch(f"{_MODULE}.flash_success", flash_success), patch(
        f"{_MODULE}.notify_bot_config_update", new=AsyncMock()
    ) as notify:
        response = await _post_fn()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code in (302, 303, 307)
    assert response.url == f"/admin/bot/guilds/{_GUILD}/bytes"

    saved = await BytesConfigOperations().get_config(db_session, _GUILD)
    assert saved.starting_balance == 500
    assert saved.daily_amount == 25
    assert saved.max_transfer == 2000
    assert saved.transfer_cooldown_hours == 1
    # JSON columns serialize integer keys to strings on persistence (parity
    # with the legacy view, which stored the same int-keyed map).
    assert saved.streak_bonuses == {"8": 3}

    flash_success.assert_called_once()
    notify.assert_awaited_once_with(_GUILD)


async def test_bytes_config_post_updates_existing_config(db_session):
    await BytesConfigOperations().create_config(
        db_session, _GUILD, starting_balance=1
    )
    await db_session.commit()

    request = SimpleNamespace(
        form=AsyncMock(
            return_value={
                "starting_balance": "999",
                "daily_amount": "10",
                "max_transfer": "1000",
                "transfer_cooldown_hours": "0",
            }
        )
    )
    with patch(f"{_MODULE}.flash_success", Mock()), patch(
        f"{_MODULE}.notify_bot_config_update", new=AsyncMock()
    ):
        await _post_fn()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    saved = await BytesConfigOperations().get_config(db_session, _GUILD)
    assert saved.starting_balance == 999


async def test_bytes_config_post_invalid_flashes_error_and_does_not_save(db_session):
    request = SimpleNamespace(
        form=AsyncMock(return_value={"starting_balance": "not-a-number"})
    )
    flash_error = Mock()
    with patch(f"{_MODULE}.flash_error", flash_error), patch(
        f"{_MODULE}.notify_bot_config_update", new=AsyncMock()
    ) as notify:
        response = await _post_fn()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/bytes"
    flash_error.assert_called_once()
    notify.assert_not_awaited()
    # Nothing persisted.
    with pytest.raises(NotFoundError):
        await BytesConfigOperations().get_config(db_session, _GUILD)


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        BytesConfigAdminController.bytes_config,
        BytesConfigAdminController.save_bytes_config,
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
