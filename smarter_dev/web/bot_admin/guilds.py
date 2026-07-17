"""Guild list and guild detail pages for the Skrift admin panel.

Ports ``guild_list`` and ``guild_detail`` from the legacy
``smarter_dev.web.admin.views`` onto Skrift-native Litestar routes under
``/admin/bot``. Guild identity comes from Discord (via
:mod:`smarter_dev.web.discord_admin_client`); the per-guild stats come from
the bytes economy tables.
"""

from __future__ import annotations

from dataclasses import dataclass

from litestar import Controller, Request, get
from litestar.response import Template as TemplateResponse
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.discord_admin_client import (
    DiscordAdminClient,
    DiscordAdminError,
    DiscordGuildDetail,
    GuildNotFoundError,
    get_admin_discord_client,
)
from smarter_dev.web.models import (
    BytesBalance,
    BytesTransaction,
    Squad,
    SquadMembership,
)

_RECENT_TRANSACTION_LIMIT = 20
_TOP_USER_LIMIT = 10


@dataclass(frozen=True)
class GuildLeaderboardEntry:
    """One row of the guild's top-balances table, ready for templating."""

    rank: int
    user_id: str
    balance: int


@dataclass(frozen=True)
class GuildSquadView:
    """A squad plus its live member count, ready for templating."""

    name: str
    description: str | None
    is_active: bool
    member_count: int
    max_members: int | None
    switch_cost: int


@dataclass(frozen=True)
class GuildStats:
    """Aggregate economy stats shown at the top of the guild detail page."""

    total_users: int
    total_balance: int
    total_transactions: int
    squad_count: int


@dataclass(frozen=True)
class GuildOverview:
    """Everything the guild detail page needs from the database."""

    top_users: list[GuildLeaderboardEntry]
    recent_transactions: list[BytesTransaction]
    squads: list[GuildSquadView]
    stats: GuildStats


async def _load_squad_views(
    db_session: AsyncSession, guild_id: str
) -> list[GuildSquadView]:
    """Load active squads for a guild with their current member counts."""
    squads_result = await db_session.execute(
        select(Squad)
        .where(Squad.guild_id == guild_id, Squad.is_active.is_(True))
        .order_by(Squad.name)
    )
    squads = list(squads_result.scalars().all())
    if not squads:
        return []

    counts_result = await db_session.execute(
        select(SquadMembership.squad_id, func.count())
        .where(SquadMembership.guild_id == guild_id)
        .group_by(SquadMembership.squad_id)
    )
    member_counts = dict(counts_result.all())

    return [
        GuildSquadView(
            name=squad.name,
            description=squad.description,
            is_active=squad.is_active,
            member_count=member_counts.get(squad.id, 0),
            max_members=squad.max_members,
            switch_cost=squad.switch_cost,
        )
        for squad in squads
    ]


async def collect_guild_overview(
    db_session: AsyncSession, guild_id: str
) -> GuildOverview:
    """Gather every DB-backed figure the guild detail page renders.

    Pure read; no Discord calls and no mutation. Kept separate from the route
    handler so the aggregation can be tested against a seeded session.
    """
    leaderboard_result = await db_session.execute(
        select(BytesBalance.user_id, BytesBalance.balance)
        .where(BytesBalance.guild_id == guild_id)
        .order_by(BytesBalance.balance.desc())
        .limit(_TOP_USER_LIMIT)
    )
    top_users = [
        GuildLeaderboardEntry(rank=index, user_id=user_id, balance=balance)
        for index, (user_id, balance) in enumerate(leaderboard_result.all(), start=1)
    ]

    transactions_result = await db_session.execute(
        select(BytesTransaction)
        .where(BytesTransaction.guild_id == guild_id)
        .order_by(BytesTransaction.created_at.desc())
        .limit(_RECENT_TRANSACTION_LIMIT)
    )
    recent_transactions = list(transactions_result.scalars().all())

    squads = await _load_squad_views(db_session, guild_id)

    stats_result = await db_session.execute(
        select(
            func.count(distinct(BytesBalance.user_id)).label("total_users"),
            func.coalesce(func.sum(BytesBalance.balance), 0).label("total_balance"),
        ).where(BytesBalance.guild_id == guild_id)
    )
    stats_row = stats_result.one()

    transaction_count = await db_session.scalar(
        select(func.count())
        .select_from(BytesTransaction)
        .where(BytesTransaction.guild_id == guild_id)
    )

    stats = GuildStats(
        total_users=stats_row.total_users or 0,
        total_balance=stats_row.total_balance or 0,
        total_transactions=transaction_count or 0,
        squad_count=len(squads),
    )

    return GuildOverview(
        top_users=top_users,
        recent_transactions=recent_transactions,
        squads=squads,
        stats=stats,
    )


class GuildAdminController(Controller):
    """Guild list and detail pages under ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    @get(
        "/guilds",
        guards=[auth_guard, Permission("administrator")],
    )
    async def guild_list(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """List every guild the bot is a member of."""
        ctx = await get_admin_context(request, db_session)

        client = get_admin_discord_client()
        try:
            guilds = await client.list_bot_guilds()
            error = None
        except DiscordAdminError as exc:
            guilds = []
            error = f"Discord API error: {exc}"

        return TemplateResponse(
            "admin/bot/guilds/list.html",
            context={
                "guilds": guilds,
                "error": error,
                "active_page": "guilds",
                "guild_id": None,
                **ctx,
            },
        )

    @get(
        "/guilds/{guild_id:str}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def guild_detail(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Show one guild's Discord identity and economy stats."""
        ctx = await get_admin_context(request, db_session)

        client = get_admin_discord_client()
        try:
            guild = await client.get_guild(guild_id)
        except GuildNotFoundError:
            return TemplateResponse(
                "admin/bot/guilds/error.html",
                context={
                    "error": f"Guild {guild_id} not found or bot is not a member.",
                    "error_code": 404,
                    "active_page": "guilds",
                    "guild_id": guild_id,
                    **ctx,
                },
                status_code=404,
            )
        except DiscordAdminError as exc:
            return TemplateResponse(
                "admin/bot/guilds/error.html",
                context={
                    "error": f"Discord API error: {exc}",
                    "error_code": 503,
                    "active_page": "guilds",
                    "guild_id": guild_id,
                    **ctx,
                },
                status_code=503,
            )

        overview = await collect_guild_overview(db_session, guild_id)
        all_guilds = await _all_guilds_or_fallback(client, guild)

        return TemplateResponse(
            "admin/bot/guilds/detail.html",
            context={
                "guild": guild,
                "guilds": all_guilds,
                "top_users": overview.top_users,
                "recent_transactions": overview.recent_transactions,
                "squads": overview.squads,
                "stats": overview.stats,
                "active_page": "guilds",
                "guild_id": guild_id,
                **ctx,
            },
        )


async def _all_guilds_or_fallback(
    client: DiscordAdminClient, current: DiscordGuildDetail
) -> list:
    """List all guilds for the picker, falling back to just the current one."""
    try:
        return await client.list_bot_guilds()
    except DiscordAdminError:
        return [current]
