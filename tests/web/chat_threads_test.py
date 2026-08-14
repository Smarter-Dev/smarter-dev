"""Thread boundaries inside a Quick chat, and the history they scope.

A Quick chat is one perpetual conversation cut into threads; the agent only ever
reads the current thread. These tests pin both halves of that: the thread rules
themselves (``smarter_dev.web.chat.threads``) and the runtime's use of them
(``_structured_history`` in ``smarter_dev.web.chat.jobs``).

The regression half matters as much as the feature half — a conversation left at
``chat_mode='standard'`` must come back with exactly the history it came back
with before threads existed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import pytest
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import UserPromptPart
from skrift.db.models.user import User
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from smarter_dev.web.chat import jobs
from smarter_dev.web.chat.compaction import DurableCompactionResult
from smarter_dev.web.chat.compaction import version_fingerprint
from smarter_dev.web.chat.conversations import derive_title
from smarter_dev.web.chat.runtime import encode_model_messages
from smarter_dev.web.chat.threads import current_thread
from smarter_dev.web.chat.threads import derive_thread_title
from smarter_dev.web.chat.threads import history_floor
from smarter_dev.web.chat.threads import open_thread
from smarter_dev.web.chat.threads import thread_breaks
from smarter_dev.web.llm_pricing import ModelPriceRates
from smarter_dev.web.models import WebChatCompaction
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatMessage
from smarter_dev.web.models import WebChatThread
from smarter_dev.web.models import WebChatTurn


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
    db_session, user, *, chat_mode: str = "standard", archived_at=None
) -> WebChatConversation:
    conversation = WebChatConversation(
        owner_user_id=user.id,
        intelligence_mode="efficient",
        selected_model_key="gpt-5-6-luna",
        reasoning_level="medium",
        title="New Chat",
        status="idle",
        chat_mode=chat_mode,
        archived_at=archived_at,
    )
    db_session.add(conversation)
    await db_session.commit()
    return conversation


def _exchange_payload(prompt: str, reply: str) -> list[dict]:
    """One request/response delta, the shape a WebChatMessage row actually holds."""
    return encode_model_messages(
        [
            ModelRequest(parts=[UserPromptPart(content=prompt)]),
            ModelResponse(parts=[TextPart(content=reply)]),
        ]
    )


async def _seed_exchanges(
    db_session, conversation: WebChatConversation, *, count: int
) -> list[WebChatMessage]:
    """``count`` finished turns: user at 2n-1, assistant at 2n, oldest first."""
    assistant_messages = []
    for ordinal in range(1, count + 1):
        response_sequence = ordinal * 2
        turn = WebChatTurn(
            conversation_id=conversation.id,
            sequence=ordinal,
            submission_key=f"submission-{ordinal}",
            response_version_group=uuid4(),
            response_sequence=response_sequence,
            model_key="gpt-5-6-luna",
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
        assistant = WebChatMessage(
            conversation_id=conversation.id,
            turn_id=turn.id,
            sequence=response_sequence,
            role="assistant",
            content=f"reply {ordinal}",
            model_message=_exchange_payload(f"prompt {ordinal}", f"reply {ordinal}"),
        )
        db_session.add(assistant)
        assistant_messages.append(assistant)
    conversation.next_sequence = count + 1
    await db_session.commit()
    return assistant_messages


async def _seed_current_turn(
    db_session, conversation: WebChatConversation, *, ordinal: int
) -> WebChatTurn:
    turn = WebChatTurn(
        conversation_id=conversation.id,
        sequence=ordinal,
        submission_key=f"submission-{ordinal}",
        response_version_group=uuid4(),
        response_sequence=ordinal * 2,
        model_key="gpt-5-6-luna",
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


def _reply_texts(history: list) -> list[str]:
    return [
        part.content
        for message in history
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, TextPart)
    ]


class TestHistoryFloor:
    async def test_standard_conversation_has_no_floor(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)

        assert await history_floor(db_session, conversation=conversation) == 0

    async def test_quick_chat_without_threads_has_no_floor(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")

        assert await history_floor(db_session, conversation=conversation) == 0

    async def test_floor_is_the_current_thread_start(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=1,
            title="First",
            origin="initial",
            reason=None,
        )
        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=9,
            title="Second",
            origin="agent",
            reason="new subject",
        )

        assert await history_floor(db_session, conversation=conversation) == 9

    async def test_a_standard_conversation_ignores_stray_threads(self, db_session):
        """chat_mode is the only switch; a standard chat is never scoped."""
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        db_session.add(
            WebChatThread(
                conversation_id=conversation.id,
                sequence=1,
                start_sequence=9,
                title="Stray",
                origin="agent",
                reason=None,
            )
        )
        await db_session.commit()

        assert await history_floor(db_session, conversation=conversation) == 0


class TestOpenThread:
    async def test_sequences_are_assigned_in_order(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")

        opened = []
        for start_sequence in (1, 5, 11):
            thread = await open_thread(
                db_session,
                conversation_id=conversation.id,
                start_sequence=start_sequence,
                title=f"Thread at {start_sequence}",
                origin="agent",
                reason=None,
            )
            opened.append(thread)

        assert [thread.sequence for thread in opened] == [1, 2, 3]
        assert [thread.start_sequence for thread in opened] == [1, 5, 11]

    async def test_reopening_the_same_start_is_a_no_op(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        first = await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=5,
            title="Only",
            origin="agent",
            reason=None,
        )
        assert first is not None

        repeat = await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=5,
            title="Only",
            origin="agent",
            reason=None,
        )

        assert repeat is None
        thread_count = await db_session.scalar(
            select(func.count())
            .select_from(WebChatThread)
            .where(WebChatThread.conversation_id == conversation.id)
        )
        assert thread_count == 1

    async def test_opening_resets_the_context_meter(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        conversation.current_context_tokens = 4200
        conversation.context_state = [{"kind": "response"}]
        conversation.context_revision = 3
        await db_session.commit()

        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=7,
            title="Fresh",
            origin="evaluator",
            reason="came back after an hour",
        )

        refreshed = await db_session.get(WebChatConversation, conversation.id)
        await db_session.refresh(refreshed)
        assert refreshed.current_context_tokens == 0
        assert refreshed.context_state == []
        assert refreshed.context_revision == 4

    async def test_a_no_op_reopen_leaves_the_meter_alone(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=7,
            title="Fresh",
            origin="agent",
            reason=None,
        )
        refreshed = await db_session.get(WebChatConversation, conversation.id)
        await db_session.refresh(refreshed)
        revision_after_first = refreshed.context_revision

        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=7,
            title="Fresh",
            origin="agent",
            reason=None,
        )

        refreshed = await db_session.get(WebChatConversation, conversation.id)
        await db_session.refresh(refreshed)
        assert refreshed.context_revision == revision_after_first


class TestCurrentThread:
    async def test_none_until_a_thread_exists(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")

        assert await current_thread(db_session, conversation_id=conversation.id) is None

    async def test_the_latest_start_wins(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        for start_sequence in (1, 9, 5):
            await open_thread(
                db_session,
                conversation_id=conversation.id,
                start_sequence=start_sequence,
                title=None,
                origin="agent",
                reason=None,
            )

        latest = await current_thread(db_session, conversation_id=conversation.id)

        assert latest is not None
        assert latest.start_sequence == 9


class TestOneLiveQuickChatPerOwner:
    async def test_a_second_live_quick_chat_is_rejected(self, db_session):
        user = await _seed_user(db_session)
        await _seed_conversation(db_session, user, chat_mode="quick")

        with pytest.raises(IntegrityError):
            await _seed_conversation(db_session, user, chat_mode="quick")

    async def test_an_archived_quick_chat_frees_the_slot(self, db_session):
        user = await _seed_user(db_session)
        await _seed_conversation(
            db_session,
            user,
            chat_mode="quick",
            archived_at=datetime.now(UTC).replace(tzinfo=None),
        )

        live = await _seed_conversation(db_session, user, chat_mode="quick")

        assert live.archived_at is None

    async def test_standard_conversations_are_unlimited(self, db_session):
        user = await _seed_user(db_session)
        await _seed_conversation(db_session, user)
        await _seed_conversation(db_session, user)

        conversation_count = await db_session.scalar(
            select(func.count())
            .select_from(WebChatConversation)
            .where(WebChatConversation.owner_user_id == user.id)
        )
        assert conversation_count == 2


class TestDeriveThreadTitle:
    def test_it_matches_the_conversation_rule(self):
        long_prompt = "word " * 60

        assert derive_thread_title(long_prompt) == derive_title(long_prompt)

    def test_it_truncates_with_an_ellipsis(self):
        long_prompt = "a" * 200

        title = derive_thread_title(long_prompt)

        assert title.endswith("…")
        assert len(title) == 80


class TestStructuredHistoryScoping:
    async def test_a_quick_chat_reads_only_the_current_thread(
        self, db_session, jobs_session
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        await _seed_exchanges(db_session, conversation, count=6)
        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=9,
            title="Second subject",
            origin="agent",
            reason="topic change",
        )
        turn = await _seed_current_turn(db_session, conversation, ordinal=7)

        branch = await jobs._structured_history(conversation, turn)

        assert [row.sequence for row in branch.rows] == [10, 12]
        assert _reply_texts(branch.messages) == ["reply 5", "reply 6"]

    async def test_a_standard_conversation_reads_everything(
        self, db_session, jobs_session
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=6)
        turn = await _seed_current_turn(db_session, conversation, ordinal=7)

        branch = await jobs._structured_history(conversation, turn)

        assert [row.sequence for row in branch.rows] == [2, 4, 6, 8, 10, 12]
        assert _reply_texts(branch.messages) == [f"reply {n}" for n in range(1, 7)]

    async def test_a_compaction_below_the_boundary_is_ignored(
        self, db_session, jobs_session
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        assistants = await _seed_exchanges(db_session, conversation, count=6)
        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=9,
            title="Second subject",
            origin="agent",
            reason="topic change",
        )
        stale_prefix = [row for row in assistants if row.sequence <= 6]
        db_session.add(
            WebChatCompaction(
                conversation_id=conversation.id,
                through_sequence=6,
                summary="the previous subject",
                original_messages=[],
                compacted_messages=encode_model_messages(
                    [ModelResponse(parts=[TextPart(content="stale summary")])]
                ),
                version_fingerprint=version_fingerprint(
                    [
                        {
                            "id": str(row.id),
                            "version_number": row.version_number,
                            "is_active": True,
                        }
                        for row in stale_prefix
                    ]
                ),
                model_key="gpt-5-6-luna",
                status="complete",
                context_revision=1,
            )
        )
        await db_session.commit()
        turn = await _seed_current_turn(db_session, conversation, ordinal=7)

        branch = await jobs._structured_history(conversation, turn)

        assert [row.sequence for row in branch.rows] == [10, 12]
        assert "stale summary" not in _reply_texts(branch.messages)
        assert _reply_texts(branch.messages) == ["reply 5", "reply 6"]

    async def test_a_compaction_above_the_boundary_is_used(
        self, db_session, jobs_session
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        assistants = await _seed_exchanges(db_session, conversation, count=6)
        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=9,
            title="Second subject",
            origin="agent",
            reason="topic change",
        )
        in_thread_prefix = [row for row in assistants if 9 <= row.sequence <= 10]
        db_session.add(
            WebChatCompaction(
                conversation_id=conversation.id,
                through_sequence=10,
                summary="this subject so far",
                original_messages=[],
                compacted_messages=encode_model_messages(
                    [ModelResponse(parts=[TextPart(content="thread summary")])]
                ),
                version_fingerprint=version_fingerprint(
                    [
                        {
                            "id": str(row.id),
                            "version_number": row.version_number,
                            "is_active": True,
                        }
                        for row in in_thread_prefix
                    ]
                ),
                model_key="gpt-5-6-luna",
                status="complete",
                context_revision=1,
            )
        )
        await db_session.commit()
        turn = await _seed_current_turn(db_session, conversation, ordinal=7)

        branch = await jobs._structured_history(conversation, turn)

        assert [row.sequence for row in branch.rows] == [10, 12]
        assert _reply_texts(branch.messages) == ["thread summary", "reply 6"]


class TestActiveMessagesFloor:
    async def test_from_sequence_defaults_to_no_floor(self, db_session, jobs_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=3)

        rows = await jobs._active_messages_before(conversation.id, 8)

        assert [row.sequence for row in rows] == [2, 4, 6]

    async def test_from_sequence_clips_the_older_rows(self, db_session, jobs_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=3)

        rows = await jobs._active_messages_before(conversation.id, 8, from_sequence=5)

        assert [row.sequence for row in rows] == [6]


class TestCurrentBranchFingerprint:
    """The re-read both compare-and-swap points use before they persist.

    ``run_chat_turn`` refuses to write the reply when the branch it generated
    against has moved, and ``_maybe_compact`` refuses to store the fold for the
    same reason. Both compare against the fingerprint ``_structured_history``
    returned, so both re-reads must cover the same window — the rows at or above
    the floor the history was read at, which in a Quick chat is the thread the
    turn started in and everywhere else is the whole conversation.
    """

    async def test_it_matches_structured_history_in_a_quick_chat(
        self, db_session, jobs_session
    ):
        """Pre-boundary rows must not leak into the re-read.

        Without the floor the re-read covers all six replies while the
        fingerprint covers the two in the current thread, so the two can never
        agree and every turn aborts as a branch change.
        """
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        await _seed_exchanges(db_session, conversation, count=6)
        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=9,
            title="Second subject",
            origin="agent",
            reason="topic change",
        )
        turn = await _seed_current_turn(db_session, conversation, ordinal=7)
        branch = await jobs._structured_history(conversation, turn)

        current_branch = await jobs._current_branch_fingerprint(
            db_session,
            conversation_id=conversation.id,
            response_sequence=turn.response_sequence,
            floor=branch.floor,
        )

        assert current_branch == branch.fingerprint

    async def test_it_matches_structured_history_in_a_standard_conversation(
        self, db_session, jobs_session
    ):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user)
        await _seed_exchanges(db_session, conversation, count=6)
        turn = await _seed_current_turn(db_session, conversation, ordinal=7)
        branch = await jobs._structured_history(conversation, turn)

        current_branch = await jobs._current_branch_fingerprint(
            db_session,
            conversation_id=conversation.id,
            response_sequence=turn.response_sequence,
            floor=branch.floor,
        )

        assert current_branch == branch.fingerprint

    async def test_a_boundary_this_turn_drew_itself_still_matches(
        self, db_session, jobs_session
    ):
        """The turn's own ``start_new_thread`` call must not cost it its reply.

        The tool draws the line in front of the message being answered, so the
        committed floor ends the turn above every reply the turn generated
        against. The question the compare-and-swap asks is whether the branch
        changed, not whether the floor moved, so it re-reads at the floor the
        history was taken at.
        """
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        await _seed_exchanges(db_session, conversation, count=3)
        turn = await _seed_current_turn(db_session, conversation, ordinal=4)
        branch = await jobs._structured_history(conversation, turn)
        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=turn.response_sequence - 1,
            title="A new subject",
            origin="agent",
            reason="pytest mocks → Postgres pricing",
        )

        current_branch = await jobs._current_branch_fingerprint(
            db_session,
            conversation_id=conversation.id,
            response_sequence=turn.response_sequence,
            floor=branch.floor,
        )

        assert current_branch == branch.fingerprint

    async def test_a_version_switch_still_changes_the_fingerprint(
        self, db_session, jobs_session
    ):
        """The floor must not blind the check to a real branch change."""
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        assistants = await _seed_exchanges(db_session, conversation, count=6)
        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=9,
            title="Second subject",
            origin="agent",
            reason="topic change",
        )
        turn = await _seed_current_turn(db_session, conversation, ordinal=7)
        branch = await jobs._structured_history(conversation, turn)
        in_thread_reply = next(row for row in assistants if row.sequence == 10)
        in_thread_reply.version_number += 1
        await db_session.commit()

        current_branch = await jobs._current_branch_fingerprint(
            db_session,
            conversation_id=conversation.id,
            response_sequence=turn.response_sequence,
            floor=branch.floor,
        )

        assert current_branch != branch.fingerprint


class TestMaybeCompactFloor:
    async def test_the_compare_and_swap_uses_the_same_floor(
        self, db_session, jobs_session, monkeypatch
    ):
        """The CAS re-read must be thread-scoped or a Quick chat never compacts.

        ``_maybe_compact`` re-reads the assistant rows and refuses to store a
        summary whose branch has moved underneath it. Read without the floor,
        that re-read sees the whole conversation while the fingerprint it is
        compared against covers only the current thread, so the two never agree
        and the fold is silently dropped every time.
        """
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")
        await _seed_exchanges(db_session, conversation, count=6)
        await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=9,
            title="Second subject",
            origin="agent",
            reason="topic change",
        )
        turn = await _seed_current_turn(db_session, conversation, ordinal=7)
        branch = await jobs._structured_history(conversation, turn)
        folded = [ModelResponse(parts=[TextPart(content="folded")])]
        rates = ModelPriceRates(
            input_mtok=Decimal("1"),
            output_mtok=Decimal("2"),
            cache_read_mtok=Decimal("0.1"),
            cache_write_mtok=Decimal("1.2"),
        )

        async def fake_compact_model_history(messages, *, summarize):
            return DurableCompactionResult(
                messages=folded,
                summary="folded summary",
                folded_messages=list(messages),
                fingerprint="",
                changed=True,
            )

        monkeypatch.setattr(jobs, "compaction_model_key", lambda *a, **k: "compactor")
        monkeypatch.setattr(jobs, "get_model", lambda key: SimpleNamespace(key=key))
        monkeypatch.setattr(jobs, "price_rates_for_model", lambda model: rates)
        monkeypatch.setattr(jobs, "should_compact_history", lambda *a, **k: True)
        monkeypatch.setattr(jobs, "compact_model_history", fake_compact_model_history)

        compacted = await jobs._maybe_compact(
            branch=branch,
            conversation=conversation,
            turn=turn,
            owner_id=user.id,
            selected_model=SimpleNamespace(key="gpt-5-6-luna"),
            settings=SimpleNamespace(
                compaction_model_key="compactor",
                compaction_fallback_model_key=None,
            ),
        )

        assert compacted == folded
        stored = await db_session.scalar(
            select(WebChatCompaction).where(
                WebChatCompaction.conversation_id == conversation.id
            )
        )
        assert stored is not None
        assert stored.through_sequence == 12
        assert stored.version_fingerprint == branch.fingerprint


class TestCachedContextAfterTurn:
    """What the conversation caches once the reply is written.

    The cache is what the context meter reads and what the next turn is charged
    for, so it has to hold the thread the conversation is now in — not the one
    the turn started in.
    """

    def _history(self) -> list:
        return [ModelResponse(parts=[TextPart(content="reply 1")])]

    def _delta(self) -> list:
        return [
            ModelRequest(parts=[UserPromptPart(content="prompt 2")]),
            ModelResponse(parts=[TextPart(content="reply 2")]),
        ]

    def test_an_unmoved_floor_keeps_the_history_in_front(self):
        history = self._history()
        delta = self._delta()

        cached = jobs._cached_context_after_turn(
            history=history, delta=delta, generation_floor=0, committed_floor=0
        )

        assert cached == [*history, *delta]

    def test_a_boundary_drawn_mid_turn_caches_only_the_new_thread(self):
        """The history belongs to the thread that just ended."""
        history = self._history()
        delta = self._delta()

        cached = jobs._cached_context_after_turn(
            history=history, delta=delta, generation_floor=0, committed_floor=3
        )

        assert cached == delta

    def test_it_leaves_what_it_is_handed_alone(self):
        history = self._history()
        delta = self._delta()

        jobs._cached_context_after_turn(
            history=history, delta=delta, generation_floor=0, committed_floor=0
        )

        assert len(history) == 1
        assert len(delta) == 2


class TestThreadBreaks:
    """Nothing writes an opening row, so every stored row is a real boundary."""

    def test_the_first_boundary_drawn_still_draws_a_divider(self):
        drawn_first = {
            "id": "thread-1",
            "sequence": 1,
            "start_sequence": 5,
            "title": "Postgres pricing",
            "origin": "agent",
        }

        breaks = thread_breaks([drawn_first])

        assert breaks == {5: drawn_first}

    def test_every_boundary_is_keyed_by_the_message_it_sits_above(self):
        threads = [
            {
                "id": "thread-1",
                "sequence": 1,
                "start_sequence": 5,
                "title": "Postgres pricing",
                "origin": "agent",
            },
            {
                "id": "thread-2",
                "sequence": 2,
                "start_sequence": 11,
                "title": "Deploying the bot",
                "origin": "evaluator",
            },
        ]

        breaks = thread_breaks(threads)

        assert sorted(breaks) == [5, 11]
        assert breaks[11]["title"] == "Deploying the bot"


class TestThreadIdentity:
    async def test_open_thread_returns_a_persisted_row(self, db_session):
        user = await _seed_user(db_session)
        conversation = await _seed_conversation(db_session, user, chat_mode="quick")

        thread = await open_thread(
            db_session,
            conversation_id=conversation.id,
            start_sequence=1,
            title="Opening",
            origin="initial",
            reason=None,
        )

        assert isinstance(thread.id, UUID)
        stored = await db_session.get(WebChatThread, thread.id)
        assert stored.origin == "initial"
        assert stored.title == "Opening"
