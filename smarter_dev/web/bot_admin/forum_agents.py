"""Forum agent management for the Skrift admin panel.

Ports the ``forum_agent_*`` views from the legacy
``smarter_dev.web.admin.views`` onto a Skrift-native Litestar controller under
``/admin/bot``. Guild identity comes from Discord (via
:mod:`smarter_dev.web.discord_admin_client`); the agents, their responses, and
their notification topics live in the ``forum_agents`` /
``forum_agent_responses`` / ``forum_notification_topics`` tables.

Form reading and validation are factored into pure module-level helpers so the
accepted-field contract and the analytics flattening can be unit-tested without
a request or a database. Unlike the legacy ``validate_forum_agent_data``, the
validator here does not mutate its input — it returns a freshly cleaned dict.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol
from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Response, Template as TemplateResponse
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission, auth_guard

from smarter_dev.web.crud import (
    ConflictError,
    ForumAgentOperations,
)
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    GuildNotFoundError,
    get_admin_discord_client,
)
from smarter_dev.web.models import (
    ForumAgent,
    ForumAgentResponse,
    ForumNotificationTopic,
)

logger = logging.getLogger(__name__)

_MAX_NOTIFICATION_TOPICS = 25
_MAX_AGENT_NAME_LENGTH = 100
_MIN_SYSTEM_PROMPT_LENGTH = 10
_MAX_SYSTEM_PROMPT_LENGTH = 10000
_MAX_RESPONSES_PER_HOUR = 100
_CREATED_BY = "admin"


class FormLike(Protocol):
    """The subset of the submitted-form interface the readers rely on.

    Litestar's ``FormMultiDict`` satisfies this; tests can build one directly to
    exercise the multi-value (``getall``) fields the same way the runtime does.
    """

    def get(self, key: str, default: Any = ...) -> Any: ...

    def getall(self, key: str, default: Any = ...) -> list: ...


def read_forum_agent_form(form: FormLike) -> dict:
    """Extract a submitted forum-agent form into a raw field dict.

    Pure function — no I/O. Mirrors the fields the legacy view read: scalar
    text fields are stripped, list fields (``monitored_forums[]``,
    ``notification_topics[]``, ``notification_topic_descriptions[]``) come from
    the multi-value form interface, and the two operation-mode checkboxes are
    kept as their raw ``"on"`` / ``None`` values so validation can interpret
    them. The result is suitable both for validation and for re-rendering the
    form after a validation failure.
    """
    return {
        "name": (form.get("name") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "system_prompt": (form.get("system_prompt") or "").strip(),
        "response_threshold": form.get("response_threshold") or "0.7",
        "max_responses_per_hour": form.get("max_responses_per_hour") or "5",
        "monitored_forums": list(form.getall("monitored_forums[]", [])),
        "is_active": form.get("is_active") == "on",
        "enable_responses": form.get("enable_responses"),
        "enable_user_tagging": form.get("enable_user_tagging"),
        "notification_topics": list(form.getall("notification_topics[]", [])),
        "notification_topic_descriptions": list(
            form.getall("notification_topic_descriptions[]", [])
        ),
    }


def validate_forum_agent_form(data: dict) -> tuple[bool, list[str], dict]:
    """Validate a raw forum-agent form dict, returning cleaned values.

    Pure function — does **not** mutate ``data``. Returns
    ``(is_valid, errors, cleaned)``; ``cleaned`` holds the typed, list-cleaned
    values ready to pass to :class:`ForumAgentOperations` when ``is_valid`` is
    ``True``. Mirrors the legacy ``validate_forum_agent_data`` rules: required
    name/system-prompt with length bounds, a 0.0-1.0 response threshold, a
    0-100 hourly rate limit, at least one operation mode enabled, and — when
    user tagging is on — 1-25 non-empty notification topics with descriptions
    padded/trimmed to match.
    """
    errors: list[str] = []

    name = str(data.get("name", "")).strip()
    if not name:
        errors.append("Agent name is required")
    elif len(name) > _MAX_AGENT_NAME_LENGTH:
        errors.append("Agent name must be 100 characters or less")

    system_prompt = str(data.get("system_prompt", "")).strip()
    if not system_prompt:
        errors.append("System prompt is required")
    elif len(system_prompt) < _MIN_SYSTEM_PROMPT_LENGTH:
        errors.append("System prompt must be at least 10 characters")
    elif len(system_prompt) > _MAX_SYSTEM_PROMPT_LENGTH:
        errors.append("System prompt must be 10,000 characters or less")

    response_threshold = 0.7
    try:
        response_threshold = float(data.get("response_threshold", 0.7))
        if response_threshold < 0.0 or response_threshold > 1.0:
            errors.append("Response threshold must be between 0.0 and 1.0")
    except (ValueError, TypeError):
        errors.append("Response threshold must be a valid number")

    max_responses_per_hour = 5
    try:
        max_responses_per_hour = int(data.get("max_responses_per_hour", 5))
        if max_responses_per_hour < 0:
            errors.append("Rate limit cannot be negative")
        elif max_responses_per_hour > _MAX_RESPONSES_PER_HOUR:
            errors.append("Rate limit cannot exceed 100 responses per hour")
    except (ValueError, TypeError):
        errors.append("Rate limit must be a valid number")

    raw_forums = data.get("monitored_forums", [])
    if not isinstance(raw_forums, list):
        errors.append("Monitored forums must be a list")
        monitored_forums: list[str] = []
    else:
        # Empty list is allowed — it means monitor all forum channels.
        monitored_forums = [
            forum.strip() for forum in raw_forums if forum and forum.strip()
        ]

    enable_responses = data.get("enable_responses") == "on"
    enable_user_tagging = data.get("enable_user_tagging") == "on"
    if not enable_responses and not enable_user_tagging:
        errors.append(
            "At least one mode must be enabled (responses or user tagging)"
        )

    notification_topics, notification_topic_descriptions = _clean_notification_topics(
        data, enable_user_tagging, errors
    )

    cleaned = {
        "name": name,
        "description": str(data.get("description", "")).strip(),
        "system_prompt": system_prompt,
        "response_threshold": response_threshold,
        "max_responses_per_hour": max_responses_per_hour,
        "monitored_forums": monitored_forums,
        "is_active": bool(data.get("is_active", False)),
        "enable_responses": enable_responses,
        "enable_user_tagging": enable_user_tagging,
        "notification_topics": notification_topics,
        "notification_topic_descriptions": notification_topic_descriptions,
    }
    return len(errors) == 0, errors, cleaned


def _clean_notification_topics(
    data: dict, enable_user_tagging: bool, errors: list[str]
) -> tuple[list[str], list[str]]:
    """Clean and validate notification topics, appending any errors.

    Returns ``(topics, descriptions)``; both are empty when user tagging is
    disabled. Descriptions are padded with empty strings and trimmed to match
    the topics length, mirroring the legacy validator.
    """
    if not enable_user_tagging:
        return [], []

    raw_topics = data.get("notification_topics", [])
    if not isinstance(raw_topics, list):
        errors.append("Notification topics must be a list")
        return [], []

    topics = [topic.strip() for topic in raw_topics if topic and topic.strip()]
    if len(topics) > _MAX_NOTIFICATION_TOPICS:
        errors.append("Maximum 25 notification topics allowed")
    elif len(topics) == 0:
        errors.append(
            "At least one notification topic is required when user tagging is enabled"
        )
    for topic in topics:
        if len(topic) > _MAX_AGENT_NAME_LENGTH:
            errors.append(f"Topic name '{topic}' is too long (max 100 characters)")

    raw_descriptions = data.get("notification_topic_descriptions", [])
    if isinstance(raw_descriptions, list):
        descriptions = [desc.strip() if desc else "" for desc in raw_descriptions]
    else:
        descriptions = []
    descriptions = descriptions[: len(topics)]
    descriptions += [""] * (len(topics) - len(descriptions))
    return topics, descriptions


def flatten_agent_analytics(analytics: dict) -> dict:
    """Flatten the CRUD analytics payload into the shape the template expects.

    Pure function. Mirrors the legacy view's remapping: statistics are lifted to
    the top level under the template's field names and a missing average
    response time collapses to the string ``"N/A"``.
    """
    stats = analytics["statistics"]
    average_response_time = stats["average_response_time_ms"]
    return {
        "total_evaluations": stats["total_evaluations"],
        "total_responses": stats["total_responses"],
        "response_rate": stats["response_rate"],
        "total_tokens": stats["total_tokens_used"],
        "avg_confidence": stats["average_confidence"],
        "average_response_time_ms": (
            average_response_time if average_response_time is not None else "N/A"
        ),
        "agent": analytics["agent"],
    }


def format_response_details(
    response: ForumAgentResponse, agent: ForumAgent
) -> dict:
    """Shape a forum response + its agent into the details JSON payload.

    Pure function. Mirrors the legacy ``get_forum_response_details`` output so
    the analytics page's response-inspector modal keeps working unchanged.
    """
    return {
        "id": str(response.id),
        "agent_name": agent.name,
        "post_title": response.post_title or "Untitled",
        "post_content": response.post_content or "",
        "author_display_name": response.author_display_name or "Unknown",
        "post_tags": response.post_tags or [],
        "confidence_score": response.confidence_score,
        "decision_reasoning": response.decision_reason or "",
        "responded": response.responded,
        "response_content": response.response_content or "",
        "tokens_used": response.tokens_used or 0,
        "response_time_ms": response.response_time_ms,
        "created_at": response.created_at.isoformat() if response.created_at else "",
        "responded_at": (
            response.responded_at.isoformat() if response.responded_at else ""
        ),
    }


async def load_agent_notification_topics(
    db_session: AsyncSession, agent: ForumAgent
) -> list[dict]:
    """Load an agent's notification topics for the edit form.

    Mirrors the legacy edit view: only meaningful when the agent has user
    tagging enabled. Topics are read for the agent's monitored forums (or ``*``
    when it monitors all forums); the first forum that has topics wins to avoid
    duplicates.
    """
    if not agent.enable_user_tagging:
        return []

    forums_to_check = agent.monitored_forums or ["*"]
    topics: list[dict] = []
    for forum_id in forums_to_check:
        result = await db_session.execute(
            select(ForumNotificationTopic)
            .where(
                and_(
                    ForumNotificationTopic.guild_id == agent.guild_id,
                    ForumNotificationTopic.forum_channel_id == forum_id,
                )
            )
            .order_by(ForumNotificationTopic.topic_name)
        )
        forum_topics = list(result.scalars().all())
        for topic in forum_topics:
            topics.append(
                {
                    "name": topic.topic_name,
                    "description": topic.topic_description or "",
                }
            )
        if forum_topics:
            break
    return topics


async def render_create_form(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    guild_id: str,
    form_data: dict | None,
    errors: list[str],
    status_code: int,
) -> TemplateResponse:
    """Render the create form with (optionally) submitted values and errors."""
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/forum_agents/create.html",
        context={
            "guild": guild,
            "errors": errors,
            "form_data": form_data,
            "active_page": "forum_agents",
            "guild_id": guild_id,
            **ctx,
        },
        status_code=status_code,
    )


async def render_edit_form(
    request: Request,
    db_session: AsyncSession,
    guild: object,
    guild_id: str,
    agent: ForumAgent,
    form_data: dict | None,
    errors: list[str],
    status_code: int,
) -> TemplateResponse:
    """Render the edit form with (optionally) submitted values and errors."""
    ctx = await get_admin_context(request, db_session)
    notification_topics = await load_agent_notification_topics(db_session, agent)
    return TemplateResponse(
        "admin/bot/forum_agents/edit.html",
        context={
            "guild": guild,
            "agent": agent,
            "notification_topics": notification_topics,
            "errors": errors,
            "form_data": form_data,
            "active_page": "forum_agents",
            "guild_id": guild_id,
            **ctx,
        },
        status_code=status_code,
    )


async def render_agent_not_found(
    request: Request, db_session: AsyncSession, guild_id: str
) -> TemplateResponse:
    """Render the shared error page for a missing forum agent."""
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/guilds/error.html",
        context={
            "error": "Forum agent not found.",
            "error_code": 404,
            "active_page": "forum_agents",
            "guild_id": guild_id,
            **ctx,
        },
        status_code=404,
    )


async def render_bulk_error(
    request: Request, db_session: AsyncSession, guild_id: str
) -> TemplateResponse:
    """Render the shared error page for an invalid bulk request."""
    ctx = await get_admin_context(request, db_session)
    return TemplateResponse(
        "admin/bot/guilds/error.html",
        context={
            "error": "Invalid bulk operation request.",
            "error_code": 400,
            "active_page": "forum_agents",
            "guild_id": guild_id,
            **ctx,
        },
        status_code=400,
    )


async def fetch_guild_or_error(
    request: Request,
    db_session: AsyncSession,
    guild_id: str,
) -> tuple[object | None, TemplateResponse | None]:
    """Fetch guild details, returning an error template on failure.

    Returns ``(guild, None)`` on success or ``(None, response)`` when the guild
    is missing (404) or Discord is unavailable (503). Mirrors the error handling
    used by the sibling bytes-config and squads controllers.
    """
    client = get_admin_discord_client()
    try:
        guild = await client.get_guild(guild_id)
    except GuildNotFoundError:
        ctx = await get_admin_context(request, db_session)
        return None, TemplateResponse(
            "admin/bot/guilds/error.html",
            context={
                "error": f"Guild {guild_id} not found or bot is not a member.",
                "error_code": 404,
                "active_page": "forum_agents",
                "guild_id": guild_id,
                **ctx,
            },
            status_code=404,
        )
    except DiscordAdminError as exc:
        ctx = await get_admin_context(request, db_session)
        return None, TemplateResponse(
            "admin/bot/guilds/error.html",
            context={
                "error": f"Discord API error: {exc}",
                "error_code": 503,
                "active_page": "forum_agents",
                "guild_id": guild_id,
                **ctx,
            },
            status_code=503,
        )
    return guild, None


class ForumAgentsAdminController(Controller):
    """Forum agent CRUD, toggle, analytics, and bulk ops under ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    # -- List -----------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/forum-agents",
        guards=[auth_guard, Permission("administrator")],
    )
    async def forum_agents_list(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """List every forum agent configured for a guild."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        ctx = await get_admin_context(request, db_session)
        agents = await ForumAgentOperations(db_session).list_agents(guild_id)

        return TemplateResponse(
            "admin/bot/forum_agents/list.html",
            context={
                "guild": guild,
                "agents": agents,
                "active_page": "forum_agents",
                "guild_id": guild_id,
                **ctx,
            },
        )

    # -- Create ---------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/forum-agents/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def forum_agent_create_form(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Render the blank create-agent form."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/forum_agents/create.html",
            context={
                "guild": guild,
                "errors": [],
                "form_data": None,
                "active_page": "forum_agents",
                "guild_id": guild_id,
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/forum-agents/create",
        guards=[auth_guard, Permission("administrator")],
    )
    async def forum_agent_create(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Validate and create a forum agent, then redirect to the list."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        form = await request.form()
        data = read_forum_agent_form(form)
        is_valid, errors, cleaned = validate_forum_agent_form(data)

        if not is_valid:
            return await render_create_form(
                request, db_session, guild, guild_id, data, errors, status_code=400
            )

        try:
            await ForumAgentOperations(db_session).create_agent(
                guild_id=guild_id, created_by=_CREATED_BY, **_agent_kwargs(cleaned)
            )
        except ConflictError as exc:
            return await render_create_form(
                request, db_session, guild, guild_id, data, [str(exc)],
                status_code=400,
            )

        return Redirect(path=f"/admin/bot/guilds/{guild_id}/forum-agents")

    # -- Edit -----------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/forum-agents/{agent_id:uuid}/edit",
        guards=[auth_guard, Permission("administrator")],
    )
    async def forum_agent_edit_form(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        agent_id: UUID,
    ) -> Response:
        """Render the edit form pre-populated from an existing agent."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        ops = ForumAgentOperations(db_session)
        agent = await ops.get_agent(agent_id, guild_id)
        if agent is None:
            return await render_agent_not_found(request, db_session, guild_id)

        ctx = await get_admin_context(request, db_session)
        notification_topics = await load_agent_notification_topics(db_session, agent)
        return TemplateResponse(
            "admin/bot/forum_agents/edit.html",
            context={
                "guild": guild,
                "agent": agent,
                "notification_topics": notification_topics,
                "errors": [],
                "form_data": None,
                "active_page": "forum_agents",
                "guild_id": guild_id,
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/forum-agents/{agent_id:uuid}/edit",
        guards=[auth_guard, Permission("administrator")],
    )
    async def forum_agent_edit(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        agent_id: UUID,
    ) -> Response:
        """Validate and apply edits to a forum agent, then redirect to the list."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        ops = ForumAgentOperations(db_session)
        agent = await ops.get_agent(agent_id, guild_id)
        if agent is None:
            return await render_agent_not_found(request, db_session, guild_id)

        form = await request.form()
        data = read_forum_agent_form(form)
        is_valid, errors, cleaned = validate_forum_agent_form(data)

        if not is_valid:
            return await render_edit_form(
                request, db_session, guild, guild_id, agent, data, errors,
                status_code=400,
            )

        try:
            await ops.update_agent(
                agent_id=agent_id, guild_id=guild_id, **_agent_kwargs(cleaned)
            )
        except ConflictError as exc:
            return await render_edit_form(
                request, db_session, guild, guild_id, agent, data, [str(exc)],
                status_code=400,
            )

        return Redirect(path=f"/admin/bot/guilds/{guild_id}/forum-agents")

    # -- Delete / toggle ------------------------------------------------------

    @post(
        "/guilds/{guild_id:str}/forum-agents/{agent_id:uuid}/delete",
        guards=[auth_guard, Permission("administrator")],
    )
    async def forum_agent_delete(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        agent_id: UUID,
    ) -> Response:
        """Delete a forum agent, then redirect to the list."""
        deleted = await ForumAgentOperations(db_session).delete_agent(
            agent_id, guild_id
        )
        if not deleted:
            return await render_agent_not_found(request, db_session, guild_id)
        return Redirect(path=f"/admin/bot/guilds/{guild_id}/forum-agents")

    @post(
        "/guilds/{guild_id:str}/forum-agents/{agent_id:uuid}/toggle",
        guards=[auth_guard, Permission("administrator")],
    )
    async def forum_agent_toggle(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        agent_id: UUID,
    ) -> Response:
        """Flip a forum agent's active status, then redirect to the list."""
        agent = await ForumAgentOperations(db_session).toggle_agent(
            agent_id, guild_id
        )
        if agent is None:
            return await render_agent_not_found(request, db_session, guild_id)
        return Redirect(path=f"/admin/bot/guilds/{guild_id}/forum-agents")

    # -- Bulk -----------------------------------------------------------------

    @post(
        "/guilds/{guild_id:str}/forum-agents/bulk",
        guards=[auth_guard, Permission("administrator")],
    )
    async def forum_agents_bulk(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Response:
        """Enable/disable/delete a set of agents in one operation."""
        form = await request.form()
        action = form.get("action", "")
        raw_ids = form.getall("agent_ids", [])

        if not action or not raw_ids:
            return await render_bulk_error(request, db_session, guild_id)

        try:
            agent_ids = [UUID(agent_id) for agent_id in raw_ids]
        except (ValueError, TypeError):
            return await render_bulk_error(request, db_session, guild_id)

        await ForumAgentOperations(db_session).bulk_update_agents(
            agent_ids=agent_ids, guild_id=guild_id, action=action
        )
        return Redirect(path=f"/admin/bot/guilds/{guild_id}/forum-agents")

    # -- Analytics ------------------------------------------------------------

    @get(
        "/guilds/{guild_id:str}/forum-agents/{agent_id:uuid}/analytics",
        guards=[auth_guard, Permission("administrator")],
    )
    async def forum_agent_analytics(
        self,
        request: Request,
        db_session: AsyncSession,
        guild_id: str,
        agent_id: UUID,
    ) -> Response:
        """Render aggregate response analytics for one forum agent."""
        guild, error = await fetch_guild_or_error(request, db_session, guild_id)
        if error is not None:
            return error

        analytics = await ForumAgentOperations(db_session).get_agent_analytics(
            agent_id, guild_id
        )
        if not analytics or "agent" not in analytics:
            return await render_agent_not_found(request, db_session, guild_id)

        ctx = await get_admin_context(request, db_session)
        return TemplateResponse(
            "admin/bot/forum_agents/analytics.html",
            context={
                "guild": guild,
                "agent": analytics["agent"],
                "analytics": flatten_agent_analytics(analytics),
                "recent_responses": analytics.get("recent_responses", []),
                "active_page": "forum_agents",
                "guild_id": guild_id,
                **ctx,
            },
        )

    # -- Response details JSON ------------------------------------------------

    @get(
        "/forum-responses/{response_id:uuid}/details",
        guards=[auth_guard, Permission("administrator")],
    )
    async def forum_response_details(
        self, db_session: AsyncSession, response_id: UUID
    ) -> Response:
        """Return one forum response's full details as JSON for the modal."""
        result = await db_session.execute(
            select(ForumAgentResponse, ForumAgent).join(
                ForumAgent, ForumAgentResponse.agent_id == ForumAgent.id
            ).where(ForumAgentResponse.id == response_id)
        )
        row = result.first()
        if row is None:
            return Response(
                content={"error": "Response not found"},
                media_type="application/json",
                status_code=404,
            )

        response, agent = row
        return Response(
            content=format_response_details(response, agent),
            media_type="application/json",
            status_code=200,
        )


def _agent_kwargs(cleaned: dict) -> dict:
    """Map a validated form dict to :class:`ForumAgentOperations` keyword args.

    Pure function. The ``description`` is stored as the (possibly empty) string
    the model's ``NOT NULL`` column expects rather than collapsing blanks to
    ``None``.
    """
    return {
        "name": cleaned["name"],
        "description": cleaned["description"],
        "system_prompt": cleaned["system_prompt"],
        "monitored_forums": cleaned["monitored_forums"],
        "response_threshold": cleaned["response_threshold"],
        "max_responses_per_hour": cleaned["max_responses_per_hour"],
        "is_active": cleaned["is_active"],
        "enable_user_tagging": cleaned["enable_user_tagging"],
        "enable_responses": cleaned["enable_responses"],
        "notification_topics": cleaned["notification_topics"],
        "notification_topic_descriptions": cleaned["notification_topic_descriptions"],
    }
