"""Chatbot tools for managing persistent handlers.

These three tools are added to the chat agent. The chatbot describes the desired
behavior in plain language; it does NOT write code. ``register_handler`` runs the
creation pipeline (author + judge) inline here in the bot, then persists the
approved script through the web API. ``list_handlers`` / ``delete_handler``
manage handlers by their id.

The bot has no DB access, so all persistence goes through the web API; the
creation pipeline (author/judge) lives in the bot because pydantic-ai is already
loaded here and this is the live user-interaction context.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_ai import RunContext

from smarter_dev.bot.agents.chat_tools import ChatDeps, _post_status
from smarter_dev.bot.agents.handler_authoring import (
    run_creation_pipeline,
    script_uses_agent,
)
from smarter_dev.shared.config import get_settings
from smarter_dev.web.handler_schedule import ScheduleError, validate_interval

logger = logging.getLogger(__name__)

# The chatbot's trigger_type vocabulary mapped to canonical handler types. The
# five member/thread event triggers are ADMIN-ONLY (see threads-and-member-events
# spec) and deliberately absent here — a member cannot register them via the chat
# tool; _admin_only_trigger routes them to a redirect instead of a bad mapping.
_TRIGGER_ALIASES = {
    "new message": "message",
    "message": "message",
    "reaction add": "reaction",
    "reaction": "reaction",
    "schedule": "schedule",
    "timer": "timer",
}

# Names (canonical and natural-language) for the admin-only member/thread
# triggers. A member asking for one is pointed at /adminhandler, not told the
# trigger is unknown — it exists, it's just not theirs to register.
_ADMIN_ONLY_TRIGGERS = frozenset(
    {
        "member_join", "member join", "member joins",
        "member_leave", "member leave", "member leaves",
        "member_rules_accepted", "member rules accepted", "rules accepted",
        "member_role_change", "member role change", "role change",
        "thread_create", "thread create", "new thread", "forum post",
    }
)

_ADMIN_ONLY_REDIRECT = (
    "that trigger requires an admin handler — ask a server admin to set it up "
    "via /adminhandler"
)


def _admin_only_trigger(trigger_type: str) -> bool:
    """Whether this is one of the five admin-only member/thread triggers."""
    return trigger_type.strip().lower() in _ADMIN_ONLY_TRIGGERS


def _api_client(ctx: RunContext[ChatDeps]):
    """Return the per-run APIClient, building one from settings if absent."""
    if ctx.deps.api_client is not None:
        return ctx.deps.api_client
    from smarter_dev.bot.services.api_client import APIClient

    settings = get_settings()
    return APIClient(base_url=settings.api_base_url, api_key=settings.bot_api_key)


def _canonical_trigger(trigger_type: str) -> str | None:
    return _TRIGGER_ALIASES.get(trigger_type.strip().lower())


async def _list_guild_emojis(ctx: RunContext[ChatDeps]) -> list[dict]:
    try:
        emojis = await ctx.deps.bot.rest.fetch_guild_emojis(ctx.deps.guild_id)
        return [{"name": e.name, "id": str(e.id)} for e in emojis]
    except Exception:  # noqa: BLE001 — emojis are advisory for the author
        logger.debug("could not fetch guild emojis", exc_info=True)
        return []


async def register_handler(
    ctx: RunContext[ChatDeps],
    description: str,
    trigger_type: str,
    settings: dict | None = None,
    channel_id: str | None = None,
) -> str:
    """Create or update a persistent handler from a plain-language description.

    Describe the behavior clearly and completely — a separate authoring
    system (which sees the channel's existing handlers) decides whether to
    edit or create one; you never write code or pick which handler to
    touch. Trigger types: "new message", "reaction add" (event);
    "schedule", "timer" (time). Member/thread events (someone joins,
    leaves, a role changes, a thread is created) are admin-only — they
    need /adminhandler, not this tool. Put timing in ``settings`` — schedule:
    {"interval_seconds": N} or {"daily_time": "HH:MM"} (UTC); timer:
    {"delay_seconds": N} or {"fire_at": "<ISO-8601 UTC>"}. Returns a
    success summary naming the handler, or an error to relay plainly.
    """
    if _admin_only_trigger(trigger_type):
        return f"error: {_ADMIN_ONLY_REDIRECT}"
    canonical = _canonical_trigger(trigger_type)
    if canonical is None:
        return f"error: unknown trigger type {trigger_type!r}"
    settings = settings or {}
    channel = str(channel_id or ctx.deps.channel_id)

    api = _api_client(ctx)
    existing_handlers = await _channel_handlers_with_scripts(api, channel)

    async def _status(text: str) -> None:
        await _post_status(ctx, text)

    await _status("Working on the handler…")
    result = await run_creation_pipeline(
        request=description,
        trigger_type=canonical,
        settings=settings,
        existing_handlers=existing_handlers,
        emoji_lister=lambda: _list_guild_emojis(ctx),
        progress=_status,
    )
    if not result.ok:
        return f"error: {result.error}"

    if result.trigger_type in ("schedule", "timer"):
        try:
            validate_interval(
                result.settings or {}, uses_agent=script_uses_agent(result.script)
            )
        except ScheduleError as exc:
            return f"error: {exc}"

    if result.action == "edit":
        resp = await api.put(
            f"/handlers/{result.target_handler_id}",
            json_data={
                "description": result.description,
                "script": result.script,
                "settings": result.settings or {},
            },
        )
        if resp.status_code >= 400:
            return f"error: {_error_detail(resp)}"
        data = resp.json()
        return f"Updated handler '{data['name']}': {data['description']}"

    resp = await api.post(
        "/handlers",
        json_data={
            "guild_id": str(ctx.deps.guild_id),
            "channel_id": channel,
            "name": result.name,
            "trigger_type": result.trigger_type,
            "settings": result.settings or {},
            "description": result.description,
            "script": result.script,
            "created_by": "chatbot",
        },
    )
    if resp.status_code >= 400:
        return f"error: {_error_detail(resp)}"
    data = resp.json()
    return (
        f"Created handler '{data['name']}' ({result.trigger_type}): "
        f"{data['description']}"
    )


async def list_handlers(ctx: RunContext[ChatDeps], channel_id: str | None = None) -> str:
    """List the handlers active in a channel, with their names, ids and triggers."""
    channel = str(channel_id or ctx.deps.channel_id)
    api = _api_client(ctx)
    resp = await api.get("/handlers", params={"channel_id": channel})
    if resp.status_code >= 400:
        return f"error: {_error_detail(resp)}"
    rows = resp.json()
    if not rows:
        return "No handlers active in this channel."
    return "\n".join(
        f"- {r['name']} ({r['handler_id']}) [{r['trigger_type']}] {r['description']}"
        for r in rows
    )


async def delete_handler(ctx: RunContext[ChatDeps], handler_id: str) -> str:
    """Remove any handler by its id (from list_handlers)."""
    api = _api_client(ctx)
    resp = await api.delete(f"/handlers/{handler_id}")
    if resp.status_code == 404:
        return f"No handler with id {handler_id}."
    if resp.status_code >= 400:
        return f"error: {_error_detail(resp)}"
    return f"Deleted handler {handler_id}."


async def _channel_handlers_with_scripts(api: Any, channel_id: str) -> list[dict]:
    """The channel's handlers, scripts included, for the author's edit-vs-create
    decision. Best-effort: an API failure means the author sees an empty list
    (it will create rather than edit)."""
    try:
        resp = await api.get(
            "/handlers", params={"channel_id": channel_id, "include_scripts": "true"}
        )
        if resp.status_code < 400:
            return list(resp.json())
    except Exception:  # noqa: BLE001
        logger.debug("could not load existing handlers", exc_info=True)
    return []


def _error_detail(resp: Any) -> str:
    try:
        body = resp.json()
        return str(body.get("detail", body))
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status_code}"


def handler_tool_functions() -> list:
    """Tool callables to register with the chat agent for handler management."""
    return [register_handler, list_handlers, delete_handler]
