"""Help-conversation audit pages for the Skrift admin panel.

Ports ``conversations_list``, ``conversation_detail`` and
``cleanup_expired_conversations`` from the legacy ``smarter_dev.web.admin.views``
onto a Skrift-native Litestar controller under ``/admin``. Help conversations
are their own admin area (own nav entry), not a per-guild bot page, so this
controller lives beside the other stand-alone dashboards rather than under the
``/admin/bot`` guild tree.

Filter parsing and the retention aggregation/deletion are factored into pure,
module-level helpers so the accepted-field contract and the cleanup arithmetic
can be unit-tested without a request object.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.exceptions import NotFoundException
from litestar.params import Parameter
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard
from skrift.flash import flash_success, get_flash_messages

from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    DiscordGuildSummary,
    GuildNotFoundError,
    get_admin_discord_client,
)
from smarter_dev.web.models import HelpConversation

logger = logging.getLogger(__name__)

_PAGE_SIZE_DEFAULT = 20
_PAGE_SIZE_MAX = 100

# Retention policy names shown on the cleanup page, mapped to their human window.
_RETENTION_POLICIES: tuple[str, ...] = ("standard", "minimal", "sensitive")


@dataclass(frozen=True)
class ConversationFilters:
    """The list-view filter selection, normalised from raw query params."""

    guild_id: str | None
    user_id: str | None
    interaction_type: str | None
    search: str | None
    resolved_only: bool


def parse_conversation_filters(params: Mapping[str, str | None]) -> ConversationFilters:
    """Normalise raw query params into a :class:`ConversationFilters`.

    Pure function — no I/O — so the accepted-field contract is unit-testable.
    Blank strings collapse to ``None`` so an empty filter box does not become a
    ``WHERE column = ''`` clause.
    """

    def cleaned(key: str) -> str | None:
        value = params.get(key)
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    return ConversationFilters(
        guild_id=cleaned("guild_id"),
        user_id=cleaned("user_id"),
        interaction_type=cleaned("interaction_type"),
        search=cleaned("search"),
        resolved_only=params.get("resolved_only") == "true",
    )


def apply_conversation_filters(
    stmt: Select, filters: ConversationFilters
) -> Select:
    """Return ``stmt`` narrowed by every active filter.

    Works on both the row query and the count query because it only adds
    ``WHERE`` clauses. SQLAlchemy ``Select`` objects are immutable, so this
    returns a new statement and does not mutate the argument.
    """
    if filters.guild_id:
        stmt = stmt.where(HelpConversation.guild_id == filters.guild_id)
    if filters.user_id:
        stmt = stmt.where(HelpConversation.user_id == filters.user_id)
    if filters.interaction_type:
        stmt = stmt.where(
            HelpConversation.interaction_type == filters.interaction_type
        )
    if filters.resolved_only:
        stmt = stmt.where(HelpConversation.is_resolved.is_(True))
    if filters.search:
        pattern = f"%{filters.search}%"
        stmt = stmt.where(
            or_(
                HelpConversation.user_question.ilike(pattern),
                HelpConversation.bot_response.ilike(pattern),
                HelpConversation.user_username.ilike(pattern),
            )
        )
    return stmt


@dataclass(frozen=True)
class RetentionBreakdown:
    """Per-policy counts plus the expired total, for the cleanup page cards."""

    standard: int
    minimal: int
    sensitive: int
    expired: int

    @property
    def total(self) -> int:
        return self.standard + self.minimal + self.sensitive


async def summarize_retention(
    db_session: AsyncSession, now: datetime
) -> RetentionBreakdown:
    """Count conversations by retention policy and how many have expired.

    ``expired`` counts every row whose ``expires_at`` is at or before ``now``
    regardless of policy — those are exactly the rows :func:`delete_expired`
    would remove.
    """
    policy_counts: dict[str, int] = {}
    for policy in _RETENTION_POLICIES:
        count = await db_session.scalar(
            select(func.count(HelpConversation.id)).where(
                HelpConversation.retention_policy == policy
            )
        )
        policy_counts[policy] = count or 0

    expired = await db_session.scalar(
        select(func.count(HelpConversation.id)).where(
            HelpConversation.expires_at <= now
        )
    )

    return RetentionBreakdown(
        standard=policy_counts["standard"],
        minimal=policy_counts["minimal"],
        sensitive=policy_counts["sensitive"],
        expired=expired or 0,
    )


async def delete_expired_conversations(
    db_session: AsyncSession, now: datetime
) -> int:
    """Delete every conversation whose retention window has elapsed.

    Returns the number of rows removed. Does not commit — the caller owns the
    transaction boundary so the count and the commit stay together.
    """
    expired = await db_session.execute(
        select(HelpConversation).where(HelpConversation.expires_at <= now)
    )
    conversations = list(expired.scalars().all())
    for conversation in conversations:
        await db_session.delete(conversation)
    return len(conversations)


async def _list_bot_guilds_or_empty() -> list[DiscordGuildSummary]:
    """Best-effort guild list for the filter dropdown; ``[]`` on any failure.

    The list page must render even when Discord is unreachable, so a failed
    guild fetch degrades to an empty dropdown rather than a 5xx.
    """
    try:
        client = get_admin_discord_client()
        return await client.list_bot_guilds()
    except DiscordAdminError as exc:
        logger.warning("Could not load guild list for help-conversations filter: %s", exc)
        return []


class HelpConversationsAdminController(Controller):
    """Audit and retention pages for help-agent conversations."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/help-conversations",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={
            "label": "Help Conversations",
            "icon": "messages",
            "order": 66,
        },
    )
    async def list_conversations(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: Annotated[str | None, Parameter(query="guild_id")] = None,
        user_id: Annotated[str | None, Parameter(query="user_id")] = None,
        interaction_type: Annotated[
            str | None, Parameter(query="interaction_type")
        ] = None,
        search: Annotated[str | None, Parameter(query="search")] = None,
        resolved_only: Annotated[
            str | None, Parameter(query="resolved_only")
        ] = None,
        page: Annotated[int, Parameter(query="page")] = 1,
        size: Annotated[int, Parameter(query="size")] = _PAGE_SIZE_DEFAULT,
    ) -> TemplateResponse:
        """List help conversations with filtering and pagination."""
        ctx = await get_admin_context(request, db_session)

        filters = parse_conversation_filters(
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "interaction_type": interaction_type,
                "search": search,
                "resolved_only": resolved_only,
            }
        )
        page = max(1, page)
        size = max(1, min(size, _PAGE_SIZE_MAX))

        total = (
            await db_session.scalar(
                apply_conversation_filters(
                    select(func.count(HelpConversation.id)), filters
                )
            )
        ) or 0

        offset = (page - 1) * size
        rows = await db_session.execute(
            apply_conversation_filters(select(HelpConversation), filters)
            .order_by(HelpConversation.started_at.desc())
            .offset(offset)
            .limit(size)
        )
        conversations = list(rows.scalars().all())

        guilds = await _list_bot_guilds_or_empty()

        return TemplateResponse(
            "admin/bot/help_conversations/list.html",
            context={
                "conversations": conversations,
                "total": total,
                "page": page,
                "size": size,
                "total_pages": max(1, (total + size - 1) // size),
                "guilds": guilds,
                "filters": {
                    "guild_id": filters.guild_id,
                    "user_id": filters.user_id,
                    "interaction_type": filters.interaction_type,
                    "search": filters.search,
                    "resolved_only": filters.resolved_only,
                },
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @get(
        "/help-conversations/cleanup",
        guards=[auth_guard, Permission("administrator")],
    )
    async def cleanup_form(
        self, request: Request, db_session: AsyncSession
    ) -> TemplateResponse:
        """Show the retention-cleanup confirmation page with live counts."""
        ctx = await get_admin_context(request, db_session)
        breakdown = await summarize_retention(db_session, datetime.now(UTC))

        return TemplateResponse(
            "admin/bot/help_conversations/cleanup.html",
            context={
                "standard_count": breakdown.standard,
                "minimal_count": breakdown.minimal,
                "sensitive_count": breakdown.sensitive,
                "expired_count": breakdown.expired,
                "total_count": breakdown.total,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/help-conversations/cleanup",
        guards=[auth_guard, Permission("administrator")],
    )
    async def run_cleanup(
        self, request: Request, db_session: AsyncSession
    ) -> Redirect:
        """Delete expired conversations, flash the result, and return to the page."""
        deleted = await delete_expired_conversations(db_session, datetime.now(UTC))
        await db_session.commit()

        logger.info("Cleaned up %d expired help conversations", deleted)
        plural = "s" if deleted != 1 else ""
        flash_success(
            request,
            f"Successfully cleaned up {deleted} expired conversation{plural}.",
        )
        return Redirect(path="/admin/help-conversations/cleanup")

    @get(
        "/help-conversations/{conversation_id:uuid}",
        guards=[auth_guard, Permission("administrator")],
    )
    async def conversation_detail(
        self,
        request: Request,
        db_session: AsyncSession,
        conversation_id: UUID,
    ) -> TemplateResponse:
        """Show one conversation's full transcript and metadata."""
        ctx = await get_admin_context(request, db_session)

        conversation = await db_session.scalar(
            select(HelpConversation).where(HelpConversation.id == conversation_id)
        )
        if conversation is None:
            raise NotFoundException(detail="Conversation not found")

        guild = await _resolve_guild_display(conversation.guild_id)

        return TemplateResponse(
            "admin/bot/help_conversations/detail.html",
            context={
                "conversation": conversation,
                "guild": guild,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )


@dataclass(frozen=True)
class GuildDisplay:
    """The minimal guild identity the detail page shows in its header."""

    id: str
    name: str


async def _resolve_guild_display(guild_id: str) -> GuildDisplay:
    """Best-effort guild name for the detail header; falls back to the raw id.

    A missing guild or an unreachable Discord must not 500 the audit page — the
    conversation row is the point of the page, the guild name is decoration.
    """
    try:
        client = get_admin_discord_client()
        guild = await client.get_guild(guild_id)
        return GuildDisplay(id=guild.id, name=guild.name)
    except (GuildNotFoundError, DiscordAdminError) as exc:
        logger.warning("Could not resolve guild %s for conversation detail: %s", guild_id, exc)
        return GuildDisplay(id=guild_id, name=f"Guild {guild_id}")
