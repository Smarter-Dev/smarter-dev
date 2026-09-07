"""Tests for the 48-hour Discord content retention sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from smarter_dev.bot.agents.chat_compaction import KEEP_RECENT_CHARS
from smarter_dev.bot.proactive.agent import COMPACTION_KEEP_MESSAGES
from smarter_dev.bot.services.chat_memory import HISTORY_TTL_SECONDS

from smarter_dev.web.models import (
    CONTENT_RETENTION_WINDOW,
    ChatAgentCompactionEvent,
    ChatAgentEngagement,
    ChatAgentError,
    ChatAgentGuildMemory,
    ChatAgentMemoryNote,
    ChatAgentMemoryRevision,
    ChatAgentTurn,
    ForumAgent,
    ForumAgentResponse,
    HandlerRun,
    HelpConversation,
    ModerationAction,
    ChannelHandler,
)
from smarter_dev.shared.message_content import MESSAGE_CONTENT_PLACEHOLDER
from smarter_dev.web import retention
from smarter_dev.web.retention import SCRUBBERS, run_retention_sweep

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
STALE = NOW - CONTENT_RETENTION_WINDOW - timedelta(minutes=1)
FRESH = NOW - CONTENT_RETENTION_WINDOW + timedelta(minutes=1)


def purged_at(value: datetime | None) -> datetime | None:
    """Normalise a stamp read back from the test DB.

    The test suite runs on SQLite, which drops tzinfo on round-trip; production
    is Postgres with ``timestamptz``. Compare in UTC either way.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _help_conversation(created_at: datetime, **overrides) -> HelpConversation:
    fields = {
        "session_id": uuid4().hex,
        "guild_id": "111",
        "channel_id": "222",
        "user_id": "333",
        "user_username": "someone",
        "interaction_type": "slash_command",
        "context_messages": [{"author": "someone", "content": "hi there"}],
        "user_question": "how do I center a div",
        "bot_response": "flexbox",
        "tokens_used": 120,
        "created_at": created_at,
        "started_at": created_at,
        "last_activity_at": created_at,
    }
    fields.update(overrides)
    return HelpConversation(**fields)


async def _engagement(session, started_at: datetime) -> ChatAgentEngagement:
    engagement = ChatAgentEngagement(
        guild_id="111",
        channel_id="222",
        activation_user_id="333",
        activation_username="someone",
        activation_message_id="444",
        started_at=started_at,
        last_topic="what the channel was talking about",
        last_notes="notes the agent kept about the humans",
        total_chat_tokens_input=1000,
        total_cost_usd=Decimal("0.0042"),
    )
    session.add(engagement)
    await session.flush()
    return engagement


async def _turn(session, engagement, started_at: datetime) -> ChatAgentTurn:
    turn = ChatAgentTurn(
        engagement_id=engagement.id,
        request_id="req123",
        turn_kind="initial",
        output_kind="send_response",
        triggering_messages=[{"author": "someone", "content": "hey bot"}],
        agent_output={"response": "hey yourself", "topic": "greetings"},
        model_messages_delta=[{"parts": [{"content": "hey bot"}]}],
        started_at=started_at,
        chat_tokens_input=900,
        chat_tokens_output=120,
        chat_model_name="gemini-3.1-flash-lite",
        chat_cost_usd=Decimal("0.0011"),
    )
    session.add(turn)
    await session.flush()
    return turn


class TestHelpConversations:
    async def test_scrubs_text_past_the_window(self, db_session):
        db_session.add(_help_conversation(STALE))
        await db_session.flush()

        result = await run_retention_sweep(db_session, now=NOW)

        conversation = (
            await db_session.execute(select(HelpConversation))
        ).scalar_one()
        assert conversation.user_question == ""
        assert conversation.bot_response == ""
        assert conversation.context_messages == []
        assert purged_at(conversation.content_purged_at) == NOW
        assert result.counts["help_conversations"] == 1

    async def test_keeps_usage_metrics(self, db_session):
        db_session.add(_help_conversation(STALE))
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        conversation = (
            await db_session.execute(select(HelpConversation))
        ).scalar_one()
        assert conversation.tokens_used == 120
        assert conversation.guild_id == "111"
        assert conversation.user_id == "333"
        assert conversation.interaction_type == "slash_command"

    async def test_leaves_conversations_inside_the_window(self, db_session):
        db_session.add(_help_conversation(FRESH))
        await db_session.flush()

        result = await run_retention_sweep(db_session, now=NOW)

        conversation = (
            await db_session.execute(select(HelpConversation))
        ).scalar_one()
        assert conversation.user_question == "how do I center a div"
        assert conversation.content_purged_at is None
        assert result.counts["help_conversations"] == 0

    async def test_expires_at_defaults_to_the_window(self):
        conversation = _help_conversation(NOW, retention_policy="standard")
        assert conversation.expires_at == conversation.created_at + CONTENT_RETENTION_WINDOW


class TestChatAgent:
    async def test_scrubs_turn_content_and_keeps_cost(self, db_session):
        engagement = await _engagement(db_session, STALE)
        await _turn(db_session, engagement, STALE)

        await run_retention_sweep(db_session, now=NOW)

        turn = (await db_session.execute(select(ChatAgentTurn))).scalar_one()
        assert turn.triggering_messages == []
        assert turn.agent_output == {}
        assert turn.model_messages_delta is None
        assert purged_at(turn.content_purged_at) == NOW
        # The numbers the operator dashboard runs on survive untouched.
        assert turn.chat_tokens_input == 900
        assert turn.chat_tokens_output == 120
        assert turn.chat_cost_usd == Decimal("0.0011")
        assert turn.chat_model_name == "gemini-3.1-flash-lite"

    async def test_scrubs_engagement_topic_and_notes(self, db_session):
        await _engagement(db_session, STALE)

        await run_retention_sweep(db_session, now=NOW)

        engagement = (
            await db_session.execute(select(ChatAgentEngagement))
        ).scalar_one()
        assert engagement.last_topic is None
        assert engagement.last_notes is None
        assert engagement.total_chat_tokens_input == 1000
        assert engagement.total_cost_usd == Decimal("0.0042")
        # Identity fields stay — an abuse report is useless without them.
        assert engagement.activation_user_id == "333"

    async def test_compaction_events_follow_their_turn(self, db_session):
        engagement = await _engagement(db_session, STALE)
        turn = await _turn(db_session, engagement, STALE)
        db_session.add(
            ChatAgentCompactionEvent(
                turn_id=turn.id,
                event_kind="tool_result",
                original_content="a long stretch of what people said",
                summary="they said things",
                original_chars=34,
                summary_chars=16,
                chars_saved=18,
            )
        )
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        event = (
            await db_session.execute(select(ChatAgentCompactionEvent))
        ).scalar_one()
        assert event.original_content == ""
        assert event.summary == ""
        assert purged_at(event.content_purged_at) == NOW
        # Ratio arithmetic survives so the compaction dashboards keep working.
        assert (event.original_chars, event.summary_chars) == (34, 16)

    async def test_fresh_compaction_events_are_untouched(self, db_session):
        engagement = await _engagement(db_session, FRESH)
        turn = await _turn(db_session, engagement, FRESH)
        db_session.add(
            ChatAgentCompactionEvent(
                turn_id=turn.id,
                event_kind="tool_result",
                original_content="still inside the window",
                summary="recent",
                original_chars=23,
                summary_chars=6,
                chars_saved=17,
            )
        )
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        event = (
            await db_session.execute(select(ChatAgentCompactionEvent))
        ).scalar_one()
        assert event.original_content == "still inside the window"

    async def test_scrubs_provider_body_but_keeps_the_traceback(self, db_session):
        db_session.add(
            ChatAgentError(
                request_id="req123",
                guild_id="111",
                channel_id="222",
                error_type="ModelAPIError",
                error_message="502 from provider",
                traceback="Traceback (most recent call last): ...",
                provider_body='{"echo": "the prompt, with message text in it"}',
                occurred_at=STALE,
            )
        )
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        error = (await db_session.execute(select(ChatAgentError))).scalar_one()
        assert error.provider_body is None
        assert error.error_type == "ModelAPIError"
        assert error.traceback.startswith("Traceback")


class TestForumAgentResponses:
    async def _agent(self, session) -> ForumAgent:
        agent = ForumAgent(
            guild_id="111",
            name="helper",
            system_prompt="be helpful",
            monitored_forums=["222"],
            created_by="333",
        )
        session.add(agent)
        await session.flush()
        return agent

    async def test_scrubs_post_and_response(self, db_session):
        agent = await self._agent(db_session)
        db_session.add(
            ForumAgentResponse(
                agent_id=agent.id,
                guild_id="111",
                channel_id="222",
                thread_id="333",
                post_title="how do I deploy this",
                post_content="here is my whole forum post",
                author_display_name="someone",
                decision_reason="looked answerable",
                response_content="try this",
                confidence_score=0.9,
                tokens_used=500,
                response_time_ms=1200,
                responded=True,
                created_at=STALE,
            )
        )
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        response = (
            await db_session.execute(select(ForumAgentResponse))
        ).scalar_one()
        assert response.post_title == ""
        assert response.post_content == ""
        assert response.decision_reason == ""
        assert response.response_content == ""
        assert purged_at(response.content_purged_at) == NOW
        # Evaluation metrics survive.
        assert response.confidence_score == 0.9
        assert response.tokens_used == 500
        assert response.responded is True


class TestModerationActions:
    def _action(self, source: str, created_at: datetime) -> ModerationAction:
        return ModerationAction(
            guild_id="111",
            target_user_id="333",
            target_username="someone",
            action_type="timeout",
            reason="timed out for repeatedly posting scam links",
            source=source,
            ai_context_summary="the AI's narrative of the exchange",
            created_at=created_at,
        )

    async def test_drops_the_ai_narrative_of_the_exchange(self, db_session):
        db_session.add(self._action("ai", STALE))
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        action = (await db_session.execute(select(ModerationAction))).scalar_one()
        assert action.ai_context_summary is None
        # The action itself is the moderation audit trail — it stays.
        assert action.action_type == "timeout"
        assert action.target_user_id == "333"
        assert purged_at(action.content_purged_at) == NOW

    @pytest.mark.parametrize("source", ["ai", "handler", "manual", "audit_log"])
    async def test_reason_survives_whoever_wrote_it(self, db_session, source):
        # A reason justifies an action we took and is already published in the
        # mod log and the target's DM. It is not a chat message.
        db_session.add(self._action(source, STALE))
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        action = (await db_session.execute(select(ModerationAction))).scalar_one()
        assert action.reason == "timed out for repeatedly posting scam links"


class TestHandlerRuns:
    async def _run(self, session, fired_at: datetime, context: dict) -> HandlerRun:
        handler = ChannelHandler(
            guild_id="111",
            channel_id="222",
            name=f"handler-{uuid4().hex[:8]}",
            description="a test handler",
            trigger_type="message",
            script="pass",
            created_by="333",
        )
        session.add(handler)
        await session.flush()
        run = HandlerRun(
            handler_id=handler.id,
            fired_at=fired_at,
            trigger_context=context,
            handler_kind="channel",
            outcome="ok",
            messages_sent=1,
            duration_ms=42,
        )
        session.add(run)
        await session.flush()
        return run

    async def test_redacts_content_in_trigger_context(self, db_session):
        await self._run(
            db_session,
            STALE,
            {
                "trigger_type": "message",
                "message_content": "what someone said",
                "author_id": "333",
            },
        )

        await run_retention_sweep(db_session, now=NOW)

        run = (await db_session.execute(select(HandlerRun))).scalar_one()
        assert run.trigger_context == {
            "trigger_type": "message",
            "message_content": MESSAGE_CONTENT_PLACEHOLDER,
            "author_id": "333",
        }
        assert purged_at(run.content_purged_at) == NOW
        assert run.messages_sent == 1
        assert run.duration_ms == 42

    async def test_leaves_fresh_runs_alone(self, db_session):
        context = {"trigger_type": "message", "message_content": "recent"}
        await self._run(db_session, FRESH, context)

        await run_retention_sweep(db_session, now=NOW)

        run = (await db_session.execute(select(HandlerRun))).scalar_one()
        assert run.trigger_context == context
        assert run.content_purged_at is None

    async def test_clears_the_script_error_message(self, db_session):
        # A script that trips over the message it is reacting to puts that text
        # into its exception message, which lands in `error` — derived text the
        # sweep owns, exactly like an agent's output.
        run = await self._run(db_session, STALE, {"trigger_type": "message"})
        run.outcome = "error"
        run.error = "runtime: ValueError: what someone said"
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        run = (await db_session.execute(select(HandlerRun))).scalar_one()
        assert run.error is None
        # The row still says the fire failed, and how much it spent doing it.
        assert run.outcome == "error"
        assert run.messages_sent == 1

    @pytest.mark.parametrize("outcome", ["error", "cap_exceeded"])
    async def test_clears_every_script_authored_error(self, db_session, outcome):
        run = await self._run(db_session, STALE, {"trigger_type": "message"})
        run.outcome = outcome
        run.error = "runtime: ValueError: what someone said"
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        run = (await db_session.execute(select(HandlerRun))).scalar_one()
        assert run.error is None

    @pytest.mark.parametrize("outcome", ["rearmed", "skipped"])
    async def test_keeps_a_host_authored_explanation(self, db_session, outcome):
        # The sweep's re-arm note and the skipped-retry note are written by us,
        # never by a script, so they cannot quote a member. Clearing them would
        # lose the only record of why a chain stalled, for no retention gain.
        explanation = "schedule chain had stopped firing; re-armed"
        run = await self._run(db_session, STALE, {"trigger_type": "sweep"})
        run.outcome = outcome
        run.error = explanation
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        run = (await db_session.execute(select(HandlerRun))).scalar_one()
        assert run.error == explanation
        assert purged_at(run.content_purged_at) == NOW

    async def test_leaves_a_fresh_error_readable(self, db_session):
        run = await self._run(db_session, FRESH, {"trigger_type": "message"})
        run.error = "runtime: ValueError: what someone said"
        await db_session.flush()

        await run_retention_sweep(db_session, now=NOW)

        run = (await db_session.execute(select(HandlerRun))).scalar_one()
        assert run.error == "runtime: ValueError: what someone said"

    async def test_handles_an_empty_context(self, db_session):
        await self._run(db_session, STALE, {})

        await run_retention_sweep(db_session, now=NOW)

        run = (await db_session.execute(select(HandlerRun))).scalar_one()
        assert run.trigger_context == {}
        assert purged_at(run.content_purged_at) == NOW


class TestChatAgentMemoryIsExempt:
    """The bot's own three-layer memory is deliberately outside the sweep.

    The blob and its revision history are prose the agent wrote about itself,
    not message content it read; the notes are deleted by the nightly dream
    long before the 48-hour window elapses. Naming all three tables here means
    a future scrubber cannot be added without a test author noticing.
    """

    EXEMPT_TABLES = (
        "chat_agent_guild_memory",
        "chat_agent_memory_notes",
        "chat_agent_memory_revisions",
    )

    @pytest.mark.parametrize("table_name", EXEMPT_TABLES)
    def test_table_has_no_scrubber(self, table_name):
        assert table_name not in SCRUBBERS

    def test_exempt_tables_carry_no_purge_marker(self):
        for model in (
            ChatAgentGuildMemory,
            ChatAgentMemoryNote,
            ChatAgentMemoryRevision,
        ):
            assert not hasattr(model, "content_purged_at")

    async def test_sweep_leaves_stale_memory_untouched(self, db_session):
        db_session.add(
            ChatAgentGuildMemory(
                guild_id="111",
                content="## Who's here\nkai (id 7) is deep in embedded rust.",
                revision=4,
                last_dream_at=STALE,
                notes_consumed=12,
                model_name="anthropic/claude-opus-4",
                created_at=STALE,
                updated_at=STALE,
            )
        )
        db_session.add(
            ChatAgentMemoryNote(
                guild_id="111",
                channel_id="222",
                channel_name="dev-help",
                content="alice (id 1) got soft shadows working and was giddy.",
                engagement_id=uuid4(),
                created_at=STALE,
                updated_at=STALE,
            )
        )
        db_session.add(
            ChatAgentMemoryRevision(
                guild_id="111",
                content="## Who's here\nkai (id 7) builds firmware.",
                revision=3,
                notes_consumed=8,
                model_name="anthropic/claude-opus-4",
                created_at=STALE,
                updated_at=STALE,
            )
        )
        await db_session.flush()

        result = await run_retention_sweep(db_session, now=NOW)

        blob = (await db_session.execute(select(ChatAgentGuildMemory))).scalar_one()
        note = (await db_session.execute(select(ChatAgentMemoryNote))).scalar_one()
        revision = (
            await db_session.execute(select(ChatAgentMemoryRevision))
        ).scalar_one()
        assert blob.content.startswith("## Who's here")
        assert note.content.startswith("alice (id 1)")
        assert revision.content.startswith("## Who's here")
        assert result.total == 0


class TestSweepBehaviour:
    async def test_is_idempotent(self, db_session):
        db_session.add(_help_conversation(STALE))
        engagement = await _engagement(db_session, STALE)
        await _turn(db_session, engagement, STALE)
        await db_session.flush()

        first = await run_retention_sweep(db_session, now=NOW)
        second = await run_retention_sweep(db_session, now=NOW + timedelta(hours=1))

        assert first.total > 0
        assert second.total == 0

    async def test_reports_the_cutoff(self, db_session):
        result = await run_retention_sweep(db_session, now=NOW)
        assert result.cutoff == NOW - CONTENT_RETENTION_WINDOW

    async def test_empty_database_is_a_no_op(self, db_session):
        result = await run_retention_sweep(db_session, now=NOW)
        assert result.total == 0
        assert "nothing due" in str(result)


REPO_ROOT = Path(__file__).resolve().parents[2]
RETENTION_DOC = REPO_ROOT / "docs" / "data-retention.md"
EXTENSION_CATALOG = REPO_ROOT / "smarter_dev" / "extensions" / "catalog"

CHAT_MEMORY_TABLES = (
    "chat_agent_guild_memory",
    "chat_agent_memory_revisions",
    "chat_agent_memory_notes",
)
DERIVED_TEXT_COLUMNS = (
    "agent_output",
    "summary",
    "ai_context_summary",
    "provider_body",
    "bot_response",
    "response_content",
    "decision_reason",
)


def states_hours(text: str, hours: int) -> bool:
    """Whether ``text`` states a duration of ``hours`` in either spelling."""
    return f"{hours} hour" in text or f"{hours}-hour" in text


@pytest.fixture(scope="module")
def retention_doc() -> str:
    return RETENTION_DOC.read_text()


@pytest.fixture(scope="module")
def module_docstring() -> str:
    return retention.__doc__ or ""


class TestDocumentedBehaviour:
    """The doc and the module docstring are the privileged-intent justification.

    Every assertion here reads its expected value out of the code, so a
    constant that moves without the prose moving with it fails the suite.
    """

    def test_documents_every_scrubbed_table(self, retention_doc):
        for table in SCRUBBERS:
            assert f"`{table}`" in retention_doc

    def test_documents_the_derived_text_the_sweep_still_owns(self, retention_doc):
        for column in DERIVED_TEXT_COLUMNS:
            assert f"`{column}`" in retention_doc

    def test_documents_the_tables_that_keep_their_text(self, retention_doc):
        for table in (*CHAT_MEMORY_TABLES, "proactive_agent_histories"):
            assert f"`{table}`" in retention_doc

    def test_states_the_placeholder_written_in_place_of_message_text(
        self, retention_doc
    ):
        assert MESSAGE_CONTENT_PLACEHOLDER in retention_doc
        assert "write time" in retention_doc

    def test_states_the_retention_window(self, retention_doc):
        assert states_hours(
            retention_doc, int(CONTENT_RETENTION_WINDOW.total_seconds() // 3600)
        )

    def test_states_how_long_chat_working_history_keeps_verbatim_text(
        self, retention_doc
    ):
        assert states_hours(retention_doc, HISTORY_TTL_SECONDS // 3600)
        assert f"{KEEP_RECENT_CHARS:,}" in retention_doc

    def test_states_how_much_proactive_history_stays_verbatim(self, retention_doc):
        assert f"{COMPACTION_KEEP_MESSAGES} messages" in retention_doc

    def test_states_that_the_proactive_streams_are_trimmed_by_age(self, retention_doc):
        assert "wake stream" in retention_doc
        assert "shadow stream" in retention_doc

    def test_states_that_moderation_auditing_uses_the_activity_audit_log(
        self, retention_doc
    ):
        assert "activity channel" in retention_doc
        assert "audit log" in retention_doc

    def test_states_that_prefix_commands_are_prohibited(self, retention_doc):
        assert "prefix command" in retention_doc
        assert "prohibited" in retention_doc

    def test_names_the_extensions_the_prohibition_removed(self, retention_doc):
        assert "`!sus`" in retention_doc
        assert "`disboard-bumping`" in retention_doc

    def test_the_named_removals_match_the_shipped_catalog(self):
        assert not (EXTENSION_CATALOG / "sus").exists()
        assert (EXTENSION_CATALOG / "disboard_bumping").exists()
        assert not (EXTENSION_CATALOG / "disboard_bumping" / "bump_commands.monty").exists()

    def test_module_docstring_states_the_write_time_placeholder(
        self, module_docstring
    ):
        assert MESSAGE_CONTENT_PLACEHOLDER in module_docstring
        assert "write time" in module_docstring

    def test_module_docstring_names_the_derived_text_the_sweep_owns(
        self, module_docstring
    ):
        for column in DERIVED_TEXT_COLUMNS:
            assert f"``{column}``" in module_docstring

    def test_module_docstring_keeps_the_memory_exemption(self, module_docstring):
        for table in CHAT_MEMORY_TABLES:
            assert f"``{table}``" in module_docstring
