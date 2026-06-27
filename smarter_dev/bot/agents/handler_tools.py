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

# The chatbot's trigger_type vocabulary mapped to canonical handler types.
_TRIGGER_ALIASES = {
    "new message": "message",
    "message": "message",
    "reaction add": "reaction",
    "reaction": "reaction",
    "schedule": "schedule",
    "timer": "timer",
}
_EVENT_TRIGGERS = ("message", "reaction")


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
    """Create a persistent handler from a plain-language description.

    Describe the behavior clearly and completely — a separate system writes and
    reviews the actual script; you do NOT write code. Trigger types: "new message",
    "reaction add" (event); "schedule", "timer" (time). For time triggers put the
    timing in ``settings`` — schedule: {"interval_seconds": N} or {"daily_time":
    "HH:MM"} (UTC); timer: {"delay_seconds": N} or {"fire_at": "<ISO-8601 UTC>"}.

    For message/reaction triggers there is one handler per channel; registering
    again merges with or replaces it. Returns a success summary or an error to
    relay plainly.
    """
    canonical = _canonical_trigger(trigger_type)
    if canonical is None:
        return f"error: unknown trigger type {trigger_type!r}"
    settings = settings or {}
    channel = str(channel_id or ctx.deps.channel_id)

    api = _api_client(ctx)

    existing_script: str | None = None
    if canonical in _EVENT_TRIGGERS:
        try:
            resp = await api.get("/handlers", params={"channel_id": channel})
            if resp.status_code < 400:
                for row in resp.json():
                    if row["trigger_type"] == canonical:
                        existing_script = (await _get_script(api, row["handler_id"])) or None
                        break
        except Exception:  # noqa: BLE001
            logger.debug("could not load existing handler", exc_info=True)

    async def _status(text: str) -> None:
        await _post_status(ctx, text)

    await _status("Setting up a new handler…")
    result = await run_creation_pipeline(
        description=description,
        trigger_type=canonical,
        settings=settings,
        existing_script=existing_script,
        emoji_lister=lambda: _list_guild_emojis(ctx),
        progress=_status,
    )
    if not result.ok:
        return f"error: {result.error}"

    if canonical in ("schedule", "timer"):
        try:
            validate_interval(settings, uses_agent=script_uses_agent(result.script))
        except ScheduleError as exc:
            return f"error: {exc}"

    payload = {
        "guild_id": str(ctx.deps.guild_id),
        "channel_id": channel,
        "trigger_type": canonical,
        "settings": settings,
        "description": description,
        "script": result.script,
        "created_by": "chatbot",
    }
    resp = await api.post("/handlers", json_data=payload)
    if resp.status_code >= 400:
        return f"error: {_error_detail(resp)}"
    data = resp.json()
    return (
        f"Created handler {data['handler_id']} ({canonical}): {data['description']}"
    )


async def list_handlers(ctx: RunContext[ChatDeps], channel_id: str | None = None) -> str:
    """List the handlers active in a channel, with their ids and triggers."""
    channel = str(channel_id or ctx.deps.channel_id)
    api = _api_client(ctx)
    resp = await api.get("/handlers", params={"channel_id": channel})
    if resp.status_code >= 400:
        return f"error: {_error_detail(resp)}"
    rows = resp.json()
    if not rows:
        return "No handlers active in this channel."
    return "\n".join(
        f"- {r['handler_id']} [{r['trigger_type']}] {r['description']}" for r in rows
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


async def _get_script(api: Any, handler_id: str) -> str | None:
    """Fetch a handler's script body (for author merge), or None if unavailable."""
    try:
        resp = await api.get(f"/handlers/{handler_id}")
        if resp.status_code < 400:
            return resp.json().get("script")
    except Exception:  # noqa: BLE001
        logger.debug("could not fetch handler script", exc_info=True)
    return None


def _error_detail(resp: Any) -> str:
    try:
        body = resp.json()
        return str(body.get("detail", body))
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status_code}"


def handler_tool_functions() -> list:
    """Tool callables to register with the chat agent for handler management."""
    return [register_handler, list_handlers, delete_handler]
