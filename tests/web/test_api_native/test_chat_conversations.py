"""Parity tests for the native (Litestar) chat-conversation API (unit U8).

Assert the wire contract of the ported ``routers/chat_conversations.py``
directly: the engagement create (201), engagement end (200 / 404 / 422 on a
malformed id), turn create (201 with the cost breakdown, engagement aggregate
bumps, compaction-event + blog-candidate capture), and the usage-leaderboard
read (200 with per-channel token totals plus the ge/le query validation). Paths
carry the final ``/api`` prefix and mirror exactly what the bot sends from
``smarter_dev/bot/services/chat_conversation_persistence.py`` and
``smarter_dev/bot/plugins/bot_usage.py``.

These run against a real in-memory SQLite session injected into a Litestar app
via ``httpx.ASGITransport`` because the handlers persist ORM rows and re-read
aggregates rather than calling a mockable crud class. Auth guards (both the base
and admin lists) are cleared for the app build — auth parity is covered
separately by ``test_auth.py``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport
from httpx import AsyncClient
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import SystemPromptPart
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool

from smarter_dev.bot.agents.chat_models import Message as ChatMessage
from smarter_dev.bot.agents.chat_models import MessageAttachment
from smarter_dev.shared.database import Base
from smarter_dev.shared.message_content import MESSAGE_CONTENT_PLACEHOLDER
from smarter_dev.web.api_native import chat_conversations as chat_module
from smarter_dev.web.api_native.chat_conversations import ChatConversationController
from smarter_dev.web.models import CandidateBlogTopic
from smarter_dev.web.models import ChatAgentCompactionEvent
from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentError
from smarter_dev.web.models import ChatAgentTurn
from smarter_dev.web.models import UsageCostRow
from tests.web.admin_template_rendering import render_admin_template

_GUILD = "123456789012345678"
_CHANNEL = "555000111222333444"
_TURN_COST_FIELDS = ("chat_cost_usd", "voice_cost_usd", "summarizer_cost_usd_total")


@pytest.fixture
async def session() -> AsyncIterator:
    """Real in-memory SQLite session with every model table created."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as opened_session:
        yield opened_session
    await engine.dispose()


@pytest.fixture
async def client(session) -> AsyncIterator[AsyncClient]:
    """Litestar app serving the chat controller with all guards cleared.

    The write handlers use ``BOT_API_ADMIN_GUARDS`` and the read handler uses
    ``BOT_API_GUARDS``; both lists are shared by reference and cleared here so
    the app builds guard-free for these tests only.
    """
    original_base = list(chat_module.BOT_API_GUARDS)
    original_admin = list(chat_module.BOT_API_ADMIN_GUARDS)
    chat_module.BOT_API_GUARDS.clear()
    chat_module.BOT_API_ADMIN_GUARDS.clear()
    try:
        app = Litestar(
            route_handlers=[ChatConversationController],
            plugins=[PydanticPlugin()],
            dependencies={"db_session": Provide(lambda: session, sync_to_thread=False)},
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as http_client:
            yield http_client
    finally:
        chat_module.BOT_API_GUARDS[:] = original_base
        chat_module.BOT_API_ADMIN_GUARDS[:] = original_admin


async def _seed_engagement(session, **overrides) -> ChatAgentEngagement:
    fields = {
        "guild_id": _GUILD,
        "channel_id": _CHANNEL,
        "channel_name": "general",
        "activation_user_id": "111",
        "activation_username": "alice",
        "activation_message_id": "999",
    }
    fields.update(overrides)
    engagement = ChatAgentEngagement(**fields)
    session.add(engagement)
    await session.commit()
    await session.refresh(engagement)
    return engagement


async def _stored_turn(session) -> ChatAgentTurn:
    """The single turn row the request under test wrote, compactions loaded."""
    turns = (
        (
            await session.execute(
                select(ChatAgentTurn).options(
                    selectinload(ChatAgentTurn.compaction_events)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(turns) == 1
    return turns[0]


async def _stored_usage_cost_row(session, operation_type: str) -> UsageCostRow:
    """The single metering row filed for ``operation_type``."""
    rows = (
        (
            await session.execute(
                select(UsageCostRow).where(
                    UsageCostRow.operation_type == operation_type
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    return rows[0]


async def _stored_compaction_events(session) -> list[ChatAgentCompactionEvent]:
    """Every compaction-event row written, oldest query order."""
    return list(
        (await session.execute(select(ChatAgentCompactionEvent))).scalars().all()
    )


def _serialised_triggering_message(**overrides) -> dict:
    """A triggering message exactly as ``chat_engine`` serialises it."""
    message = ChatMessage(
        message_id="444",
        author_id="333",
        body="what someone actually said",
        reactions=["👍"],
        attachments=[MessageAttachment(url="https://cdn/x.png", filename="x.png")],
        sent_at=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        mentions_bot=True,
        reply_to_message_id="443",
        reply_to_author_id="222",
        reply_to_is_self=True,
    )
    return message.model_dump(mode="json") | overrides


def _serialised_model_messages() -> list[dict]:
    """A pydantic-ai message delta carrying every part kind the agent emits."""
    return ModelMessagesTypeAdapter.dump_python(
        [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="you are a bot"),
                    UserPromptPart(content="what someone actually said"),
                ]
            ),
            ModelResponse(
                parts=[
                    TextPart(content="the agent reply"),
                    ToolCallPart(
                        tool_name="web_read",
                        args={"query": "a phrase"},
                        tool_call_id="c1",
                    ),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="web_read",
                        content={"messages": ["what someone actually said"]},
                        tool_call_id="c1",
                    )
                ]
            ),
        ],
        mode="json",
    )


def _compaction_event(**overrides) -> dict:
    """A compaction event exactly as ``chat_compaction`` reports it."""
    return {
        "event_kind": "tool_summary",
        "tool_name": "search",
        "original_content": "everything the channel said",
        "summary": "they said hello",
        "original_chars": 27,
        "summary_chars": 15,
    } | overrides


def _turn_payload(engagement_id: str, **overrides) -> dict:
    """A turn-create request body as the bot sends it, required fields filled."""
    return {
        "engagement_id": engagement_id,
        "request_id": "req-turn",
        "turn_kind": "initial",
        "output_kind": "send_response",
        "triggering_messages": [_serialised_triggering_message()],
        "agent_output": {"topic": "greetings", "notes": "friendly"},
        "model_messages_delta": _serialised_model_messages(),
    } | overrides


async def _post_turn(client: AsyncClient, engagement, **overrides) -> dict:
    """POST one turn for ``engagement`` and return the 201 body."""
    response = await client.post(
        "/api/chat-conversations/turns",
        json=_turn_payload(str(engagement.id), **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestCreateEngagement:
    async def test_creates_and_returns_201(self, client: AsyncClient, session):
        response = await client.post(
            "/api/chat-conversations/engagements",
            json={
                "guild_id": _GUILD,
                "channel_id": _CHANNEL,
                "guild_name": "Guild",
                "channel_name": "general",
                "activation_user_id": "111",
                "activation_username": "alice",
                "activation_message_id": "999",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["id"]
        assert body["started_at"]

        rows = (await session.execute(select(ChatAgentEngagement))).scalars().all()
        assert len(rows) == 1
        assert str(rows[0].id) == body["id"]


class TestEndEngagement:
    async def test_end_marks_reason_and_returns_200(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)

        response = await client.post(
            f"/api/chat-conversations/engagements/{engagement.id}/end",
            json={"deactivation_reason": "inactivity"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(engagement.id)
        assert body["ended_at"]

        await session.refresh(engagement)
        assert engagement.ended_at is not None
        assert engagement.deactivation_reason == "inactivity"

    async def test_unknown_engagement_is_plain_404(self, client: AsyncClient):
        response = await client.post(
            f"/api/chat-conversations/engagements/{uuid4()}/end",
            json={"deactivation_reason": "inactivity"},
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Engagement not found"}

    async def test_malformed_engagement_id_is_422(self, client: AsyncClient):
        response = await client.post(
            "/api/chat-conversations/engagements/not-a-uuid/end",
            json={"deactivation_reason": "inactivity"},
        )
        assert response.status_code == 422


class TestCreateError:
    async def test_persists_full_error_and_returns_admin_url(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)
        response = await client.post(
            "/api/chat-conversations/errors",
            json={
                "engagement_id": str(engagement.id),
                "request_id": "err-1234",
                "guild_id": _GUILD,
                "channel_id": _CHANNEL,
                "model_name": "kimi-k2.6",
                "reasoning_level": "medium",
                "error_type": "pydantic_ai.exceptions.ModelHTTPError",
                "error_message": "status_code: 503",
                "traceback": "Traceback (most recent call last):\\n...",
                "provider_status_code": 503,
                "provider_body": '{"error":{"message":"overloaded"}}',
                "error_context": {"first_activation": True},
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["id"]
        assert body["occurred_at"]
        assert body["admin_url"].endswith(f"/admin/chat-errors/{body['id']}")

        errors = (await session.execute(select(ChatAgentError))).scalars().all()
        assert len(errors) == 1
        error = errors[0]
        assert error.engagement_id == engagement.id
        assert error.request_id == "err-1234"
        assert error.model_name == "kimi-k2.6"
        assert error.reasoning_level == "medium"
        assert error.provider_status_code == 503
        assert "overloaded" in (error.provider_body or "")
        assert error.error_context == {"first_activation": True}


class TestCreateTurn:
    async def test_creates_turn_and_bumps_aggregates(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        body = await _post_turn(
            client,
            engagement,
            chat_tokens_input=100,
            chat_tokens_output=50,
            chat_reasoning_level="high",
        )
        assert body["id"]
        # Unknown / absent model → zero cost, serialised as a string.
        assert body["chat_cost_usd"] == "0"
        assert body["voice_cost_usd"] == "0"
        assert body["summarizer_cost_usd_total"] == "0"

        turn = await _stored_turn(session)
        assert turn.chat_reasoning_level == "high"

        await session.refresh(engagement)
        assert engagement.total_chat_tokens_input == 100
        assert engagement.total_chat_tokens_output == 50
        assert engagement.last_topic == "greetings"
        assert engagement.last_notes == "friendly"

    async def test_unknown_engagement_is_404(self, client: AsyncClient, session):
        response = await client.post(
            "/api/chat-conversations/turns",
            json=_turn_payload(str(uuid4())),
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "Engagement not found"
        assert (await session.execute(select(ChatAgentTurn))).scalars().all() == []

    async def test_replaying_a_request_id_returns_the_first_turn(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)
        metered = {
            "chat_model_name": "kimi-k2.6",
            "chat_tokens_input": 1000,
            "chat_tokens_output": 500,
            "compaction_events": [
                _compaction_event(
                    summarizer_model_name="kimi-k2.6",
                    summarizer_tokens_input=2000,
                    summarizer_tokens_output=100,
                )
            ],
        }

        first = await _post_turn(client, engagement, **metered)
        replay = await _post_turn(client, engagement, **metered)

        assert replay["id"] == first["id"]
        assert replay["started_at"] == first["started_at"]
        assert Decimal(first["summarizer_cost_usd_total"]) > 0
        for cost_field in _TURN_COST_FIELDS:
            assert Decimal(replay[cost_field]) == Decimal(first[cost_field])
        assert str((await _stored_turn(session)).id) == first["id"]
        assert len(await _stored_compaction_events(session)) == 1
        usage_rows = (await session.execute(select(UsageCostRow))).scalars().all()
        assert len(usage_rows) == 2
        await session.refresh(engagement)
        assert engagement.total_chat_tokens_input == 1000
        assert engagement.total_chat_tokens_output == 500

    async def test_reasoning_level_defaults_to_null_when_absent(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        await _post_turn(client, engagement, output_kind="no_response")

        turn = await _stored_turn(session)
        assert turn.chat_reasoning_level is None

    async def test_chat_cache_tokens_flow_to_row_and_discount_cost(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        body = await _post_turn(
            client,
            engagement,
            chat_tokens_input=1000,
            chat_tokens_output=500,
            chat_model_name="kimi-k2.6",
            chat_cache_read_tokens=400,
            chat_cache_write_tokens=0,
        )
        # 600 uncached input @0.76 + 500 out @3.20 + 400 cached @0.19, per Mtok.
        assert body["chat_cost_usd"] == "0.002132"

        turn = await _stored_turn(session)
        assert turn.chat_cache_read_tokens == 400
        assert turn.chat_cache_write_tokens == 0

    async def test_chat_cache_tokens_absent_uses_full_input_rate(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        body = await _post_turn(
            client,
            engagement,
            chat_tokens_input=1000,
            chat_tokens_output=500,
            chat_model_name="kimi-k2.6",
        )
        # No cache split → full 1000 input @0.76 + 500 out @3.20.
        assert body["chat_cost_usd"] == "0.00236"

        turn = await _stored_turn(session)
        assert turn.chat_cache_read_tokens is None
        assert turn.chat_cache_write_tokens is None

    async def test_summarizer_cache_tokens_flow_to_row_and_discount_cost(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        body = await _post_turn(
            client,
            engagement,
            turn_kind="followup",
            output_kind="no_response",
            compaction_events=[
                _compaction_event(
                    event_kind="conversation",
                    tool_name=None,
                    summarizer_tokens_input=2000,
                    summarizer_tokens_output=100,
                    summarizer_model_name="kimi-k2.6",
                    summarizer_cache_read_tokens=1500,
                    summarizer_cache_write_tokens=0,
                )
            ],
        )
        # 500 uncached @0.76 + 100 out @3.20 + 1500 cached @0.19, per Mtok.
        assert body["summarizer_cost_usd_total"] == "0.000985"

        events = await _stored_compaction_events(session)
        assert len(events) == 1
        assert events[0].summarizer_cache_read_tokens == 1500
        assert events[0].summarizer_cache_write_tokens == 0

    async def test_persists_compaction_events(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)

        await _post_turn(
            client,
            engagement,
            turn_kind="followup",
            output_kind="no_response",
            compaction_events=[_compaction_event(summarizer_reasoning_level="low")],
        )

        events = await _stored_compaction_events(session)
        assert len(events) == 1
        assert events[0].chars_saved == 12
        assert events[0].summarizer_reasoning_level == "low"

    async def test_captures_blog_topic_candidates(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)

        await _post_turn(
            client,
            engagement,
            agent_output={
                "blog_topic_candidates": [
                    {
                        "headline": "A neat pattern",
                        "observation": "People keep asking the same thing",
                        "scope": "community",
                        "evidence": ["msg1", "msg2"],
                        "category": "trend",
                    },
                    {"headline": "", "observation": "dropped — no headline"},
                ]
            },
        )

        candidates = (await session.execute(select(CandidateBlogTopic))).scalars().all()
        assert len(candidates) == 1
        assert candidates[0].engagement_id == engagement.id
        assert candidates[0].turn_id == (await _stored_turn(session)).id
        assert candidates[0].headline == "A neat pattern"
        assert candidates[0].evidence == ["msg1", "msg2"]


class TestTurnUsageCostRows:
    """The metering rows a turn files, one per model that billed for it."""

    async def test_chat_model_files_a_primary_row(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)

        await _post_turn(
            client,
            engagement,
            request_id="req-usage",
            chat_model_name="kimi-k3",
            chat_reasoning_level="high",
            chat_tokens_input=1000,
            chat_tokens_output=500,
            chat_cache_read_tokens=400,
            chat_cache_write_tokens=7,
        )

        turn = await _stored_turn(session)
        row = await _stored_usage_cost_row(session, "primary")
        assert row.operation_key == f"discord:turn:{turn.id}:primary"
        assert row.product_mode == "discord"
        assert row.discord_user_id == engagement.activation_user_id
        assert row.conversation_id == engagement.id
        assert row.root_turn_id == turn.id
        assert row.provider_key == "opencode_zen"
        assert row.catalog_model_key == "kimi-k3"
        assert row.model_id == "kimi-k3"
        assert row.reasoning_level == "high"
        assert row.input_tokens == 1000
        assert row.output_tokens == 500
        assert row.cache_read_tokens == 400
        assert row.cache_write_tokens == 7
        assert row.cost_usd == turn.chat_cost_usd
        assert row.details == {"request_id": "req-usage"}

    async def test_voice_model_files_a_voice_row(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)

        await _post_turn(
            client,
            engagement,
            request_id="req-usage",
            voice_model_name="kimi-k3",
            voice_tokens_input=30,
            voice_tokens_output=40,
        )

        turn = await _stored_turn(session)
        row = await _stored_usage_cost_row(session, "voice")
        assert row.operation_key == f"discord:turn:{turn.id}:voice"
        assert row.root_turn_id == turn.id
        assert row.discord_user_id == engagement.activation_user_id
        assert row.reasoning_level is None
        assert row.input_tokens == 30
        assert row.output_tokens == 40
        assert row.cache_read_tokens == 0
        assert row.cache_write_tokens == 0
        assert row.cost_usd == turn.voice_cost_usd
        assert row.details == {"request_id": "req-usage"}

    async def test_each_summarized_compaction_files_a_compaction_row(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        await _post_turn(
            client,
            engagement,
            compaction_events=[
                _compaction_event(
                    summarizer_model_name="kimi-k3",
                    summarizer_reasoning_level="low",
                    summarizer_tokens_input=200,
                    summarizer_tokens_output=20,
                    summarizer_cache_read_tokens=100,
                    summarizer_cache_write_tokens=0,
                )
            ],
        )

        turn = await _stored_turn(session)
        event = (await _stored_compaction_events(session))[0]
        row = await _stored_usage_cost_row(session, "compaction")
        assert row.operation_key == f"discord:turn:{turn.id}:compaction:{event.id}"
        assert row.root_turn_id == turn.id
        assert row.reasoning_level == "low"
        assert row.input_tokens == 200
        assert row.output_tokens == 20
        assert row.cache_read_tokens == 100
        assert row.cost_usd == event.summarizer_cost_usd
        assert row.details == {"event_kind": "tool_summary"}

    async def test_a_turn_without_model_names_files_nothing(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        await _post_turn(client, engagement, compaction_events=[_compaction_event()])

        rows = (await session.execute(select(UsageCostRow))).scalars().all()
        assert list(rows) == []


class TestUsageLeaderboard:
    async def _seed_turn(
        self, session, engagement, tokens_in: int, tokens_out: int, started_at=None
    ) -> None:
        session.add(
            ChatAgentTurn(
                engagement_id=engagement.id,
                request_id="req",
                turn_kind="initial",
                output_kind="send_response",
                triggering_messages=[],
                agent_output={},
                started_at=started_at or datetime.now(UTC),
                chat_tokens_input=tokens_in,
                chat_tokens_output=tokens_out,
            )
        )
        await session.commit()

    async def test_returns_channels_ordered_descending(
        self, client: AsyncClient, session
    ):
        busy = await _seed_engagement(session, channel_id="C-busy", channel_name="busy")
        quiet = await _seed_engagement(
            session, channel_id="C-quiet", channel_name="quiet"
        )
        await self._seed_turn(session, busy, 1000, 500)
        await self._seed_turn(session, quiet, 100, 50)

        response = await client.get(
            "/api/chat-conversations/usage-leaderboard",
            params={"guild_id": _GUILD, "days": 1, "limit": 20},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["days"] == 1
        assert body["total_tokens_all_time"] == 1650
        assert body["total_tokens_in_window"] == 1650
        assert [(e["channel_id"], e["total_tokens"]) for e in body["entries"]] == [
            ("C-busy", 1500),
            ("C-quiet", 150),
        ]

    async def test_excludes_turns_outside_window(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session, channel_id="C1")
        old = datetime.now(UTC) - timedelta(days=10)
        await self._seed_turn(session, engagement, 999, 999, started_at=old)
        await self._seed_turn(session, engagement, 100, 0)

        response = await client.get(
            "/api/chat-conversations/usage-leaderboard",
            params={"guild_id": _GUILD, "days": 7},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["total_tokens_in_window"] == 100
        assert body["total_tokens_all_time"] == 2098

    async def test_days_out_of_range_is_422(self, client: AsyncClient):
        response = await client.get(
            "/api/chat-conversations/usage-leaderboard",
            params={"guild_id": _GUILD, "days": 0},
        )
        assert response.status_code == 422

    async def test_limit_out_of_range_is_422(self, client: AsyncClient):
        response = await client.get(
            "/api/chat-conversations/usage-leaderboard",
            params={"guild_id": _GUILD, "limit": 500},
        )
        assert response.status_code == 422

    async def test_missing_guild_id_is_422(self, client: AsyncClient):
        response = await client.get("/api/chat-conversations/usage-leaderboard")
        assert response.status_code == 422


class TestTurnStoresPlaceholdersForMessageText:
    async def test_triggering_message_body_is_a_placeholder(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        await _post_turn(client, engagement)

        turn = await _stored_turn(session)
        stored_message = turn.triggering_messages[0]
        assert stored_message["body"] == MESSAGE_CONTENT_PLACEHOLDER
        assert stored_message["attachments"] == []

    async def test_triggering_message_metadata_survives(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        await _post_turn(client, engagement)

        stored_message = (await _stored_turn(session)).triggering_messages[0]
        assert stored_message["message_id"] == "444"
        assert stored_message["author_id"] == "333"
        assert stored_message["reply_to_message_id"] == "443"
        assert stored_message["reply_to_author_id"] == "222"
        assert stored_message["reply_to_is_self"] is True
        assert stored_message["mentions_bot"] is True
        assert stored_message["reactions"] == ["👍"]
        assert stored_message["sent_at"].startswith("2026-07-26T12:00:00")

    async def test_empty_body_stays_empty(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)

        await _post_turn(
            client,
            engagement,
            triggering_messages=[_serialised_triggering_message(body="")],
        )

        assert (await _stored_turn(session)).triggering_messages[0]["body"] == ""

    async def test_no_triggering_messages_stores_empty_list(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        await _post_turn(client, engagement, triggering_messages=[])

        assert (await _stored_turn(session)).triggering_messages == []

    async def test_agent_output_survives_verbatim(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)
        agent_output = {
            "topic": "greetings",
            "notes": "friendly",
            "response": {"message": "hello there", "target_message_id": "444"},
        }

        await _post_turn(client, engagement, agent_output=agent_output)

        assert (await _stored_turn(session)).agent_output == agent_output


class TestTurnDeltaRedaction:
    async def test_user_prompt_and_tool_return_content_are_placeholders(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        await _post_turn(client, engagement)

        parts = [
            part
            for message in (await _stored_turn(session)).model_messages_delta
            for part in message["parts"]
        ]
        by_kind = {part["part_kind"]: part for part in parts}
        assert by_kind["user-prompt"]["content"] == MESSAGE_CONTENT_PLACEHOLDER
        assert by_kind["tool-return"]["content"] == {}

    async def test_text_tool_call_and_system_parts_survive(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        await _post_turn(client, engagement)

        parts = [
            part
            for message in (await _stored_turn(session)).model_messages_delta
            for part in message["parts"]
        ]
        by_kind = {part["part_kind"]: part for part in parts}
        assert by_kind["text"]["content"] == "the agent reply"
        assert by_kind["tool-call"]["tool_name"] == "web_read"
        assert by_kind["tool-call"]["args"] == {"query": "a phrase"}
        assert by_kind["system-prompt"]["content"] == "you are a bot"
        assert by_kind["tool-return"]["tool_name"] == "web_read"

    async def test_absent_delta_stays_null(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)

        await _post_turn(client, engagement, model_messages_delta=None)

        assert (await _stored_turn(session)).model_messages_delta is None


class TestCompactionEventRedaction:
    async def test_original_content_is_a_placeholder_and_metrics_survive(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        await _post_turn(
            client,
            engagement,
            compaction_events=[_compaction_event()],
        )

        events = await _stored_compaction_events(session)
        assert len(events) == 1
        assert events[0].original_content == MESSAGE_CONTENT_PLACEHOLDER
        assert events[0].summary == "they said hello"
        assert events[0].original_chars == 27
        assert events[0].summary_chars == 15
        assert events[0].chars_saved == 12
        assert events[0].tool_name == "search"

    async def test_empty_original_content_stays_empty(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        await _post_turn(
            client,
            engagement,
            compaction_events=[
                _compaction_event(
                    original_content="",
                    summary="nothing to say",
                    original_chars=0,
                    summary_chars=14,
                )
            ],
        )

        events = await _stored_compaction_events(session)
        assert events[0].original_content == ""


class TestDetailTemplateRendersRedactedRows:
    async def test_renders_placeholders_alongside_surviving_detail(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)
        await _post_turn(
            client,
            engagement,
            compaction_events=[_compaction_event()],
        )
        turn = await _stored_turn(session)

        html = render_admin_template(
            "admin/chat-conversations/detail.html",
            engagement=engagement,
            turns=[turn],
        )

        assert MESSAGE_CONTENT_PLACEHOLDER in html
        assert "what someone actually said" not in html
        assert "everything the channel said" not in html
        assert "they said hello" in html
        assert "search" in html
        assert "web_read" in html
        assert re.search(r"returned \d+ chars", html) is None

    async def test_explains_message_text_is_never_stored(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)
        await _post_turn(client, engagement)
        turn = await _stored_turn(session)

        html = render_admin_template(
            "admin/chat-conversations/detail.html",
            engagement=engagement,
            turns=[turn],
        )

        assert "Discord message text is never stored" in html
        assert "kept for 48 hours" not in html
        assert "purged" not in html

    async def test_purge_stamp_names_derived_text_only(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)
        await _post_turn(client, engagement)
        turn = await _stored_turn(session)
        turn.content_purged_at = datetime(2026, 7, 28, 14, 30, tzinfo=UTC)

        html = render_admin_template(
            "admin/chat-conversations/detail.html",
            engagement=engagement,
            turns=[turn],
        )

        assert "Derived text purged 2026-07-28 14:30 UTC" in html
        assert "Discord message text is never stored" in html
        assert "kept for 48 hours" not in html
