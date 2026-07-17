"""Native Litestar admin bot API (help-conversation audit).

Ported from the legacy FastAPI ``routers/admin.py`` (prefix ``/admin``) — unit
U9 in docs/v2/legacy-sunset/04-api-rewrite.md. Preserves the exact paths,
verbs, status codes, and request/response shapes so the bot client
(``POST /admin/conversations`` from ``bot/client.py`` / ``plugins/llm.py`` /
``plugins/help.py``) and any external caller need zero changes:

- ``POST   /api/admin/conversations`` → 201, store a help-agent conversation.
- ``GET    /api/admin/conversations`` → paginated listing with filters.
- ``GET    /api/admin/conversations/stats`` → conversation analytics.
- ``GET    /api/admin/conversations/{conversation_id}`` → single conversation.

The legacy-table API key endpoints (``GET /stats`` and the ``/api-keys``
CRUD) were REMOVED in the phase-05 decommission: they operated on the legacy
``public.api_keys`` table, which no longer exists, and Skrift's built-in
``/admin/api-keys`` UI manages the Skrift-native keys. The bot and harness
never called them.

Auth-scope parity (SENSITIVE): the legacy ``verify_admin_permissions`` demanded
an ``admin:read``/``admin:write``/``admin:manage`` scope on every endpoint
except ``POST /conversations``, which took ``bot:write``/``admin:write``
(the bot's write path). Both gates map to :data:`BOT_API_ADMIN_GUARDS`
(``Permission("bot-api-admin")``) — the same mapping the chat-conversations
port used for its ``_require_bot_write`` paths. The bot's service key carries
``bot-api`` and ``bot-api-admin`` (roles.py ``bot-service``); the split exists
so a future narrow key can be minted without code change. A valid key lacking
the permission now answers 401 (Skrift guard) where FastAPI answered 403 —
accepted in 04-api-rewrite.md ("401-parity"); the bot treats both as auth
failures.

INTENTIONAL FIX — ``GET /conversations/stats``: the FastAPI router declared
``/conversations/{conversation_id}`` BEFORE ``/conversations/stats``, so the
literal path ``stats`` was captured by the UUID param and answered **422**
(verified empirically against the mount). Litestar routes static segments
before parameterized ones, so the stats endpoint becomes reachable — resolving
the ambiguity as 04-api-rewrite.md instructs. No caller depends on the 422
(nothing could ever have called stats successfully).

Error-shape parity: bare ``HTTPException`` plain ``{"detail": "<string>"}``
bodies via :func:`errors.plain_error`; malformed UUID path segments answer 422
via :func:`errors.parse_uuid_path`; request-model validation failures answer
422 via the shared exception handlers. The legacy broad ``except`` → 500
wrappers on the conversation endpoints are ported as-is (parity port).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from litestar import Controller, Request, get, post
from litestar.params import Parameter
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.web.api_native.schemas import (
    HelpConversationCreate,
    HelpConversationCreateResponse,
    HelpConversationListResponse,
    HelpConversationResponse,
    HelpConversationStatsResponse,
)
from smarter_dev.web.api_native.auth import bot_api_auth_guard, resolve_request_api_key
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    BotApiException,
    parse_uuid_path,
    plain_error,
)
from smarter_dev.web.models import HelpConversation
from smarter_dev.web.security_logger import get_security_logger

# Permissions granted to the bot's Skrift service key (see roles.py
# `bot-service` role and the phase-01 key-mint runbook). ``bot-api-admin``
# gates every endpoint here: the legacy router demanded an admin scope on all
# of them (bot:write also satisfied the conversations write path — the bot's
# key carries both permissions).
BOT_API_ADMIN_PERMISSION = "bot-api-admin"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_ADMIN_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_ADMIN_PERMISSION)]


class AdminController(Controller):
    """System administration: help-conversation audit."""

    path = "/api/admin"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    # ------------------------------------------------------------------ #
    # Help conversations
    # ------------------------------------------------------------------ #

    @post("/conversations", status_code=HTTP_201_CREATED, guards=BOT_API_ADMIN_GUARDS)
    async def create_conversation(
        self,
        request: Request,
        db_session: AsyncSession,
        data: HelpConversationCreate,
    ) -> HelpConversationCreateResponse:
        """Store a help-agent conversation record (bot write path)."""
        caller = await resolve_request_api_key(request)
        try:
            conversation = HelpConversation(
                session_id=data.session_id,
                guild_id=data.guild_id,
                channel_id=data.channel_id,
                user_id=data.user_id,
                user_username=data.user_username,
                interaction_type=data.interaction_type,
                context_messages=data.context_messages,
                user_question=data.user_question,
                bot_response=data.bot_response,
                tokens_used=data.tokens_used,
                response_time_ms=data.response_time_ms,
                retention_policy=data.retention_policy,
                is_sensitive=data.is_sensitive,
                command_metadata=data.command_metadata,
            )
            db_session.add(conversation)
            await db_session.commit()
            await db_session.refresh(conversation)

            await get_security_logger().log_admin_operation(
                session=db_session,
                operation="create_help_conversation",
                user_identifier=f"bot:{caller.display_name}",
                request=request,
                success=True,
                details=(
                    f"Conversation created for user {data.user_id} "
                    f"in guild {data.guild_id}"
                ),
            )

            return HelpConversationCreateResponse(
                id=conversation.id,
                message="Conversation recorded successfully",
                created_at=conversation.created_at,
            )
        except Exception as create_error:  # Parity port of the legacy 500 wrapper
            await db_session.rollback()
            raise plain_error(
                500, f"Failed to create conversation record: {str(create_error)}"
            )

    @get("/conversations", status_code=HTTP_200_OK, guards=BOT_API_ADMIN_GUARDS)
    async def list_conversations(
        self,
        request: Request,
        db_session: AsyncSession,
        page: int = Parameter(default=1, ge=1),
        size: int = Parameter(default=20, ge=1, le=100),
        guild_id: str | None = None,
        user_id: str | None = None,
        interaction_type: str | None = None,
        resolved_only: bool = False,
        search: str | None = None,
    ) -> HelpConversationListResponse:
        """Paginated conversation listing with guild/user/type/text filters."""
        caller = await resolve_request_api_key(request)
        try:
            query = select(HelpConversation)
            count_query = select(func.count(HelpConversation.id))

            if guild_id:
                query = query.where(HelpConversation.guild_id == guild_id)
                count_query = count_query.where(HelpConversation.guild_id == guild_id)
            if user_id:
                query = query.where(HelpConversation.user_id == user_id)
                count_query = count_query.where(HelpConversation.user_id == user_id)
            if interaction_type:
                query = query.where(
                    HelpConversation.interaction_type == interaction_type
                )
                count_query = count_query.where(
                    HelpConversation.interaction_type == interaction_type
                )
            if resolved_only:
                query = query.where(HelpConversation.is_resolved.is_(True))
                count_query = count_query.where(HelpConversation.is_resolved.is_(True))
            if search:
                search_filter = or_(
                    HelpConversation.user_question.ilike(f"%{search}%"),
                    HelpConversation.bot_response.ilike(f"%{search}%"),
                    HelpConversation.user_username.ilike(f"%{search}%"),
                )
                query = query.where(search_filter)
                count_query = count_query.where(search_filter)

            offset = (page - 1) * size
            query = (
                query.order_by(HelpConversation.started_at.desc())
                .offset(offset)
                .limit(size)
            )

            conversations = (await db_session.execute(query)).scalars().all()
            total = (await db_session.execute(count_query)).scalar()

            conversation_responses = [
                HelpConversationResponse.model_validate(conversation)
                for conversation in conversations
            ]
            pages = math.ceil(total / size) if total > 0 else 1

            await get_security_logger().log_admin_operation(
                session=db_session,
                operation="list_help_conversations",
                user_identifier=caller.display_name,
                request=request,
                success=True,
                details=(
                    f"Listed {len(conversations)} conversations (page {page}/{pages})"
                ),
            )

            return HelpConversationListResponse(
                items=conversation_responses,
                total=total,
                page=page,
                size=size,
                pages=pages,
            )
        except Exception as list_error:  # Parity port of the legacy 500 wrapper
            raise plain_error(500, f"Failed to list conversations: {str(list_error)}")

    @get(
        "/conversations/stats",
        status_code=HTTP_200_OK,
        guards=BOT_API_ADMIN_GUARDS,
    )
    async def get_conversation_stats(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str | None = None,
        days: int = Parameter(default=30, ge=1, le=365),
    ) -> HelpConversationStatsResponse:
        """Conversation analytics (counts, tokens, response times, top users).

        Reachable in the native port — the FastAPI route was shadowed by the
        ``{conversation_id}`` param and always answered 422 (see module doc).
        """
        caller = await resolve_request_api_key(request)
        try:
            end_date = datetime.now(timezone.utc)
            start_date = end_date - timedelta(days=days)
            today_start = end_date.replace(hour=0, minute=0, second=0, microsecond=0)

            base_filter = HelpConversation.started_at >= start_date
            today_filter = HelpConversation.started_at >= today_start
            if guild_id:
                base_filter = base_filter & (HelpConversation.guild_id == guild_id)
                today_filter = today_filter & (HelpConversation.guild_id == guild_id)

            total_conversations = (
                await db_session.execute(
                    select(func.count(HelpConversation.id)).where(base_filter)
                )
            ).scalar() or 0
            conversations_today = (
                await db_session.execute(
                    select(func.count(HelpConversation.id)).where(today_filter)
                )
            ).scalar() or 0
            total_tokens = (
                await db_session.execute(
                    select(func.sum(HelpConversation.tokens_used)).where(base_filter)
                )
            ).scalar() or 0
            tokens_today = (
                await db_session.execute(
                    select(func.sum(HelpConversation.tokens_used)).where(today_filter)
                )
            ).scalar() or 0

            avg_response_time = (
                await db_session.execute(
                    select(func.avg(HelpConversation.response_time_ms)).where(
                        base_filter & (HelpConversation.response_time_ms.is_not(None))
                    )
                )
            ).scalar()
            avg_response_time_ms = int(avg_response_time) if avg_response_time else None

            top_users_query = (
                select(
                    HelpConversation.user_username,
                    HelpConversation.user_id,
                    func.count(HelpConversation.id).label("conversation_count"),
                    func.sum(HelpConversation.tokens_used).label("total_tokens"),
                )
                .where(base_filter)
                .group_by(HelpConversation.user_username, HelpConversation.user_id)
                .order_by(func.count(HelpConversation.id).desc())
                .limit(10)
            )
            top_users = [
                {
                    "username": row.user_username,
                    "user_id": row.user_id,
                    "conversation_count": row.conversation_count,
                    "total_tokens": row.total_tokens or 0,
                }
                for row in await db_session.execute(top_users_query)
            ]

            types_query = (
                select(
                    HelpConversation.interaction_type,
                    func.count(HelpConversation.id).label("count"),
                )
                .where(base_filter)
                .group_by(HelpConversation.interaction_type)
            )
            conversation_types = {
                row.interaction_type: row.count
                for row in await db_session.execute(types_query)
            }

            resolved_count = (
                await db_session.execute(
                    select(func.count(HelpConversation.id)).where(
                        base_filter & (HelpConversation.is_resolved.is_(True))
                    )
                )
            ).scalar() or 0
            resolution_rate = (
                (resolved_count / total_conversations * 100)
                if total_conversations > 0
                else 0.0
            )

            await get_security_logger().log_admin_operation(
                session=db_session,
                operation="view_help_conversation_stats",
                user_identifier=caller.display_name,
                request=request,
                success=True,
                details=(
                    f"Viewed conversation stats for {days} days"
                    + (f" in guild {guild_id}" if guild_id else "")
                ),
            )

            return HelpConversationStatsResponse(
                total_conversations=total_conversations,
                conversations_today=conversations_today,
                total_tokens_used=total_tokens,
                tokens_used_today=tokens_today,
                average_response_time_ms=avg_response_time_ms,
                top_users=top_users,
                conversation_types=conversation_types,
                resolution_rate=resolution_rate,
            )
        except Exception as stats_error:  # Parity port of the legacy 500 wrapper
            raise plain_error(
                500, f"Failed to get conversation stats: {str(stats_error)}"
            )

    @get(
        "/conversations/{conversation_id:str}",
        status_code=HTTP_200_OK,
        guards=BOT_API_ADMIN_GUARDS,
    )
    async def get_conversation(
        self,
        request: Request,
        db_session: AsyncSession,
        conversation_id: str,
    ) -> HelpConversationResponse:
        """Full detail of one conversation (context, question, response, metrics)."""
        caller = await resolve_request_api_key(request)
        parsed_conversation_id = parse_uuid_path(conversation_id, "conversation_id")
        try:
            conversation = (
                await db_session.execute(
                    select(HelpConversation).where(
                        HelpConversation.id == parsed_conversation_id
                    )
                )
            ).scalar_one_or_none()
            if not conversation:
                raise plain_error(404, "Conversation not found")

            await get_security_logger().log_admin_operation(
                session=db_session,
                operation="view_help_conversation",
                user_identifier=caller.display_name,
                request=request,
                success=True,
                details=f"Viewed conversation {parsed_conversation_id}",
            )

            return HelpConversationResponse.model_validate(conversation)
        except BotApiException:
            raise
        except Exception as get_error:  # Parity port of the legacy 500 wrapper
            raise plain_error(500, f"Failed to get conversation: {str(get_error)}")
