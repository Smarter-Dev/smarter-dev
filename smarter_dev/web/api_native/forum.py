"""Native Litestar port of the forum bot API (unit U7).

Ports the legacy FastAPI ``routers/forum_agents_simple.py`` (prefix
``/guilds/{guild_id}/forum-agents``) and ``routers/forum_notifications.py`` (no
prefix — full paths) — see docs/v2/legacy-sunset/04-api-rewrite.md. Preserves
the exact paths, verbs, status codes, and request/response shapes of the FastAPI
implementation so ``smarter_dev/bot/services/forum_agent_service.py`` and
``smarter_dev/bot/plugins/forum_notifications.py`` (and any external caller) need
zero changes.

NOT registered in ``app.yaml`` yet — the FastAPI mount still owns ``/api``. This
module exists for isolated parity tests until the atomic switchover.

Session note: post phase-02 the two legacy sessions collapse into the single
injected ``db_session``. ``forum_agents_simple`` used the request-scoped
``get_database_session`` and ``forum_notifications`` used the module-level
``get_db_session``; both are replaced by the injected ``db_session`` here.

Error-shape parity:
- ``forum_agents_simple`` used the *nested* ``exceptions.create_validation_error``
  / ``create_not_found_error`` bodies (a full ``ErrorResponse`` mapping under
  ``detail``, ``request_id=None`` because the router passed no request) —
  reproduced via :func:`errors.nested_validation_error` /
  :func:`errors.nested_not_found_error`. Its catch-all wraps every other failure
  in a *plain* ``{"detail": "Failed to ...: <exc>"}`` 500.
- ``forum_notifications`` answered every failure with a bare ``HTTPException`` — a
  plain ``{"detail": "<string>"}`` body — reproduced via :func:`errors.plain_error`.

Rate-limiting parity is deferred to the switchover commit (see the plan's
"Rate-limiting parity" section); the FastAPI mount still enforces those windows
in production until switchover.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from litestar import Controller, get, post, put
from litestar.exceptions import ValidationException
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission, auth_guard

from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    BotApiException,
    nested_not_found_error,
    nested_validation_error,
    plain_error,
)
from smarter_dev.web.crud import ForumAgentOperations
from smarter_dev.web.models import (
    ForumAgentResponse,
    ForumNotificationTopic,
    ForumUserSubscription,
)

logger = logging.getLogger(__name__)

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute. See the bytes
# controller and docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


def _validate_discord_id(value: str, field_name: str) -> str:
    """Validate a Discord snowflake, raising the nested 400 the FastAPI API used.

    Mirrors ``smarter_dev.web.api.exceptions.validate_discord_id`` (nested
    ``validation_error`` body, ``request_id=None``).
    """
    try:
        if int(value) <= 0:
            raise ValueError("ID must be positive")
        return value
    except ValueError:
        raise nested_validation_error(f"Invalid {field_name} format")


def _parse_agent_id(value: str) -> UUID:
    """Parse an ``agent_id`` path segment, matching FastAPI's 422 on bad format.

    The FastAPI route declared ``agent_id`` as ``UUID``, so a malformed value
    produced a 422 ``RequestValidationError``. Declaring the Litestar param as
    ``str`` and parsing here reproduces that 422 (via
    :func:`errors.handle_validation_exception`) instead of a route-miss 404.
    """
    try:
        return UUID(value)
    except ValueError as parse_error:
        raise ValidationException(
            detail="Invalid agent_id format",
            extra=[{"key": "agent_id", "message": "value is not a valid uuid"}],
        ) from parse_error


class ForumAgentController(Controller):
    """Forum agent listing and response recording."""

    path = "/api/guilds/{guild_id:str}/forum-agents"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_forum_agents(
        self,
        db_session: AsyncSession,
        guild_id: str,
    ) -> list[dict]:
        """Get all forum agents for a guild.

        The legacy router serialized agents to plain dicts (with the path
        ``guild_id`` echoed back) and answered any failure with a plain 500
        ``{"detail": "Failed to retrieve forum agents: <exc>"}``.
        """
        _validate_discord_id(guild_id, "guild_id")
        try:
            forum_ops = ForumAgentOperations(db_session)
            agents = await forum_ops.list_agents(guild_id)
            return [
                {
                    "id": str(agent.id),
                    "guild_id": guild_id,
                    "name": agent.name,
                    "description": agent.description,
                    "system_prompt": agent.system_prompt,
                    "monitored_forums": agent.monitored_forums,
                    "is_active": agent.is_active,
                    "enable_responses": agent.enable_responses,
                    "enable_user_tagging": agent.enable_user_tagging,
                    "response_threshold": agent.response_threshold,
                    "max_responses_per_hour": agent.max_responses_per_hour,
                    "created_by": agent.created_by,
                    "created_at": agent.created_at.isoformat() if agent.created_at else None,
                    "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
                }
                for agent in agents
            ]
        except Exception as error:
            raise plain_error(500, f"Failed to retrieve forum agents: {error}")

    @post("/{agent_id:str}/responses", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def record_agent_response(
        self,
        db_session: AsyncSession,
        guild_id: str,
        agent_id: str,
        data: dict,
    ) -> dict:
        """Record a forum agent response."""
        # FastAPI validated the ``agent_id`` UUID path param (422) before the
        # handler body ran its ``guild_id`` snowflake check (400) — same order.
        parsed_agent_id = _parse_agent_id(agent_id)
        _validate_discord_id(guild_id, "guild_id")
        try:
            forum_ops = ForumAgentOperations(db_session)
            agent = await forum_ops.get_agent(parsed_agent_id, guild_id)
            if not agent:
                raise nested_not_found_error("Forum agent not found")

            response_record = ForumAgentResponse(
                id=uuid4(),
                agent_id=parsed_agent_id,
                guild_id=guild_id,
                channel_id=data.get("channel_id", ""),
                thread_id=data.get("thread_id", ""),
                post_title=data.get("post_title", ""),
                post_content=data.get("post_content", ""),
                author_display_name=data.get("author_display_name", "Unknown"),
                post_tags=data.get("post_tags", []),
                attachments=data.get("attachments", []),
                decision_reason=data.get("decision_reason", ""),
                confidence_score=data.get("confidence_score", 0.0),
                response_content=data.get("response_content", ""),
                tokens_used=data.get("tokens_used", 0),
                response_time_ms=data.get("response_time_ms", 0),
                responded=data.get("responded", False),
                created_at=datetime.now(timezone.utc),
            )

            db_session.add(response_record)
            await db_session.commit()
            await db_session.refresh(response_record)

            return {
                "id": str(response_record.id),
                "created_at": response_record.created_at.isoformat(),
            }
        except BotApiException:
            raise
        except Exception as error:
            raise plain_error(500, f"Failed to record agent response: {error}")

    @get("/{agent_id:str}/responses/count", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_agent_response_count(
        self,
        db_session: AsyncSession,
        guild_id: str,
        agent_id: str,
        hours: int = 1,
    ) -> dict:
        """Get the count of actual agent responses within a time period."""
        # FastAPI validated the ``agent_id`` UUID path param (422) before the
        # handler body ran its ``guild_id`` snowflake check (400) — same order.
        parsed_agent_id = _parse_agent_id(agent_id)
        _validate_discord_id(guild_id, "guild_id")
        try:
            forum_ops = ForumAgentOperations(db_session)
            agent = await forum_ops.get_agent(parsed_agent_id, guild_id)
            if not agent:
                raise nested_not_found_error("Forum agent not found")

            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours)
            count_stmt = select(func.count(ForumAgentResponse.id)).where(
                and_(
                    ForumAgentResponse.agent_id == parsed_agent_id,
                    ForumAgentResponse.responded == True,  # noqa: E712 — SQL boolean
                    ForumAgentResponse.created_at >= cutoff_time,
                )
            )
            result = await db_session.execute(count_stmt)
            count = result.scalar()

            return {
                "count": count or 0,
                "hours": hours,
                "cutoff_time": cutoff_time.isoformat(),
            }
        except BotApiException:
            raise
        except Exception as error:
            raise plain_error(500, f"Failed to get agent response count: {error}")


class ForumNotificationTopicResponse(BaseModel):
    """Serialized forum notification topic (parity with the FastAPI schema)."""

    model_config = ConfigDict(from_attributes=True)

    id: str | UUID
    guild_id: str
    forum_channel_id: str
    topic_name: str
    topic_description: str | None
    created_at: datetime
    updated_at: datetime


class ForumUserSubscriptionCreate(BaseModel):
    """Request body for creating/updating a user's forum subscription."""

    user_id: str
    username: str
    forum_channel_id: str
    subscribed_topics: list[str]
    notification_hours: int


class ForumUserSubscriptionResponse(BaseModel):
    """Serialized forum user subscription (parity with the FastAPI schema)."""

    model_config = ConfigDict(from_attributes=True)

    id: str | UUID
    guild_id: str
    user_id: str
    username: str
    forum_channel_id: str
    subscribed_topics: list[str]
    notification_hours: int
    created_at: datetime
    updated_at: datetime


class ForumNotificationController(Controller):
    """Forum notification topics and user subscription management.

    The legacy ``forum_notifications`` router carried no prefix and declared each
    endpoint's full path; reproduced here with a bare ``/api`` controller path.
    """

    path = "/api"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get(
        "/guilds/{guild_id:str}/forum-channels/{forum_channel_id:str}/notification-topics",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def get_notification_topics(
        self,
        db_session: AsyncSession,
        guild_id: str,
        forum_channel_id: str,
    ) -> list[ForumNotificationTopicResponse]:
        """Get all notification topics for a specific forum channel."""
        try:
            topics_stmt = (
                select(ForumNotificationTopic)
                .where(
                    and_(
                        ForumNotificationTopic.guild_id == guild_id,
                        ForumNotificationTopic.forum_channel_id == forum_channel_id,
                    )
                )
                .order_by(ForumNotificationTopic.topic_name)
            )
            result = await db_session.execute(topics_stmt)
            topics = result.scalars().all()
            return [
                ForumNotificationTopicResponse.model_validate(topic) for topic in topics
            ]
        except Exception as error:
            logger.error(
                "Error fetching notification topics for guild %s, forum %s: %s",
                guild_id,
                forum_channel_id,
                error,
            )
            raise plain_error(500, "Failed to fetch notification topics")

    @get(
        "/guilds/{guild_id:str}/forum-channels/{forum_channel_id:str}/user-subscriptions",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def get_forum_user_subscriptions(
        self,
        db_session: AsyncSession,
        guild_id: str,
        forum_channel_id: str,
    ) -> list[ForumUserSubscriptionResponse]:
        """Get all non-expired user subscriptions for a specific forum channel."""
        try:
            subscriptions_stmt = (
                select(ForumUserSubscription)
                .where(
                    and_(
                        ForumUserSubscription.guild_id == guild_id,
                        ForumUserSubscription.forum_channel_id == forum_channel_id,
                    )
                )
                .order_by(ForumUserSubscription.username)
            )
            result = await db_session.execute(subscriptions_stmt)
            subscriptions = result.scalars().all()
            active_subscriptions = [
                subscription
                for subscription in subscriptions
                if not subscription.is_expired
            ]
            return [
                ForumUserSubscriptionResponse.model_validate(subscription)
                for subscription in active_subscriptions
            ]
        except Exception as error:
            logger.error(
                "Error fetching user subscriptions for guild %s, forum %s: %s",
                guild_id,
                forum_channel_id,
                error,
            )
            raise plain_error(500, "Failed to fetch user subscriptions")

    @get(
        "/guilds/{guild_id:str}/users/{user_id:str}/forum-subscriptions/{forum_channel_id:str}",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def get_user_forum_subscription(
        self,
        db_session: AsyncSession,
        guild_id: str,
        user_id: str,
        forum_channel_id: str,
    ) -> ForumUserSubscriptionResponse:
        """Get a specific user's (non-expired) subscription for a forum channel."""
        try:
            subscription_stmt = select(ForumUserSubscription).where(
                and_(
                    ForumUserSubscription.guild_id == guild_id,
                    ForumUserSubscription.user_id == user_id,
                    ForumUserSubscription.forum_channel_id == forum_channel_id,
                )
            )
            result = await db_session.execute(subscription_stmt)
            subscription = result.scalar_one_or_none()
            if not subscription:
                raise plain_error(404, "User subscription not found")
            if subscription.is_expired:
                raise plain_error(404, "User subscription has expired")
            return ForumUserSubscriptionResponse.model_validate(subscription)
        except BotApiException:
            raise
        except Exception as error:
            logger.error("Error fetching user subscription: %s", error)
            raise plain_error(500, "Failed to fetch user subscription")

    @put(
        "/guilds/{guild_id:str}/users/{user_id:str}/forum-subscriptions/{forum_channel_id:str}",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def create_or_update_user_forum_subscription(
        self,
        db_session: AsyncSession,
        guild_id: str,
        user_id: str,
        forum_channel_id: str,
        data: ForumUserSubscriptionCreate,
    ) -> ForumUserSubscriptionResponse:
        """Create or update a user's forum subscription.

        Path ``user_id``/``forum_channel_id`` scope the lookup; the *stored*
        values on a freshly created row come from the request body (byte-for-byte
        with the FastAPI router, which also read them from ``subscription_data``).
        """
        try:
            existing_stmt = select(ForumUserSubscription).where(
                and_(
                    ForumUserSubscription.guild_id == guild_id,
                    ForumUserSubscription.user_id == user_id,
                    ForumUserSubscription.forum_channel_id == forum_channel_id,
                )
            )
            result = await db_session.execute(existing_stmt)
            existing_subscription = result.scalar_one_or_none()

            current_time = datetime.now(timezone.utc)

            if existing_subscription:
                existing_subscription.username = data.username
                existing_subscription.subscribed_topics = data.subscribed_topics
                existing_subscription.notification_hours = data.notification_hours
                existing_subscription.updated_at = current_time

                await db_session.commit()
                await db_session.refresh(existing_subscription)

                return ForumUserSubscriptionResponse.model_validate(existing_subscription)

            new_subscription = ForumUserSubscription(
                id=uuid4(),
                guild_id=guild_id,
                user_id=data.user_id,
                username=data.username,
                forum_channel_id=data.forum_channel_id,
                subscribed_topics=data.subscribed_topics,
                notification_hours=data.notification_hours,
                created_at=current_time,
                updated_at=current_time,
            )
            db_session.add(new_subscription)
            await db_session.commit()
            await db_session.refresh(new_subscription)

            return ForumUserSubscriptionResponse.model_validate(new_subscription)
        except Exception as error:
            await db_session.rollback()
            logger.error("Error creating/updating user subscription: %s", error)
            raise plain_error(500, "Failed to create or update user subscription")
