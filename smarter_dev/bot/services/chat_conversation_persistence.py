"""Best-effort POSTs to the chat-conversations API so the operator dashboard
has data to render.

Failures are swallowed (logged at warning) — persistence MUST NOT block the
user-facing reply path. Engine calls these in a try/except, so even if this
module misbehaves the agent still works.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from pydantic_ai.messages import ModelMessagesTypeAdapter

from smarter_dev.bot.agents.chat_compaction import CompactionEvent

logger = logging.getLogger(__name__)

_ENGAGEMENTS_PATH = "/admin/chat-conversations/engagements"
_END_PATH_TMPL = "/admin/chat-conversations/engagements/{id}/end"
_TURNS_PATH = "/admin/chat-conversations/turns"
# Note: the bot APIClient prefixes paths with the API base URL set in
# settings (api_base_url, e.g. http://web:8000/api in compose). The router
# is mounted at /chat-conversations under the same /api prefix, so we pass
# the path relative to /api. We use "/admin" prefix because the bot API
# client config historically used that mount; double-check by reading the
# router prefix below.

# (Actual router is at /chat-conversations, NOT /admin/chat-conversations.)
_ENGAGEMENTS_PATH = "/chat-conversations/engagements"
_END_PATH_TMPL = "/chat-conversations/engagements/{id}/end"
_TURNS_PATH = "/chat-conversations/turns"


def _get_api_client(bot: Any) -> Any | None:
    """Pull the APIClient out of bot.d if it's there."""
    return getattr(bot, "d", {}).get("api_client")


async def start_engagement(
    *,
    bot: Any,
    guild_id: int,
    channel_id: int,
    guild_name: str | None,
    channel_name: str | None,
    activation_user_id: int,
    activation_username: str,
    activation_message_id: int,
) -> UUID | None:
    """Create the engagement row. Returns its id on success, None on failure."""
    api_client = _get_api_client(bot)
    if api_client is None:
        logger.debug("api_client not available — skipping engagement persist")
        return None
    payload = {
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "guild_name": guild_name,
        "channel_name": channel_name,
        "activation_user_id": str(activation_user_id),
        "activation_username": activation_username,
        "activation_message_id": str(activation_message_id),
    }
    try:
        resp = await api_client.post(_ENGAGEMENTS_PATH, json_data=payload)
        if resp.status_code not in (200, 201):
            logger.warning(
                "chat conversation engagement persist failed: HTTP %s — %s",
                resp.status_code,
                resp.text[:200] if hasattr(resp, "text") else "",
            )
            return None
        body = resp.json()
        return UUID(body["id"])
    except Exception:
        logger.exception("Failed to start chat conversation engagement")
        return None


async def end_engagement(
    *,
    bot: Any,
    engagement_id: UUID,
    deactivation_reason: str,
) -> None:
    """Finalise an engagement. Best-effort."""
    api_client = _get_api_client(bot)
    if api_client is None:
        return
    try:
        resp = await api_client.post(
            _END_PATH_TMPL.format(id=str(engagement_id)),
            json_data={"deactivation_reason": deactivation_reason},
        )
        if resp.status_code not in (200, 201):
            logger.warning(
                "chat conversation engagement end failed: HTTP %s",
                resp.status_code,
            )
    except Exception:
        logger.exception("Failed to end chat conversation engagement")


async def persist_turn(
    *,
    bot: Any,
    engagement_id: UUID,
    request_id: str,
    turn_kind: str,  # initial / followup
    output_kind: str,  # send_response / no_response
    triggering_messages: list[dict],
    agent_output: dict,
    new_model_messages: list,  # list[ModelMessage] from result.new_messages()
    duration_ms: int | None,
    chat_tokens_input: int,
    chat_tokens_output: int,
    chat_model_name: str | None,
    chat_reasoning_level: str | None = None,
    chat_cache_read_tokens: int | None = None,
    chat_cache_write_tokens: int | None = None,
    voice_tokens_input: int = 0,
    voice_tokens_output: int = 0,
    voice_model_name: str | None = None,
    voice_sent_ok: bool | None = None,
    voice_send_error: str | None = None,
    compaction_events: list[CompactionEvent] | None = None,
) -> None:
    """Persist one agent turn + its compaction events. Best-effort."""
    api_client = _get_api_client(bot)
    if api_client is None:
        return

    # Serialise Pydantic AI messages to plain dicts so they round-trip JSON.
    try:
        delta_json = ModelMessagesTypeAdapter.dump_json(new_model_messages)
        delta = json.loads(delta_json)
    except Exception:
        logger.exception("Failed to serialise model_messages_delta; storing None")
        delta = None

    compaction_payload = [
        {
            "event_kind": ev.event_kind,
            "tool_name": ev.tool_name,
            "original_content": ev.original_content,
            "summary": ev.summary,
            "original_chars": ev.original_chars,
            "summary_chars": ev.summary_chars,
            "summarizer_tokens_input": ev.summarizer_tokens_input,
            "summarizer_tokens_output": ev.summarizer_tokens_output,
            "summarizer_model_name": ev.summarizer_model_name,
            "summarizer_reasoning_level": ev.summarizer_reasoning_level,
            "summarizer_cache_read_tokens": ev.summarizer_cache_read_tokens,
            "summarizer_cache_write_tokens": ev.summarizer_cache_write_tokens,
        }
        for ev in (compaction_events or [])
    ]

    payload = {
        "engagement_id": str(engagement_id),
        "request_id": request_id,
        "turn_kind": turn_kind,
        "output_kind": output_kind,
        "triggering_messages": triggering_messages,
        "agent_output": agent_output,
        "model_messages_delta": delta,
        "duration_ms": duration_ms,
        "chat_tokens_input": chat_tokens_input,
        "chat_tokens_output": chat_tokens_output,
        "chat_model_name": chat_model_name,
        "chat_reasoning_level": chat_reasoning_level,
        "chat_cache_read_tokens": chat_cache_read_tokens,
        "chat_cache_write_tokens": chat_cache_write_tokens,
        "voice_tokens_input": voice_tokens_input,
        "voice_tokens_output": voice_tokens_output,
        "voice_model_name": voice_model_name,
        "voice_sent_ok": voice_sent_ok,
        "voice_send_error": voice_send_error,
        "compaction_events": compaction_payload,
    }

    try:
        resp = await api_client.post(_TURNS_PATH, json_data=payload)
        if resp.status_code not in (200, 201):
            logger.warning(
                "chat conversation turn persist failed: HTTP %s — %s",
                resp.status_code,
                resp.text[:200] if hasattr(resp, "text") else "",
            )
    except Exception:
        logger.exception("Failed to persist chat conversation turn")
