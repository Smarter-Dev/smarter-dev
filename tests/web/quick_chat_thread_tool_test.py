"""The agent's ``start_new_thread`` tool, and the rules that keep it rare.

The tool is the first thing that draws a boundary, so these tests pin both the
boundary it writes and the three structural guards around it: it does not exist
where there is nothing to break away from, it does not exist for a sub-agent,
and it cannot be called twice in one turn. The last test is the point of the
whole feature — after the line is drawn, the next turn's history starts at it.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pydantic_ai
import pytest
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.test import TestModel
from skrift.db.models.user import User
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.shared.model_catalog import get_model
from smarter_dev.web.chat import jobs
from smarter_dev.web.chat.policy import policy_for
from smarter_dev.web.chat.runtime import encode_model_messages
from smarter_dev.web.chat.subagents import QUICK_THREAD_GUIDANCE
from smarter_dev.web.chat.subagents import effective_system_prompt
from smarter_dev.web.chat.threads import THREAD_ALREADY_STARTED_RESULT
from smarter_dev.web.chat.threads import THREAD_STARTED_RESULT
from smarter_dev.web.chat.threads import derive_thread_title
from smarter_dev.web.chat.threads import open_thread
from smarter_dev.web.chat.toolsets import ExecutionCounters
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatMessage
from smarter_dev.web.models import WebChatRuntimeEvent
from smarter_dev.web.models import WebChatThread
from smarter_dev.web.models import WebChatTurn

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_JOBS_SOURCE = (_PROJECT_ROOT / "smarter_dev" / "web" / "chat" / "jobs.py").read_text()
_CHAT_JS = _PROJECT_ROOT / "themes" / "smarterdev" / "static" / "js" / "chat.js"

MODEL_KEY = "gpt-5-6-luna"


def _chat_js_function_source(name: str) -> str:
    """One named function out of chat.js, from its ``function`` line to its brace.

    Every function in the theme's chat script lives at one indent inside the
    file's IIFE, so its closing brace is the first line that is exactly two
    spaces and a brace.
    """
    source = _CHAT_JS.read_text()
    start = source.index(f"\n  function {name}(") + 1
    collected: list[str] = []
    for line in source[start:].splitlines():
        collected.append(line)
        if line == "  }":
            return "\n".join(collected)
    raise AssertionError(f"chat.js function {name} is not closed at the expected indent")


async def _seed_user(db_session) -> User:
    """Skrift's User table is not in this project's metadata; create it here."""
    connection = await db_session.connection()
    await connection.run_sync(
        lambda sync_connection: User.metadata.create_all(sync_connection)
    )
    user = User(email=f"{uuid4().hex}@example.test", name="Chat User", is_active=True)
    db_session.add(user)
    await db_session.commit()
    return user


async def _seed_conversation(
    db_session, user, *, chat_mode: str = "quick"
) -> WebChatConversation:
    conversation = WebChatConversation(
        owner_user_id=user.id,
        intelligence_mode="efficient",
        selected_model_key=MODEL_KEY,
        reasoning_level="medium",
        title="Quick chat",
        title_is_custom=True,
        status="idle",
        chat_mode=chat_mode,
    )
    db_session.add(conversation)
    await db_session.commit()
    return conversation


def _exchange_payload(prompt: str, reply: str) -> list[dict]:
    return encode_model_messages(
        [
            ModelRequest(parts=[UserPromptPart(content=prompt)]),
            ModelResponse(parts=[TextPart(content=reply)]),
        ]
    )


async def _seed_exchanges(
    db_session, conversation: WebChatConversation, *, count: int
) -> list[WebChatTurn]:
    """``count`` finished turns: user at 2n-1, assistant at 2n, oldest first."""
    turns = []
    for ordinal in range(1, count + 1):
        response_sequence = ordinal * 2
        turn = WebChatTurn(
            conversation_id=conversation.id,
            sequence=ordinal,
            submission_key=f"submission-{ordinal}",
            response_version_group=uuid4(),
            response_sequence=response_sequence,
            model_key=MODEL_KEY,
            status="complete",
        )
        db_session.add(turn)
        await db_session.flush()
        db_session.add(
            WebChatMessage(
                conversation_id=conversation.id,
                turn_id=turn.id,
                sequence=response_sequence - 1,
                role="user",
                content=f"prompt {ordinal}",
            )
        )
        db_session.add(
            WebChatMessage(
                conversation_id=conversation.id,
                turn_id=turn.id,
                sequence=response_sequence,
                role="assistant",
                content=f"reply {ordinal}",
                model_message=_exchange_payload(
                    f"prompt {ordinal}", f"reply {ordinal}"
                ),
            )
        )
        turns.append(turn)
    conversation.next_sequence = count * 2 + 1
    await db_session.commit()
    return turns


async def _seed_running_turn(
    db_session, conversation: WebChatConversation, *, ordinal: int
) -> WebChatTurn:
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=ordinal,
        submission_key=f"submission-{ordinal}",
        response_version_group=uuid4(),
        response_sequence=ordinal * 2,
        model_key=MODEL_KEY,
        status="running",
    )
    db_session.add(turn)
    await db_session.commit()
    return turn


@pytest.fixture
def jobs_session(db_session, monkeypatch):
    """Point every ``get_db_session_context`` inside jobs at the test session."""

    @asynccontextmanager
    async def fake_session_context():
        yield db_session

    monkeypatch.setattr(jobs, "get_db_session_context", fake_session_context)
    return db_session


@pytest.fixture
def published(monkeypatch) -> list[tuple[str, dict]]:
    """Every notification the runtime publishes, in order."""
    sent: list[tuple[str, dict]] = []

    async def record(user_id, event_type, **payload):
        sent.append((event_type, payload))

    monkeypatch.setattr(jobs, "notify_chat_user", record)
    return sent


@pytest.fixture
def spend_allows(monkeypatch):
    """A spend decision that neither cuts the turn off nor warns it."""

    async def decision(**_kwargs):
        return SimpleNamespace(hard_cutoff=False, in_overage=False)

    monkeypatch.setattr(jobs, "_tool_state", decision)
    return decision


_SOME_HISTORY = [ModelResponse(parts=[TextPart(content="an earlier reply")])]


async def _root_agent(
    monkeypatch,
    *,
    conversation: WebChatConversation,
    turn: WebChatTurn,
    history: list,
    user_message_content: str = "How do I mock a socket in pytest?",
    counters: ExecutionCounters | None = None,
):
    """Build the root agent without letting it call a provider."""
    built = []
    build_agent = pydantic_ai.Agent

    def record_agent(*args, **kwargs):
        agent = build_agent(*args, **kwargs)
        built.append(agent)
        return agent

    async def skip_the_run(coroutine, **_kwargs):
        coroutine.close()
        return SimpleNamespace(output="", new_messages=lambda: [])

    monkeypatch.setattr(pydantic_ai, "Agent", record_agent)
    monkeypatch.setattr(jobs, "SpendMeteredModel", lambda inner, _metering: inner)
    monkeypatch.setattr(jobs, "build_model_for", lambda _model: TestModel())
    monkeypatch.setattr(jobs, "run_cancellable", skip_the_run)
    await jobs._build_root_agent(
        model=get_model(MODEL_KEY),
        reasoning=None,
        prompt=user_message_content,
        history=history,
        policy=policy_for(conversation.intelligence_mode),
        counters=counters or ExecutionCounters(),
        turn=turn,
        conversation=conversation,
        owner_id=conversation.owner_user_id,
        tier="standard",
        settings=SimpleNamespace(
            summarizer_model_key=MODEL_KEY,
            summarizer_fallback_model_key=MODEL_KEY,
        ),
        draft_message_id=uuid4(),
        user_message_content=user_message_content,
    )
    return built[-1]


def _tool_names(agent) -> list[str]:
    return list(agent._function_toolset.tools)


def _thread_tool(agent):
    return agent._function_toolset.tools["start_new_thread"].function


def _function_source(name: str) -> str:
    for node in ast.walk(ast.parse(_JOBS_SOURCE)):
        if (
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name == name
        ):
            return ast.get_source_segment(_JOBS_SOURCE, node)
    raise AssertionError(f"{name} is not defined in jobs.py")


async def _thread_count(db_session, conversation: WebChatConversation) -> int:
    return await db_session.scalar(
        select(func.count())
        .select_from(WebChatThread)
        .where(WebChatThread.conversation_id == conversation.id)
    )


class TestQuickThreadGuidance:
    def test_the_root_agent_is_told_the_bar(self):
        prompt = effective_system_prompt(child=False, quick_thread=True)

        assert QUICK_THREAD_GUIDANCE in prompt

    def test_a_standard_conversation_is_not(self):
        prompt = effective_system_prompt(child=False, quick_thread=False)

        assert QUICK_THREAD_GUIDANCE not in prompt
        assert "start_new_thread" not in prompt

    def test_a_child_never_sees_it(self):
        prompt = effective_system_prompt(child=True, quick_thread=True)

        assert QUICK_THREAD_GUIDANCE not in prompt

    def test_the_guidance_names_the_tool_and_refuses_the_unsure_case(self):
        """The bar itself lives in this wording; the code only enforces shape."""
        assert "start_new_thread(reason)" in QUICK_THREAD_GUIDANCE
        assert "unsure" in QUICK_THREAD_GUIDANCE
        assert "follow-up" in QUICK_THREAD_GUIDANCE


class TestToolAvailability:
    async def test_it_is_registered_in_a_quick_chat_with_history(
        self, db_session, jobs_session, monkeypatch
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        turn = await _seed_running_turn(db_session, conversation, ordinal=2)

        agent = await _root_agent(
            monkeypatch, conversation=conversation, turn=turn, history=_SOME_HISTORY
        )

        assert "start_new_thread" in _tool_names(agent)

    async def test_a_standard_conversation_never_has_it(
        self, db_session, jobs_session, monkeypatch
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="standard")
        turn = await _seed_running_turn(db_session, conversation, ordinal=2)

        agent = await _root_agent(
            monkeypatch, conversation=conversation, turn=turn, history=_SOME_HISTORY
        )

        assert "start_new_thread" not in _tool_names(agent)

    async def test_an_empty_thread_has_nothing_to_break_away_from(
        self, db_session, jobs_session, monkeypatch
    ):
        """First message of a Quick chat, and first message after a boundary."""
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        turn = await _seed_running_turn(db_session, conversation, ordinal=1)

        agent = await _root_agent(
            monkeypatch, conversation=conversation, turn=turn, history=[]
        )

        assert "start_new_thread" not in _tool_names(agent)

    def test_a_sub_agent_cannot_reach_it(self):
        """A child rewriting the conversation's thread structure would be a bug."""
        child_handler = _function_source("run_chat_subagent")

        assert "effective_system_prompt(child=True)" in child_handler
        assert "start_new_thread" not in child_handler


class TestDrawingTheBoundary:
    async def test_it_opens_a_thread_in_front_of_the_message_being_answered(
        self, db_session, jobs_session, monkeypatch, published, spend_allows
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=2)
        turn = await _seed_running_turn(db_session, conversation, ordinal=3)
        agent = await _root_agent(
            monkeypatch,
            conversation=conversation,
            turn=turn,
            history=_SOME_HISTORY,
            user_message_content="Now, how do I price a Postgres upgrade?",
        )

        result = await _thread_tool(agent)(None, "moved from pytest mocks to pricing")

        thread = await db_session.scalar(
            select(WebChatThread).where(
                WebChatThread.conversation_id == conversation.id
            )
        )
        assert thread is not None
        assert thread.start_sequence == turn.response_sequence - 1 == 5
        assert thread.origin == "agent"
        assert thread.reason == "moved from pytest mocks to pricing"
        assert thread.title == derive_thread_title(
            "Now, how do I price a Postgres upgrade?"
        )
        assert THREAD_STARTED_RESULT in result

    async def test_it_announces_the_divider_live_and_durably(
        self, db_session, jobs_session, monkeypatch, published, spend_allows
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=2)
        turn = await _seed_running_turn(db_session, conversation, ordinal=3)
        agent = await _root_agent(
            monkeypatch,
            conversation=conversation,
            turn=turn,
            history=_SOME_HISTORY,
            user_message_content="Now, how do I price a Postgres upgrade?",
        )

        await _thread_tool(agent)(None, "pytest mocks → Postgres pricing")

        thread = await db_session.scalar(
            select(WebChatThread).where(
                WebChatThread.conversation_id == conversation.id
            )
        )
        announced = [
            payload for event, payload in published if event == "chat_thread_started"
        ]
        assert len(announced) == 1
        assert announced[0]["thread_id"] == str(thread.id)
        assert announced[0]["start_sequence"] == 5
        assert announced[0]["title"] == thread.title
        recorded = await db_session.scalar(
            select(WebChatRuntimeEvent).where(
                WebChatRuntimeEvent.turn_id == turn.id,
                WebChatRuntimeEvent.event_type == "chat_thread_started",
            )
        )
        assert recorded is not None
        assert recorded.payload["thread_id"] == str(thread.id)

    async def test_a_second_call_in_the_same_turn_changes_nothing(
        self, db_session, jobs_session, monkeypatch, published, spend_allows
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=2)
        turn = await _seed_running_turn(db_session, conversation, ordinal=3)
        agent = await _root_agent(
            monkeypatch, conversation=conversation, turn=turn, history=_SOME_HISTORY
        )
        start_new_thread = _thread_tool(agent)
        await start_new_thread(None, "first break")

        result = await start_new_thread(None, "second break")

        assert result == THREAD_ALREADY_STARTED_RESULT
        assert await _thread_count(db_session, conversation) == 1

    @pytest.mark.parametrize("reason", ["", "   ", "\n\t "])
    async def test_a_missing_reason_is_refused(
        self, db_session, jobs_session, monkeypatch, published, spend_allows, reason
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=2)
        turn = await _seed_running_turn(db_session, conversation, ordinal=3)
        agent = await _root_agent(
            monkeypatch, conversation=conversation, turn=turn, history=_SOME_HISTORY
        )

        result = await _thread_tool(agent)(None, reason)

        assert result == "error: reason is required"
        assert await _thread_count(db_session, conversation) == 0

    async def test_an_oversized_reason_is_refused(
        self, db_session, jobs_session, monkeypatch, published, spend_allows
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=2)
        turn = await _seed_running_turn(db_session, conversation, ordinal=3)
        agent = await _root_agent(
            monkeypatch, conversation=conversation, turn=turn, history=_SOME_HISTORY
        )

        result = await _thread_tool(agent)(None, "x" * 501)

        assert result.startswith("error:")
        assert await _thread_count(db_session, conversation) == 0

    async def test_a_refused_reason_does_not_spend_the_one_call(
        self, db_session, jobs_session, monkeypatch, published, spend_allows
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=2)
        turn = await _seed_running_turn(db_session, conversation, ordinal=3)
        agent = await _root_agent(
            monkeypatch, conversation=conversation, turn=turn, history=_SOME_HISTORY
        )
        start_new_thread = _thread_tool(agent)
        await start_new_thread(None, " ")

        result = await start_new_thread(None, "pytest mocks → Postgres pricing")

        assert THREAD_STARTED_RESULT in result
        assert await _thread_count(db_session, conversation) == 1

    async def test_a_worker_retry_reports_success(
        self, db_session, jobs_session, monkeypatch, published, spend_allows
    ):
        """The line is already where it should be, so this is not an error."""
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=2)
        turn = await _seed_running_turn(db_session, conversation, ordinal=3)
        already = await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=turn.response_sequence - 1,
            title="Drawn by the attempt that died",
            origin="agent",
            reason="pytest mocks → Postgres pricing",
        )
        agent = await _root_agent(
            monkeypatch, conversation=conversation, turn=turn, history=_SOME_HISTORY
        )

        result = await _thread_tool(agent)(None, "pytest mocks → Postgres pricing")

        assert THREAD_STARTED_RESULT in result
        assert await _thread_count(db_session, conversation) == 1
        announced = [
            payload for event, payload in published if event == "chat_thread_started"
        ]
        assert announced[0]["thread_id"] == str(already.id)

    async def test_a_hard_cutoff_draws_no_line(
        self, db_session, jobs_session, monkeypatch, published
    ):
        async def cut_off(**_kwargs):
            return SimpleNamespace(hard_cutoff=True, in_overage=True)

        monkeypatch.setattr(jobs, "_tool_state", cut_off)
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=2)
        turn = await _seed_running_turn(db_session, conversation, ordinal=3)
        agent = await _root_agent(
            monkeypatch, conversation=conversation, turn=turn, history=_SOME_HISTORY
        )

        result = await _thread_tool(agent)(None, "pytest mocks → Postgres pricing")

        assert result == jobs.USAGE_LIMIT_RESULT
        assert await _thread_count(db_session, conversation) == 0


class TestOutsideTheSharedToolBudget:
    """A busy turn is exactly the turn that most needs the line drawn."""

    async def test_an_exhausted_tool_allowance_still_draws_the_line(
        self, db_session, jobs_session, monkeypatch, published, spend_allows
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=2)
        turn = await _seed_running_turn(db_session, conversation, ordinal=3)
        policy = policy_for(conversation.intelligence_mode)
        spent = ExecutionCounters(
            tool_calls=policy.max_tool_calls,
            searches=policy.max_searches,
            subagent_attempts=0,
        )
        agent = await _root_agent(
            monkeypatch,
            conversation=conversation,
            turn=turn,
            history=_SOME_HISTORY,
            counters=spent,
        )

        result = await _thread_tool(agent)(None, "pytest mocks → Postgres pricing")

        assert THREAD_STARTED_RESULT in result
        assert await _thread_count(db_session, conversation) == 1
        assert spent.tool_calls == policy.max_tool_calls

    def test_the_tool_body_does_not_consult_the_shared_budget(self):
        """Read as source because a passing budget test can pass by luck."""
        body = _JOBS_SOURCE.split("async def start_new_thread(", 1)[1].split(
            "\n    agent.tool(", 1
        )[0]

        assert 'accept("tool"' not in body
        assert "accept(" not in body


class TestTheNextTurnStartsAtTheLine:
    async def test_history_after_a_boundary_is_only_the_new_thread(
        self, db_session, jobs_session, monkeypatch, published, spend_allows
    ):
        """The end-to-end point of the tool, built from real rows."""
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        turns = await _seed_exchanges(db_session, conversation, count=3)
        breaking_turn = turns[-1]
        agent = await _root_agent(
            monkeypatch,
            conversation=conversation,
            turn=breaking_turn,
            history=_SOME_HISTORY,
            user_message_content="prompt 3",
        )
        await _thread_tool(agent)(None, "prompt 2 was about something else")

        next_turn = await _seed_running_turn(db_session, conversation, ordinal=4)
        branch = await jobs._structured_history(conversation, next_turn)

        assert [row.sequence for row in branch.rows] == [6]
        assert [
            part.content
            for message in branch.messages
            if isinstance(message, ModelResponse)
            for part in message.parts
            if isinstance(part, TextPart)
        ] == ["reply 3"]


class TestTheTurnThatDrewTheLineStillAnswers:
    """The boundary is drawn mid-turn, so the reply write outlives it.

    Only a full worker run reaches the compare-and-swap that guards that write,
    so the wiring is read as source: it re-reads the branch at the floor the
    history was taken at, and it caches the context the committed floor allows.
    Read at the committed floor instead, the turn would fingerprint an empty
    window, throw the finished reply away, and answer again on no history.
    """

    def test_the_reply_write_reads_the_branch_at_the_generation_floor(self):
        body = _JOBS_SOURCE.split("async def run_chat_turn(", 1)[1]

        assert "floor=branch.floor," in body

    def test_the_cached_context_is_floored_by_the_committed_boundary(self):
        body = _JOBS_SOURCE.split("async def run_chat_turn(", 1)[1]

        assert "_cached_context_after_turn(" in body
        assert "committed_floor=committed_floor," in body


class TestLiveDivider:
    def test_the_notification_handler_inserts_a_divider(self):
        source = _CHAT_JS.read_text()

        assert "chat_thread_started" in source
        assert "insertLiveThreadBreak" in source

    def test_the_optimistic_user_article_is_named_once_the_turn_exists(self):
        """The divider anchors on the turn, so the send path must name the turn.

        The user's own bubble goes in before the POST returns, when there is no
        turn id to give it. Naming it on the response is what lets the divider
        for an in-flight turn find the message it belongs above.
        """
        body = _chat_js_function_source("sendMessage")

        assert "user.dataset.turnId" in body

    @pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
    def test_the_divider_lands_above_the_message_being_answered(self, tmp_path):
        """Run the real functions over a stub DOM; selectors are the whole risk."""
        harness = (Path(__file__).parent / "js" / "live_thread_break_harness.js").read_text()
        functions = "\n\n".join(
            _chat_js_function_source(name)
            for name in ("threadBreakElement", "liveThreadBreakAnchor", "insertLiveThreadBreak")
        )
        script = tmp_path / "live_thread_break.js"
        script.write_text(harness.replace("// <CHAT_JS_FUNCTIONS>", functions))

        result = subprocess.run(
            ["node", str(script)], capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_live_insert_reuses_the_reconcile_builder(self):
        """Two builders is how the live divider and the reconciled one diverge."""
        source = _CHAT_JS.read_text()
        body = source.split("function insertLiveThreadBreak(", 1)[1].split(
            "\n  function ", 1
        )[0]

        assert "threadBreakElement(" in body
        assert "data-thread-id" in body

    def test_the_live_insert_stays_es5(self):
        source = _CHAT_JS.read_text()
        body = source.split("function insertLiveThreadBreak(", 1)[1].split(
            "\n  function ", 1
        )[0]
        code = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("//")
        )

        assert "=>" not in code
        assert "`" not in code
        assert "let " not in code
        assert "const " not in code
