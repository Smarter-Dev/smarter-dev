"""Tests for the Scan research agent system."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from smarter_dev.web.scan.schemas import (
    FollowupRequest,
    ResearchRequest,
    ResearchResponse,
    ResearchSessionSchema,
    SourceSchema,
)
from smarter_dev.web.scan.tools import URLRateLimiter, brave_search, jina_read
from smarter_dev.web.scan.crud import ResearchSessionOperations
from smarter_dev.web.scan.agent import ResearchDeps, ResearchResult, Source, research_agent, generate_session_name
from smarter_dev.web.models import ResearchSession


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_research_request_valid(self):
        req = ResearchRequest(query="how to use asyncio", user_id="123")
        assert req.query == "how to use asyncio"
        assert req.user_id == "123"
        assert req.guild_id is None

    def test_research_request_with_context(self):
        req = ResearchRequest(
            query="test",
            user_id="123",
            guild_id="456",
            channel_id="789",
            context={"key": "val"},
        )
        assert req.guild_id == "456"
        assert req.context == {"key": "val"}

    def test_research_request_empty_query_rejected(self):
        with pytest.raises(Exception):
            ResearchRequest(query="", user_id="123")

    def test_research_response(self):
        sid = uuid4()
        resp = ResearchResponse(
            session_id=sid,
            source_key=f"research:{sid}",
            stream_url=f"/api/research/{sid}/stream",
        )
        assert resp.status == "running"

    def test_source_schema_defaults(self):
        src = SourceSchema(url="https://example.com", title="Example")
        assert src.type == "other"
        assert src.cited is False

    def test_followup_request(self):
        req = FollowupRequest(query="tell me more")
        assert req.query == "tell me more"

    def test_research_session_schema_name(self):
        schema = ResearchSessionSchema(
            id=uuid4(),
            query="how to use asyncio",
            name="Asyncio Usage Guide",
            user_id="123",
            status="complete",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
        assert schema.name == "Asyncio Usage Guide"

    def test_research_session_schema_name_optional(self):
        schema = ResearchSessionSchema(
            id=uuid4(),
            query="test",
            user_id="123",
            status="running",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        )
        assert schema.name is None

    def test_research_result(self):
        result = ResearchResult(
            response="# Answer\nSome response",
            sources=[Source(url="https://example.com", title="Example", type="docs", cited=True)],
            summary="A short summary.",
        )
        assert len(result.sources) == 1
        assert result.sources[0].cited is True


# ---------------------------------------------------------------------------
# Tools tests
# ---------------------------------------------------------------------------


class TestURLRateLimiter:
    @pytest.mark.asyncio
    async def test_first_request_no_delay(self):
        limiter = URLRateLimiter()
        # Should not delay on first request
        await limiter.wait_if_needed("https://example.com/page1")
        assert "example.com" in limiter._last_request

    @pytest.mark.asyncio
    async def test_different_domains_no_delay(self):
        limiter = URLRateLimiter()
        await limiter.wait_if_needed("https://example.com")
        await limiter.wait_if_needed("https://other.com")
        assert len(limiter._last_request) == 2


class TestBraveSearch:
    @pytest.mark.asyncio
    async def test_no_api_key_returns_error(self):
        with patch.dict("os.environ", {}, clear=True):
            async with httpx.AsyncClient() as client:
                results = await brave_search(client, "test query")
        assert len(results) == 1
        assert "error" in results[0]

    @pytest.mark.asyncio
    async def test_successful_search(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "web": {
                "results": [
                    {
                        "title": "Result 1",
                        "url": "https://example.com/1",
                        "description": "First result",
                    },
                    {
                        "title": "Result 2",
                        "url": "https://example.com/2",
                        "description": "Second result",
                    },
                ]
            }
        }

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": "test-key"}):
            results = await brave_search(mock_client, "test query", num_results=5)

        assert len(results) == 2
        assert results[0]["title"] == "Result 1"
        assert results[1]["url"] == "https://example.com/2"

    @pytest.mark.asyncio
    async def test_num_results_capped_at_20(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"web": {"results": []}}
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": "test-key"}):
            await brave_search(mock_client, "test", num_results=50)

        call_kwargs = mock_client.get.call_args
        assert call_kwargs.kwargs["params"]["count"] == 20


class TestJinaRead:
    @pytest.mark.asyncio
    async def test_successful_read(self):
        mock_response = httpx.Response(
            200,
            json={
                "data": {
                    "title": "Test Page",
                    "description": "A test page",
                    "content": "# Hello\nThis is content",
                    "url": "https://example.com",
                }
            },
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await jina_read(mock_client, "https://example.com")
        assert result["title"] == "Test Page"
        assert result["content"] == "# Hello\nThis is content"

    @pytest.mark.asyncio
    async def test_non_200_returns_error(self):
        mock_response = httpx.Response(403, json={})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await jina_read(mock_client, "https://example.com")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("fail"))

        result = await jina_read(mock_client, "https://example.com")
        assert "error" in result


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


class TestResearchSessionCRUD:
    @pytest.mark.asyncio
    async def test_create_session(self, db_session):
        ops = ResearchSessionOperations()
        session = await ops.create_session(
            db_session,
            query="how to use pytest",
            user_id="user123",
            name="Pytest Usage Guide",
            guild_id="guild456",
        )
        await db_session.commit()

        assert session.id is not None
        assert session.query == "how to use pytest"
        assert session.name == "Pytest Usage Guide"
        assert session.user_id == "user123"
        assert session.status == "running"

    @pytest.mark.asyncio
    async def test_get_session(self, db_session):
        ops = ResearchSessionOperations()
        created = await ops.create_session(
            db_session, query="test query", user_id="user1"
        )
        await db_session.commit()

        fetched = await ops.get_session(db_session, created.id)
        assert fetched is not None
        assert fetched.query == "test query"

    @pytest.mark.asyncio
    async def test_get_session_not_found(self, db_session):
        ops = ResearchSessionOperations()
        result = await ops.get_session(db_session, uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_update_session_result(self, db_session):
        ops = ResearchSessionOperations()
        session = await ops.create_session(
            db_session, query="test", user_id="user1"
        )
        await db_session.commit()

        await ops.update_session_result(
            db_session,
            session.id,
            response="# Answer\nHere is the answer.",
            summary="Short summary.",
            sources=[{"url": "https://example.com", "title": "Example"}],
            tool_log=[{"tool": "brave_search", "status": "complete"}],
        )

        fetched = await ops.get_session(db_session, session.id)
        assert fetched.status == "complete"
        assert fetched.response == "# Answer\nHere is the answer."
        assert fetched.summary == "Short summary."
        assert len(fetched.sources) == 1

    @pytest.mark.asyncio
    async def test_update_session_error(self, db_session):
        ops = ResearchSessionOperations()
        session = await ops.create_session(
            db_session, query="test", user_id="user1"
        )
        await db_session.commit()

        await ops.update_session_error(
            db_session, session.id, "RuntimeError: something broke"
        )

        fetched = await ops.get_session(db_session, session.id)
        assert fetched.status == "error"
        assert fetched.error_message == "RuntimeError: something broke"

    @pytest.mark.asyncio
    async def test_add_followup(self, db_session):
        ops = ResearchSessionOperations()
        parent = await ops.create_session(
            db_session, query="parent query", user_id="user1"
        )
        await db_session.commit()

        followup_id = uuid4()
        await ops.add_followup(
            db_session, parent.id, followup_id, "followup question"
        )

        fetched = await ops.get_session(db_session, parent.id)
        assert len(fetched.followups) == 1
        assert fetched.followups[0]["query"] == "followup question"

    @pytest.mark.asyncio
    async def test_list_sessions(self, db_session):
        ops = ResearchSessionOperations()
        await ops.create_session(db_session, query="q1", user_id="user1")
        await ops.create_session(db_session, query="q2", user_id="user1")
        await ops.create_session(db_session, query="q3", user_id="other_user")
        await db_session.commit()

        results = await ops.list_sessions(db_session, "user1")
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Agent definition tests
# ---------------------------------------------------------------------------


class TestAgentDefinition:
    def test_agent_has_tools(self):
        tool_names = [t.name for t in research_agent._function_toolset.tools.values()]
        assert "brave_search" in tool_names
        assert "jina_read" in tool_names

    def test_research_result_type(self):
        result = ResearchResult(
            response="test",
            sources=[],
            summary="test summary",
        )
        assert isinstance(result.response, str)
        assert isinstance(result.sources, list)

    def test_research_deps(self):
        deps = ResearchDeps(
            session_id="test-id",
            http_client=MagicMock(),
            url_rate_limiter=URLRateLimiter(),
        )
        assert deps.session_id == "test-id"

    @pytest.mark.asyncio
    async def test_generate_session_name_fallback(self):
        """When the naming agent fails, falls back to truncated query."""
        with patch("smarter_dev.web.scan.agent._naming_agent") as mock_agent:
            mock_agent.run = AsyncMock(side_effect=Exception("API error"))
            name = await generate_session_name("a long query about testing")
            assert name == "a long query about testing"

    @pytest.mark.asyncio
    async def test_generate_session_name_truncates(self):
        """Fallback truncates query to 200 chars."""
        long_query = "x" * 300
        with patch("smarter_dev.web.scan.agent._naming_agent") as mock_agent:
            mock_agent.run = AsyncMock(side_effect=Exception("fail"))
            name = await generate_session_name(long_query)
            assert len(name) == 200


# ---------------------------------------------------------------------------
# Runner notification tests
# ---------------------------------------------------------------------------


class TestRunnerNotifications:
    @pytest.mark.asyncio
    async def test_emit_calls_notify_source(self):
        with patch("smarter_dev.web.scan.runner.notify_source", new_callable=AsyncMock) as mock_notify:
            from smarter_dev.web.scan.runner import _emit

            await _emit("test-session-id", "status", stage="planning", message="Analyzing...")

            mock_notify.assert_called_once()
            call_kwargs = mock_notify.call_args
            assert call_kwargs.args[0] == "research:test-session-id"
            assert call_kwargs.args[1] == "status"
            assert call_kwargs.kwargs["stage"] == "planning"

    @pytest.mark.asyncio
    async def test_emit_uses_timeseries_mode(self):
        with patch("smarter_dev.web.scan.runner.notify_source", new_callable=AsyncMock) as mock_notify:
            from smarter_dev.web.scan.runner import _emit
            from skrift.notifications import NotificationMode

            await _emit("sid", "tool_use", tool="brave_search")

            call_kwargs = mock_notify.call_args
            assert call_kwargs.kwargs["mode"] == NotificationMode.TIMESERIES


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestResearchSessionModel:
    @pytest.mark.asyncio
    async def test_model_defaults(self, db_session):
        """SQLAlchemy defaults are applied at flush/insert time, not in Python."""
        session = ResearchSession(
            query="test query",
            user_id="user123",
            status="running",
        )
        db_session.add(session)
        await db_session.flush()

        assert session.status == "running"
        assert session.response is None
        assert session.name is None
        assert session.guild_id is None

    def test_model_repr(self):
        session = ResearchSession(query="test", user_id="u1")
        repr_str = repr(session)
        assert "ResearchSession" in repr_str
