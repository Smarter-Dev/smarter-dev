"""Tests for the Skrift admin guild list + detail controller."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.bot_admin.guilds import (
    GuildAdminController,
    collect_guild_overview,
)
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    DiscordGuildDetail,
    DiscordGuildSummary,
    GuildNotFoundError,
)
from smarter_dev.web.models import (
    BytesBalance,
    BytesTransaction,
    Squad,
    SquadMembership,
)

_GUILD = "111111111111111111"
_OTHER_GUILD = "222222222222222222"


async def _seed_guild(db_session) -> None:
    """Seed one guild with balances, transactions, and a squad."""
    db_session.add_all(
        [
            BytesBalance(guild_id=_GUILD, user_id="u_low", balance=50),
            BytesBalance(guild_id=_GUILD, user_id="u_high", balance=500),
            BytesBalance(guild_id=_GUILD, user_id="u_mid", balance=200),
            # A balance in another guild that must never leak in.
            BytesBalance(guild_id=_OTHER_GUILD, user_id="u_other", balance=9999),
        ]
    )
    db_session.add_all(
        [
            BytesTransaction(
                guild_id=_GUILD,
                giver_id="u_high",
                giver_username="High",
                receiver_id="u_low",
                receiver_username="Low",
                amount=10,
                reason="first",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            BytesTransaction(
                guild_id=_GUILD,
                giver_id="u_mid",
                giver_username="Mid",
                receiver_id="u_low",
                receiver_username="Low",
                amount=20,
                reason="second",
                created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            ),
        ]
    )
    squad = Squad(guild_id=_GUILD, role_id="role1", name="Alpha")
    db_session.add(squad)
    await db_session.flush()
    db_session.add_all(
        [
            SquadMembership(squad_id=squad.id, user_id="u_low", guild_id=_GUILD),
            SquadMembership(squad_id=squad.id, user_id="u_mid", guild_id=_GUILD),
        ]
    )
    await db_session.commit()


# --- aggregation (happy path) ------------------------------------------------


async def test_collect_guild_overview_ranks_and_scopes(db_session):
    await _seed_guild(db_session)

    overview = await collect_guild_overview(db_session, _GUILD)

    # Ranked by balance descending; only this guild's users.
    assert [(u.rank, u.user_id, u.balance) for u in overview.top_users] == [
        (1, "u_high", 500),
        (2, "u_mid", 200),
        (3, "u_low", 50),
    ]
    # Stats exclude the other guild's 9999 balance.
    assert overview.stats.total_users == 3
    assert overview.stats.total_balance == 750
    assert overview.stats.total_transactions == 2
    assert overview.stats.squad_count == 1


async def test_collect_guild_overview_recent_transactions_newest_first(db_session):
    await _seed_guild(db_session)

    overview = await collect_guild_overview(db_session, _GUILD)

    reasons = [tx.reason for tx in overview.recent_transactions]
    assert reasons == ["second", "first"]


async def test_collect_guild_overview_squad_member_counts(db_session):
    await _seed_guild(db_session)

    overview = await collect_guild_overview(db_session, _GUILD)

    assert len(overview.squads) == 1
    squad = overview.squads[0]
    assert squad.name == "Alpha"
    assert squad.member_count == 2


async def test_collect_guild_overview_empty_guild(db_session):
    overview = await collect_guild_overview(db_session, "999")

    assert overview.top_users == []
    assert overview.recent_transactions == []
    assert overview.squads == []
    assert overview.stats.total_users == 0
    assert overview.stats.total_balance == 0
    assert overview.stats.total_transactions == 0


# --- controller: guild list --------------------------------------------------


def _guild_list_fn():
    return GuildAdminController.guild_list.fn


def _guild_detail_fn():
    return GuildAdminController.guild_detail.fn


async def test_guild_list_renders_guilds(db_session):
    client = SimpleNamespace(
        list_bot_guilds=AsyncMock(
            return_value=[DiscordGuildSummary(id="1", name="First", icon=None)]
        )
    )
    with patch(
        "smarter_dev.web.bot_admin.guilds.get_admin_context",
        new=AsyncMock(return_value={}),
    ), patch(
        "smarter_dev.web.bot_admin.guilds.get_admin_discord_client",
        return_value=client,
    ):
        response = await _guild_list_fn()(None, request=object(), db_session=db_session)

    assert response.template_name == "admin/bot/guilds/list.html"
    assert [g.name for g in response.context["guilds"]] == ["First"]
    assert response.context["error"] is None


async def test_guild_list_handles_discord_error(db_session):
    client = SimpleNamespace(
        list_bot_guilds=AsyncMock(side_effect=DiscordAdminError("rate limited"))
    )
    with patch(
        "smarter_dev.web.bot_admin.guilds.get_admin_context",
        new=AsyncMock(return_value={}),
    ), patch(
        "smarter_dev.web.bot_admin.guilds.get_admin_discord_client",
        return_value=client,
    ):
        response = await _guild_list_fn()(None, request=object(), db_session=db_session)

    assert response.context["guilds"] == []
    assert "rate limited" in response.context["error"]


# --- controller: guild detail ------------------------------------------------


async def test_guild_detail_renders_detail_and_stats(db_session):
    await _seed_guild(db_session)
    guild = DiscordGuildDetail(
        id=_GUILD,
        name="Alpha Guild",
        icon=None,
        owner_id="owner",
        member_count=42,
        description=None,
    )
    client = SimpleNamespace(
        get_guild=AsyncMock(return_value=guild),
        list_bot_guilds=AsyncMock(return_value=[]),
    )
    with patch(
        "smarter_dev.web.bot_admin.guilds.get_admin_context",
        new=AsyncMock(return_value={}),
    ), patch(
        "smarter_dev.web.bot_admin.guilds.get_admin_discord_client",
        return_value=client,
    ):
        response = await _guild_detail_fn()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.template_name == "admin/bot/guilds/detail.html"
    assert response.context["guild"].name == "Alpha Guild"
    assert response.context["stats"].total_balance == 750
    assert response.context["top_users"][0].user_id == "u_high"


async def test_guild_detail_not_found_returns_404(db_session):
    client = SimpleNamespace(
        get_guild=AsyncMock(side_effect=GuildNotFoundError("nope")),
        list_bot_guilds=AsyncMock(return_value=[]),
    )
    with patch(
        "smarter_dev.web.bot_admin.guilds.get_admin_context",
        new=AsyncMock(return_value={}),
    ), patch(
        "smarter_dev.web.bot_admin.guilds.get_admin_discord_client",
        return_value=client,
    ):
        response = await _guild_detail_fn()(
            None, request=object(), db_session=db_session, guild_id="missing"
        )

    assert response.status_code == 404
    assert response.template_name == "admin/bot/guilds/error.html"
    assert response.context["error_code"] == 404


async def test_guild_detail_discord_error_returns_503(db_session):
    client = SimpleNamespace(
        get_guild=AsyncMock(side_effect=DiscordAdminError("upstream boom")),
        list_bot_guilds=AsyncMock(return_value=[]),
    )
    with patch(
        "smarter_dev.web.bot_admin.guilds.get_admin_context",
        new=AsyncMock(return_value={}),
    ), patch(
        "smarter_dev.web.bot_admin.guilds.get_admin_discord_client",
        return_value=client,
    ):
        response = await _guild_detail_fn()(
            None, request=object(), db_session=db_session, guild_id=_GUILD
        )

    assert response.status_code == 503
    assert response.context["error_code"] == 503


# --- auth wiring -------------------------------------------------------------


@pytest.mark.parametrize("handler", [GuildAdminController.guild_list, GuildAdminController.guild_detail])
def test_routes_require_admin(handler):
    guards = handler.guards
    assert auth_guard in guards
    admin_guards = [
        g for g in guards
        if isinstance(g, Permission) and g.permission == "administrator"
    ]
    assert admin_guards, "route must require the administrator permission"


async def test_administrator_permission_denies_non_admin_and_allows_admin():
    guard = Permission("administrator")
    non_admin = SimpleNamespace(permissions={"view-drafts"}, roles=set())
    admin = SimpleNamespace(permissions={"administrator"}, roles=set())

    assert await guard.check(non_admin) is False
    assert await guard.check(admin) is True
