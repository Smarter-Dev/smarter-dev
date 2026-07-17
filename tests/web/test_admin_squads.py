"""Tests for the Skrift admin squads + squad-sale-events controller."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest

from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.bot_admin.squads import (
    SquadsAdminController,
    parse_datetime_local,
    parse_sale_event_form,
    parse_sale_event_update_form,
    parse_squad_create_form,
    parse_squad_update_form,
)
from smarter_dev.web.crud import (
    ConflictError,
    SquadOperations,
    SquadSaleEventOperations,
)
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    DiscordChannel,
    DiscordGuildDetail,
    DiscordRole,
    GuildNotFoundError,
)
from smarter_dev.web.models import Squad, SquadMembership

_GUILD = "111111111111111111"
_ROLE = "222222222222222222"
_MODULE = "smarter_dev.web.bot_admin.squads"


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
        get_guild_roles=AsyncMock(
            return_value=[
                DiscordRole(
                    id=_ROLE,
                    name="Squad Role",
                    color=0x00FF00,
                    position=2,
                    managed=False,
                    mentionable=True,
                )
            ]
        ),
        get_announcement_channels=AsyncMock(
            return_value=[
                DiscordChannel(id="333", name="general", type=0, position=0)
            ]
        ),
    )


async def _seed_squad(db_session, *, is_default: bool = False) -> Squad:
    squad = Squad(guild_id=_GUILD, role_id=_ROLE, name="Alpha", is_default=is_default)
    db_session.add(squad)
    await db_session.flush()
    db_session.add(
        SquadMembership(squad_id=squad.id, user_id="u1", guild_id=_GUILD)
    )
    await db_session.commit()
    return squad


# --- pure form parsers -------------------------------------------------------


def test_parse_squad_create_form_happy():
    parsed = parse_squad_create_form(
        {
            "role_id": _ROLE,
            "name": "Bravo",
            "description": "",
            "welcome_message": "Hi",
            "announcement_channel": "",
            "switch_cost": "75",
            "max_members": "10",
            "is_default": "on",
        }
    )
    assert parsed["role_id"] == _ROLE
    assert parsed["name"] == "Bravo"
    assert parsed["description"] is None
    assert parsed["welcome_message"] == "Hi"
    assert parsed["announcement_channel"] is None
    assert parsed["switch_cost"] == 75
    assert parsed["max_members"] == 10
    assert parsed["is_default"] is True


def test_parse_squad_create_form_defaults_and_unlimited_members():
    parsed = parse_squad_create_form({"role_id": _ROLE, "name": "Bravo"})
    assert parsed["switch_cost"] == 50
    assert parsed["max_members"] is None
    assert parsed["is_default"] is False


def test_parse_squad_create_form_rejects_non_integer_switch_cost():
    with pytest.raises(ValueError):
        parse_squad_create_form({"role_id": _ROLE, "name": "x", "switch_cost": "abc"})


def test_parse_squad_update_form_happy():
    squad_id = uuid4()
    parsed_id, updates = parse_squad_update_form(
        {
            "squad_id": str(squad_id),
            "name": "Renamed",
            "switch_cost": "10",
            "is_active": "on",
        }
    )
    assert parsed_id == squad_id
    assert updates["name"] == "Renamed"
    assert updates["switch_cost"] == 10
    assert updates["is_active"] is True
    assert updates["is_default"] is False
    assert updates["max_members"] is None


def test_parse_squad_update_form_rejects_bad_uuid():
    with pytest.raises(ValueError):
        parse_squad_update_form({"squad_id": "not-a-uuid", "switch_cost": "1"})


def test_parse_datetime_local():
    assert parse_datetime_local("2026-08-01T14:30") == datetime(2026, 8, 1, 14, 30)


def test_parse_sale_event_form_happy():
    parsed = parse_sale_event_form(
        {
            "name": "Sale",
            "description": "",
            "start_time": "2026-08-01T14:30",
            "duration_hours": "48",
            "join_discount_percent": "25",
        }
    )
    assert parsed["name"] == "Sale"
    assert parsed["description"] == ""
    assert parsed["start_time"] == datetime(2026, 8, 1, 14, 30)
    assert parsed["duration_hours"] == 48
    assert parsed["join_discount_percent"] == 25
    assert parsed["switch_discount_percent"] == 0


def test_parse_sale_event_form_rejects_missing_start_time():
    with pytest.raises(AttributeError):
        parse_sale_event_form({"name": "Sale", "duration_hours": "1"})


def test_parse_sale_event_update_form_reads_is_active_toggle():
    updates = parse_sale_event_update_form(
        {
            "name": "Sale",
            "start_time": "2026-08-01T14:30",
            "duration_hours": "1",
            "is_active": "true",
        }
    )
    assert updates["is_active"] is True
    assert parse_sale_event_update_form(
        {"name": "S", "start_time": "2026-08-01T14:30", "duration_hours": "1"}
    )["is_active"] is False


# --- controller: squads config GET -------------------------------------------


def _squads_get_fn():
    return SquadsAdminController.squads_config.fn


async def test_squads_config_get_renders(db_session):
    await _seed_squad(db_session)
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=_admin_client()
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await _squads_get_fn()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/squads/config.html"
    assert response.context["active_page"] == "squads"
    assert [s.name for s in response.context["squads"]] == ["Alpha"]
    assert len(response.context["squad_members"]) == 1
    assert response.context["guild_roles"][0].id == _ROLE


async def test_squads_config_get_guild_not_found_returns_404(db_session):
    client = SimpleNamespace(get_guild=AsyncMock(side_effect=GuildNotFoundError("x")))
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(f"{_MODULE}.get_admin_discord_client", return_value=client):
        response = await _squads_get_fn()(
            None, request=object(), db_session=db_session, guild_id="missing"
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"


async def test_squads_config_get_channels_error_degrades_to_empty(db_session):
    await _seed_squad(db_session)
    client = _admin_client()
    client.get_announcement_channels = AsyncMock(
        side_effect=DiscordAdminError("channels boom")
    )
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client", return_value=client
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await _squads_get_fn()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.context["channels"] == []


# --- controller: squads config POST ------------------------------------------


def _squads_post_fn():
    return SquadsAdminController.save_squads_config.fn


async def _run_squads_post(db_session, form):
    request = SimpleNamespace(form=AsyncMock(return_value=form))
    flash_success, flash_error = Mock(), Mock()
    with patch(f"{_MODULE}.flash_success", flash_success), patch(
        f"{_MODULE}.flash_error", flash_error
    ):
        response = await _squads_post_fn()(
            None, request=request, db_session=db_session, guild_id=_GUILD
        )
    return response, flash_success, flash_error


async def test_squads_post_create_persists_and_redirects(db_session):
    response, flash_success, flash_error = await _run_squads_post(
        db_session,
        {"action": "create", "role_id": _ROLE, "name": "Bravo", "switch_cost": "20"},
    )

    assert response.status_code in (302, 303, 307)
    assert response.url == f"/admin/bot/guilds/{_GUILD}/squads"
    flash_success.assert_called_once()
    flash_error.assert_not_called()

    squads = await SquadOperations().get_guild_squads(db_session, _GUILD)
    assert [s.name for s in squads] == ["Bravo"]


async def test_squads_post_update_changes_squad(db_session):
    squad = await _seed_squad(db_session)
    response, flash_success, _ = await _run_squads_post(
        db_session,
        {
            "action": "update",
            "squad_id": str(squad.id),
            "name": "Renamed",
            "switch_cost": "99",
            "is_active": "on",
        },
    )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/squads"
    flash_success.assert_called_once()
    refreshed = await SquadOperations().get_squad(db_session, squad.id)
    assert refreshed.name == "Renamed"
    assert refreshed.switch_cost == 99


async def test_squads_post_delete_removes_squad(db_session):
    squad = await _seed_squad(db_session)
    response, flash_success, _ = await _run_squads_post(
        db_session, {"action": "delete", "squad_id": str(squad.id)}
    )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/squads"
    flash_success.assert_called_once()
    squads = await SquadOperations().get_guild_squads(db_session, _GUILD)
    assert squads == []


async def test_squads_post_invalid_flashes_error_and_persists_nothing(db_session):
    response, flash_success, flash_error = await _run_squads_post(
        db_session,
        {"action": "create", "role_id": _ROLE, "name": "x", "switch_cost": "abc"},
    )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/squads"
    flash_error.assert_called_once()
    flash_success.assert_not_called()
    squads = await SquadOperations().get_guild_squads(db_session, _GUILD)
    assert squads == []


async def test_squads_post_duplicate_default_flashes_conflict(db_session):
    await _seed_squad(db_session, is_default=True)
    response, flash_success, flash_error = await _run_squads_post(
        db_session,
        {
            "action": "create",
            "role_id": "999",
            "name": "SecondDefault",
            "is_default": "on",
        },
    )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/squads"
    flash_error.assert_called_once()
    flash_success.assert_not_called()


async def test_squads_post_unknown_action_flashes_error(db_session):
    _, flash_success, flash_error = await _run_squads_post(
        db_session, {"action": "bogus"}
    )
    flash_error.assert_called_once()
    flash_success.assert_not_called()


# --- controller: sale events GET ---------------------------------------------


def _sale_list_fn():
    return SquadsAdminController.sale_events_list.fn


async def _make_sale_event(db_session, name="Sale", *, active_now=True):
    sale_ops = SquadSaleEventOperations(db_session)
    start = datetime.now(timezone.utc) - (
        timedelta(hours=1) if active_now else timedelta(days=10)
    )
    return await sale_ops.create_sale_event(
        guild_id=_GUILD,
        name=name,
        description="desc",
        start_time=start,
        duration_hours=48 if active_now else 1,
        join_discount_percent=10,
        switch_discount_percent=20,
        created_by="admin",
    )


async def test_sale_events_list_renders(db_session):
    # SQLite stores DateTime columns tz-naive, so ``is_currently_active`` (which
    # compares against a tz-aware ``now``) can't run on active rows here. Seed an
    # inactive event: it still appears in the full list but is excluded from the
    # active-events query before any datetime comparison happens.
    event = await _make_sale_event(db_session, "InactiveSale", active_now=True)
    await SquadSaleEventOperations(db_session).toggle_sale_event(event.id, _GUILD)
    with patch(
        f"{_MODULE}.get_admin_context", new=AsyncMock(return_value={})
    ), patch(
        f"{_MODULE}.get_admin_discord_client",
        return_value=SimpleNamespace(get_guild=AsyncMock(return_value=_guild_detail())),
    ), patch(f"{_MODULE}.get_flash_messages", return_value=[]):
        response = await _sale_list_fn()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/squads/sale_events.html"
    assert [e.name for e in response.context["events"]] == ["InactiveSale"]
    assert response.context["active_events"] == []


# --- controller: sale events mutations ---------------------------------------


async def _run_sale_post(db_session, handler, form, **path_kwargs):
    request = SimpleNamespace(form=AsyncMock(return_value=form))
    flash_success, flash_error = Mock(), Mock()
    with patch(f"{_MODULE}.flash_success", flash_success), patch(
        f"{_MODULE}.flash_error", flash_error
    ):
        response = await handler(
            None, request=request, db_session=db_session, guild_id=_GUILD, **path_kwargs
        )
    return response, flash_success, flash_error


async def test_create_sale_event_persists_and_redirects(db_session):
    response, flash_success, flash_error = await _run_sale_post(
        db_session,
        SquadsAdminController.create_sale_event.fn,
        {
            "name": "Blackout",
            "description": "",
            "start_time": "2026-08-01T14:30",
            "duration_hours": "24",
            "join_discount_percent": "50",
        },
    )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/squad-sale-events"
    flash_success.assert_called_once()
    events, _ = await SquadSaleEventOperations(db_session).get_sale_events_by_guild(
        _GUILD
    )
    assert [e.name for e in events] == ["Blackout"]


async def test_create_sale_event_duplicate_name_flashes_conflict(db_session):
    # A duplicate name raises ConflictError from the CRUD layer on Postgres
    # (the unique-constraint name is only surfaced there); patch the op to
    # assert the controller maps that conflict to a flashed error, not a 500.
    with patch.object(
        SquadSaleEventOperations,
        "create_sale_event",
        new=AsyncMock(side_effect=ConflictError("Sale event with name 'Dup' already exists")),
    ):
        _, flash_success, flash_error = await _run_sale_post(
            db_session,
            SquadsAdminController.create_sale_event.fn,
            {
                "name": "Dup",
                "start_time": "2026-08-01T14:30",
                "duration_hours": "24",
            },
        )

    flash_error.assert_called_once()
    flash_success.assert_not_called()


async def test_create_sale_event_invalid_start_time_flashes_error(db_session):
    response, flash_success, flash_error = await _run_sale_post(
        db_session,
        SquadsAdminController.create_sale_event.fn,
        {"name": "Bad", "start_time": "not-a-date", "duration_hours": "24"},
    )

    flash_error.assert_called_once()
    flash_success.assert_not_called()


async def test_edit_sale_event_updates(db_session):
    event = await _make_sale_event(db_session, "Editable", active_now=True)
    response, flash_success, _ = await _run_sale_post(
        db_session,
        SquadsAdminController.edit_sale_event.fn,
        {
            "name": "Edited",
            "start_time": "2026-08-01T14:30",
            "duration_hours": "12",
            "is_active": "true",
        },
        event_id=event.id,
    )

    assert response.url == f"/admin/bot/guilds/{_GUILD}/squad-sale-events"
    flash_success.assert_called_once()
    refreshed = await SquadSaleEventOperations(db_session).get_sale_event_by_id(
        event.id, _GUILD
    )
    assert refreshed.name == "Edited"
    assert refreshed.duration_hours == 12


async def test_edit_sale_event_missing_flashes_error(db_session):
    _, flash_success, flash_error = await _run_sale_post(
        db_session,
        SquadsAdminController.edit_sale_event.fn,
        {"name": "X", "start_time": "2026-08-01T14:30", "duration_hours": "1"},
        event_id=uuid4(),
    )
    flash_error.assert_called_once()
    flash_success.assert_not_called()


async def test_toggle_sale_event_flips_status(db_session):
    event = await _make_sale_event(db_session, "Toggler", active_now=True)
    assert event.is_active is True
    _, flash_success, _ = await _run_sale_post(
        db_session,
        SquadsAdminController.toggle_sale_event.fn,
        {},
        event_id=event.id,
    )

    flash_success.assert_called_once()
    refreshed = await SquadSaleEventOperations(db_session).get_sale_event_by_id(
        event.id, _GUILD
    )
    assert refreshed.is_active is False


async def test_toggle_sale_event_missing_flashes_error(db_session):
    _, flash_success, flash_error = await _run_sale_post(
        db_session,
        SquadsAdminController.toggle_sale_event.fn,
        {},
        event_id=uuid4(),
    )
    flash_error.assert_called_once()
    flash_success.assert_not_called()


async def test_delete_sale_event_removes_it(db_session):
    event = await _make_sale_event(db_session, "Doomed", active_now=True)
    _, flash_success, _ = await _run_sale_post(
        db_session,
        SquadsAdminController.delete_sale_event.fn,
        {},
        event_id=event.id,
    )

    flash_success.assert_called_once()
    events, _ = await SquadSaleEventOperations(db_session).get_sale_events_by_guild(
        _GUILD
    )
    assert events == []


async def test_delete_sale_event_missing_flashes_error(db_session):
    _, flash_success, flash_error = await _run_sale_post(
        db_session,
        SquadsAdminController.delete_sale_event.fn,
        {},
        event_id=uuid4(),
    )
    flash_error.assert_called_once()
    flash_success.assert_not_called()


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        SquadsAdminController.squads_config,
        SquadsAdminController.save_squads_config,
        SquadsAdminController.sale_events_list,
        SquadsAdminController.create_sale_event,
        SquadsAdminController.edit_sale_event,
        SquadsAdminController.toggle_sale_event,
        SquadsAdminController.delete_sale_event,
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
