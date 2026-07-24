"""Tests for the bot-side persistence helpers that POST chat-agent
engagements and turns to the operator dashboard API.

These are tight unit tests over the payload shape — the API client is
mocked so failures and missing clients are exercised cleanly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic_ai.exceptions import ModelHTTPError

from smarter_dev.bot.agents.chat_compaction import CompactionEvent
from smarter_dev.bot.services import chat_conversation_persistence as ccp


def _bot_with_api_client(post: AsyncMock):
    api_client = MagicMock()
    api_client.post = post
    bot = MagicMock()
    bot.d = {"api_client": api_client}
    return bot


@pytest.mark.asyncio
async def test_start_engagement_posts_and_returns_id():
    new_id = uuid4()
    post = AsyncMock(
        return_value=SimpleNamespace(
            status_code=201, json=lambda: {"id": str(new_id)}
        )
    )
    bot = _bot_with_api_client(post)
    out = await ccp.start_engagement(
        bot=bot,
        guild_id=111,
        channel_id=222,
        guild_name="g",
        channel_name="c",
        activation_user_id=333,
        activation_username="alice",
        activation_message_id=444,
    )
    assert out == new_id
    post.assert_awaited_once()
    args, kwargs = post.call_args
    assert args[0] == "/chat-conversations/engagements"
    payload = kwargs["json_data"]
    assert payload["guild_id"] == "111"
    assert payload["channel_id"] == "222"
    assert payload["activation_user_id"] == "333"
    assert payload["activation_username"] == "alice"
    assert payload["activation_message_id"] == "444"


@pytest.mark.asyncio
async def test_start_engagement_returns_none_when_api_unavailable():
    bot = MagicMock()
    bot.d = {}  # no api_client
    out = await ccp.start_engagement(
        bot=bot,
        guild_id=1,
        channel_id=2,
        guild_name=None,
        channel_name=None,
        activation_user_id=3,
        activation_username="x",
        activation_message_id=4,
    )
    assert out is None


@pytest.mark.asyncio
async def test_start_engagement_returns_none_on_http_failure():
    post = AsyncMock(
        return_value=SimpleNamespace(
            status_code=500,
            text="server error",
            json=lambda: {},
        )
    )
    bot = _bot_with_api_client(post)
    out = await ccp.start_engagement(
        bot=bot,
        guild_id=1,
        channel_id=2,
        guild_name=None,
        channel_name=None,
        activation_user_id=3,
        activation_username="x",
        activation_message_id=4,
    )
    assert out is None


@pytest.mark.asyncio
async def test_end_engagement_posts_reason():
    post = AsyncMock(return_value=SimpleNamespace(status_code=200))
    bot = _bot_with_api_client(post)
    eng_id = uuid4()
    await ccp.end_engagement(
        bot=bot, engagement_id=eng_id, deactivation_reason="stop_phrase"
    )
    post.assert_awaited_once()
    args, kwargs = post.call_args
    assert args[0] == f"/chat-conversations/engagements/{eng_id}/end"
    assert kwargs["json_data"] == {"deactivation_reason": "stop_phrase"}


@pytest.mark.asyncio
async def test_persist_error_posts_full_diagnostics_and_returns_admin_url():
    error_id = uuid4()
    admin_url = f"https://smarter.dev/admin/chat-errors/{error_id}"
    post = AsyncMock(
        return_value=SimpleNamespace(
            status_code=201,
            json=lambda: {"id": str(error_id), "admin_url": admin_url},
        )
    )
    bot = _bot_with_api_client(post)
    engagement_id = uuid4()
    try:
        raise ModelHTTPError(
            status_code=503,
            model_name="kimi-k2.6",
            body={"error": {"message": "upstream overloaded"}},
        )
    except ModelHTTPError as error:
        result = await ccp.persist_error(
            bot=bot,
            error=error,
            engagement_id=engagement_id,
            request_id="abcd1234",
            guild_id=111,
            channel_id=222,
            model_name="kimi-k2.6",
            reasoning_level="medium",
            error_context={"first_activation": True},
        )

    assert result == admin_url
    post.assert_awaited_once()
    args, kwargs = post.call_args
    assert args[0] == "/chat-conversations/errors"
    payload = kwargs["json_data"]
    assert payload["engagement_id"] == str(engagement_id)
    assert payload["request_id"] == "abcd1234"
    assert payload["guild_id"] == "111"
    assert payload["channel_id"] == "222"
    assert payload["model_name"] == "kimi-k2.6"
    assert payload["reasoning_level"] == "medium"
    assert payload["error_type"].endswith(".ModelHTTPError")
    assert payload["provider_status_code"] == 503
    assert "upstream overloaded" in payload["provider_body"]
    assert "ModelHTTPError" in payload["traceback"]
    assert payload["error_context"] == {"first_activation": True}


@pytest.mark.asyncio
async def test_persist_error_returns_none_when_api_unavailable():
    bot = MagicMock()
    bot.d = {}
    result = await ccp.persist_error(
        bot=bot,
        error=RuntimeError("boom"),
        engagement_id=None,
        request_id="abcd1234",
        guild_id=111,
        channel_id=222,
        model_name=None,
        reasoning_level=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_persist_turn_serialises_compaction_events_and_delta():
    post = AsyncMock(return_value=SimpleNamespace(status_code=201))
    bot = _bot_with_api_client(post)
    eng_id = uuid4()
    comp = CompactionEvent(
        event_kind="user_prompt",
        tool_name=None,
        original_content="x" * 12000,
        summary="short summary",
        original_chars=12000,
        summary_chars=13,
        summarizer_tokens_input=42,
        summarizer_tokens_output=7,
        summarizer_model_name="stub-model",
        summarizer_reasoning_level="low",
        summarizer_cache_read_tokens=30,
        summarizer_cache_write_tokens=0,
    )
    await ccp.persist_turn(
        bot=bot,
        engagement_id=eng_id,
        request_id="abcd1234",
        turn_kind="followup",
        output_kind="send_response",
        triggering_messages=[{"message_id": "9", "body": "hi"}],
        agent_output={"kind": "send_response", "topic": "t", "notes": "n"},
        new_model_messages=[],
        duration_ms=1234,
        chat_tokens_input=500,
        chat_tokens_output=100,
        chat_model_name="gemini-3.1-flash-lite-preview",
        chat_reasoning_level="high",
        chat_cache_read_tokens=120,
        chat_cache_write_tokens=0,
        voice_tokens_input=80,
        voice_tokens_output=20,
        voice_model_name="gemini-2.5-flash-preview-tts",
        voice_sent_ok=True,
        voice_send_error=None,
        compaction_events=[comp],
    )
    post.assert_awaited_once()
    args, kwargs = post.call_args
    assert args[0] == "/chat-conversations/turns"
    body = kwargs["json_data"]
    assert body["engagement_id"] == str(eng_id)
    assert body["turn_kind"] == "followup"
    assert body["output_kind"] == "send_response"
    assert body["chat_tokens_input"] == 500
    assert body["chat_reasoning_level"] == "high"
    assert body["chat_cache_read_tokens"] == 120
    assert body["chat_cache_write_tokens"] == 0
    assert body["voice_tokens_input"] == 80
    assert body["voice_sent_ok"] is True
    assert body["model_messages_delta"] == []
    assert len(body["compaction_events"]) == 1
    posted_event = body["compaction_events"][0]
    assert posted_event["event_kind"] == "user_prompt"
    assert posted_event["original_chars"] == 12000
    assert posted_event["summarizer_tokens_input"] == 42
    assert posted_event["summarizer_reasoning_level"] == "low"
    assert posted_event["summarizer_cache_read_tokens"] == 30
    assert posted_event["summarizer_cache_write_tokens"] == 0


@pytest.mark.asyncio
async def test_persist_turn_defaults_reasoning_level_to_none():
    post = AsyncMock(return_value=SimpleNamespace(status_code=201))
    bot = _bot_with_api_client(post)
    await ccp.persist_turn(
        bot=bot,
        engagement_id=uuid4(),
        request_id="abcd1234",
        turn_kind="initial",
        output_kind="no_response",
        triggering_messages=[],
        agent_output={"kind": "no_response", "topic": "x"},
        new_model_messages=[],
        duration_ms=10,
        chat_tokens_input=0,
        chat_tokens_output=0,
        chat_model_name=None,
    )
    post.assert_awaited_once()
    _, kwargs = post.call_args
    # Field is always present so an unset value round-trips as an explicit None.
    assert kwargs["json_data"]["chat_reasoning_level"] is None
    assert kwargs["json_data"]["chat_cache_read_tokens"] is None
    assert kwargs["json_data"]["chat_cache_write_tokens"] is None


@pytest.mark.asyncio
async def test_persist_turn_is_noop_without_api_client():
    bot = MagicMock()
    bot.d = {}
    # Should not raise.
    await ccp.persist_turn(
        bot=bot,
        engagement_id=uuid4(),
        request_id="abcd1234",
        turn_kind="initial",
        output_kind="no_response",
        triggering_messages=[],
        agent_output={"kind": "no_response", "topic": "x"},
        new_model_messages=[],
        duration_ms=10,
        chat_tokens_input=0,
        chat_tokens_output=0,
        chat_model_name=None,
    )
