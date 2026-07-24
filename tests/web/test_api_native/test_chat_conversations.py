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

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.shared.database import Base
from smarter_dev.web.api_native import chat_conversations as chat_module
from smarter_dev.web.api_native.chat_conversations import ChatConversationController
from smarter_dev.web.models import (
    CandidateBlogTopic,
    ChatAgentCompactionEvent,
    ChatAgentEngagement,
    ChatAgentError,
    ChatAgentTurn,
)

_GUILD = "123456789012345678"
_CHANNEL = "555000111222333444"


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
            dependencies={
                "db_session": Provide(lambda: session, sync_to_thread=False)
            },
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
    async def test_creates_turn_and_bumps_aggregates(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)

        response = await client.post(
            "/api/chat-conversations/turns",
            json={
                "engagement_id": str(engagement.id),
                "request_id": "req-1",
                "turn_kind": "initial",
                "output_kind": "send_response",
                "triggering_messages": [{"id": "m1"}],
                "agent_output": {"topic": "greetings", "notes": "friendly"},
                "chat_tokens_input": 100,
                "chat_tokens_output": 50,
                "chat_reasoning_level": "high",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["id"]
        # Unknown / absent model → zero cost, serialised as a string.
        assert body["chat_cost_usd"] == "0"
        assert body["voice_cost_usd"] == "0"
        assert body["summarizer_cost_usd_total"] == "0"

        turns = (await session.execute(select(ChatAgentTurn))).scalars().all()
        assert len(turns) == 1
        assert turns[0].chat_reasoning_level == "high"

        await session.refresh(engagement)
        assert engagement.total_chat_tokens_input == 100
        assert engagement.total_chat_tokens_output == 50
        assert engagement.last_topic == "greetings"
        assert engagement.last_notes == "friendly"

    async def test_reasoning_level_defaults_to_null_when_absent(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        response = await client.post(
            "/api/chat-conversations/turns",
            json={
                "engagement_id": str(engagement.id),
                "request_id": "req-noreason",
                "turn_kind": "initial",
                "output_kind": "no_response",
                "triggering_messages": [],
                "agent_output": {},
            },
        )
        assert response.status_code == 201

        turns = (await session.execute(select(ChatAgentTurn))).scalars().all()
        assert len(turns) == 1
        assert turns[0].chat_reasoning_level is None

    async def test_chat_cache_tokens_flow_to_row_and_discount_cost(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        response = await client.post(
            "/api/chat-conversations/turns",
            json={
                "engagement_id": str(engagement.id),
                "request_id": "req-cache",
                "turn_kind": "initial",
                "output_kind": "send_response",
                "triggering_messages": [],
                "agent_output": {"topic": "t"},
                "chat_tokens_input": 1000,
                "chat_tokens_output": 500,
                "chat_model_name": "kimi-k2.6",
                "chat_cache_read_tokens": 400,
                "chat_cache_write_tokens": 0,
            },
        )
        assert response.status_code == 201
        body = response.json()
        # 600 uncached input @0.76 + 500 out @3.20 + 400 cached @0.19, per Mtok.
        assert body["chat_cost_usd"] == "0.002132"

        turns = (await session.execute(select(ChatAgentTurn))).scalars().all()
        assert len(turns) == 1
        assert turns[0].chat_cache_read_tokens == 400
        assert turns[0].chat_cache_write_tokens == 0

    async def test_chat_cache_tokens_absent_uses_full_input_rate(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        response = await client.post(
            "/api/chat-conversations/turns",
            json={
                "engagement_id": str(engagement.id),
                "request_id": "req-nocache",
                "turn_kind": "initial",
                "output_kind": "send_response",
                "triggering_messages": [],
                "agent_output": {"topic": "t"},
                "chat_tokens_input": 1000,
                "chat_tokens_output": 500,
                "chat_model_name": "kimi-k2.6",
            },
        )
        assert response.status_code == 201
        body = response.json()
        # No cache split → full 1000 input @0.76 + 500 out @3.20.
        assert body["chat_cost_usd"] == "0.00236"

        turns = (await session.execute(select(ChatAgentTurn))).scalars().all()
        assert len(turns) == 1
        assert turns[0].chat_cache_read_tokens is None
        assert turns[0].chat_cache_write_tokens is None

    async def test_summarizer_cache_tokens_flow_to_row_and_discount_cost(
        self, client: AsyncClient, session
    ):
        engagement = await _seed_engagement(session)

        response = await client.post(
            "/api/chat-conversations/turns",
            json={
                "engagement_id": str(engagement.id),
                "request_id": "req-summ-cache",
                "turn_kind": "followup",
                "output_kind": "no_response",
                "triggering_messages": [],
                "agent_output": {},
                "compaction_events": [
                    {
                        "event_kind": "conversation",
                        "tool_name": None,
                        "original_content": "aaaa",
                        "summary": "a",
                        "original_chars": 4,
                        "summary_chars": 1,
                        "summarizer_tokens_input": 2000,
                        "summarizer_tokens_output": 100,
                        "summarizer_model_name": "kimi-k2.6",
                        "summarizer_cache_read_tokens": 1500,
                        "summarizer_cache_write_tokens": 0,
                    }
                ],
            },
        )
        assert response.status_code == 201
        body = response.json()
        # 500 uncached @0.76 + 100 out @3.20 + 1500 cached @0.19, per Mtok.
        assert body["summarizer_cost_usd_total"] == "0.000985"

        events = (
            await session.execute(select(ChatAgentCompactionEvent))
        ).scalars().all()
        assert len(events) == 1
        assert events[0].summarizer_cache_read_tokens == 1500
        assert events[0].summarizer_cache_write_tokens == 0

    async def test_persists_compaction_events(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)

        response = await client.post(
            "/api/chat-conversations/turns",
            json={
                "engagement_id": str(engagement.id),
                "request_id": "req-2",
                "turn_kind": "followup",
                "output_kind": "no_response",
                "triggering_messages": [],
                "agent_output": {},
                "compaction_events": [
                    {
                        "event_kind": "tool_summary",
                        "tool_name": "search",
                        "original_content": "aaaa",
                        "summary": "a",
                        "original_chars": 4,
                        "summary_chars": 1,
                        "summarizer_reasoning_level": "low",
                    }
                ],
            },
        )
        assert response.status_code == 201

        events = (
            await session.execute(select(ChatAgentCompactionEvent))
        ).scalars().all()
        assert len(events) == 1
        assert events[0].chars_saved == 3
        assert events[0].summarizer_reasoning_level == "low"

    async def test_captures_blog_topic_candidates(self, client: AsyncClient, session):
        engagement = await _seed_engagement(session)

        response = await client.post(
            "/api/chat-conversations/turns",
            json={
                "engagement_id": str(engagement.id),
                "request_id": "req-3",
                "turn_kind": "initial",
                "output_kind": "send_response",
                "triggering_messages": [],
                "agent_output": {
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
            },
        )
        assert response.status_code == 201

        candidates = (
            await session.execute(select(CandidateBlogTopic))
        ).scalars().all()
        assert len(candidates) == 1
        assert candidates[0].headline == "A neat pattern"
        assert candidates[0].evidence == ["msg1", "msg2"]


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
                started_at=started_at or datetime.now(timezone.utc),
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
        old = datetime.now(timezone.utc) - timedelta(days=10)
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
