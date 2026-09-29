"""Behavior and personality: the two durable blocks beside the memory blob.

The dream edits all three, but the two new blocks are held to a stricter
contract than the blob: an omitted field keeps the block verbatim, an empty or
over-limit revision is refused rather than truncated, and an established
personality changes only with a stated reason. These tests pin that contract
at every layer it passes through: the migration, the upsert, the dream's
validation and persistence, and the real pydantic-ai validator loop.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.models.test import TestModel
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import text

from alembic.migration import MigrationContext
from alembic.operations import Operations
from smarter_dev.web import chat_memory_dream
from smarter_dev.web.chat_memory_dream import DreamContext
from smarter_dev.web.chat_memory_dream import DreamOutcome
from smarter_dev.web.chat_memory_dream import DreamOutput
from smarter_dev.web.chat_memory_dream import build_dream_user_message
from smarter_dev.web.chat_memory_dream import compose_blocks
from smarter_dev.web.chat_memory_dream import get_dream_agent
from smarter_dev.web.chat_memory_dream import run_guild_dream
from smarter_dev.web.crud import create_memory_note
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import record_memory_revision
from smarter_dev.web.crud import upsert_guild_memory_blob
from smarter_dev.web.models import MAX_BEHAVIOR_CHARS
from smarter_dev.web.models import MAX_PERSONALITY_CHARS
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.models import ChatAgentMemoryRevision

REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "main"
    / "versions"
    / "20260929_233000_a3f6d2c8e1b7_split_guild_memory_blocks.py"
)

_GUILD = "123456789012345678"
_CHANNEL = "555000111222333444"
_CUTOFF = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
_DREAMED_AT = _CUTOFF + timedelta(minutes=20)
_YESTERDAY_MIDNIGHT = _CUTOFF - timedelta(days=1)
_YESTERDAY_MORNING = _CUTOFF - timedelta(hours=15)

_MEMORY = "## People & Relationships\nkai (id 7) is deep in embedded rust."
_IDENTITY = "## Identity & Voice\n- I use dry humor.\n- I let others finish."
_BEHAVIOR = "Wait to be asked before explaining a toolchain."
_PERSONALITY = "Dry, warm, quietly delighted by good shader work."
_NOTE = "kai shipped the shader."


@dataclass
class _StubRunResult:
    output: DreamOutput


@dataclass
class _StubDreamAgent:
    output: DreamOutput
    prompts: list[str] = field(default_factory=list)

    async def run(self, user_prompt: str, *, deps: DreamContext) -> _StubRunResult:
        self.prompts.append(user_prompt)
        return _StubRunResult(output=self.output)


def _context(**overrides) -> DreamContext:
    values = {
        "previous_blob": _MEMORY,
        "notes": [_NOTE],
        "previous_behavior": _BEHAVIOR,
        "previous_personality": _PERSONALITY,
    }
    values.update(overrides)
    return DreamContext(**values)


async def _seed(session, *, content=_MEMORY, behavior=_BEHAVIOR, personality=_PERSONALITY):
    await upsert_guild_memory_blob(
        session,
        guild_id=_GUILD,
        content=content,
        behavior=behavior,
        personality=personality,
        notes_consumed=1,
        model_name="stub-model",
        dreamed_at=_YESTERDAY_MIDNIGHT,
    )
    await session.commit()


async def _write_note(session, content: str = _NOTE) -> ChatAgentMemoryNote:
    note = await create_memory_note(
        session,
        guild_id=_GUILD,
        channel_id=_CHANNEL,
        channel_name="dev-help",
        content=content,
        created_at=_YESTERDAY_MORNING,
        day_start=_YESTERDAY_MIDNIGHT,
    )
    await session.commit()
    return note


async def _dream(session, output: DreamOutput) -> tuple:
    agent = _StubDreamAgent(output=output)
    result = await run_guild_dream(
        session,
        guild_id=_GUILD,
        cutoff=_CUTOFF,
        dreamed_at=_DREAMED_AT,
        agent=agent,
    )
    return result, agent


# -- migration -----------------------------------------------------------------


def _revision_module():
    spec = importlib.util.spec_from_file_location("split_blocks_revision", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_adds_empty_blocks_and_keeps_existing_memory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        # The two tables exactly as the previous head left them.
        connection.execute(
            text(
                "CREATE TABLE chat_agent_guild_memory (id CHAR(32) PRIMARY KEY, "
                "guild_id VARCHAR(20) NOT NULL, content VARCHAR(2000) NOT NULL "
                "DEFAULT '', revision INTEGER NOT NULL DEFAULT 0)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE chat_agent_memory_revisions (id CHAR(32) PRIMARY KEY, "
                "guild_id VARCHAR(20) NOT NULL, content VARCHAR(2000) NOT NULL, "
                "revision INTEGER NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO chat_agent_guild_memory VALUES ('a', :guild, :content, 9)"
            ),
            {"guild": _GUILD, "content": _IDENTITY + "\n\n" + _MEMORY},
        )
        connection.execute(
            text("INSERT INTO chat_agent_memory_revisions VALUES ('b', :guild, 'old', 8)"),
            {"guild": _GUILD},
        )

        revision = _revision_module()
        revision.op = Operations(MigrationContext.configure(connection))
        revision.upgrade()

        row = connection.execute(
            text("SELECT content, behavior, personality, revision FROM chat_agent_guild_memory")
        ).one()
        assert row == (_IDENTITY + "\n\n" + _MEMORY, "", "", 9)
        history = connection.execute(
            text("SELECT content, behavior, personality FROM chat_agent_memory_revisions")
        ).one()
        assert history == ("old", "", "")

        revision.downgrade()
        columns = {
            column["name"]
            for column in inspect(connection).get_columns("chat_agent_guild_memory")
        }
        assert "behavior" not in columns and "personality" not in columns
        assert connection.execute(
            text("SELECT content FROM chat_agent_guild_memory")
        ).scalar_one() == _IDENTITY + "\n\n" + _MEMORY
    engine.dispose()


# -- the upsert ------------------------------------------------------------------


async def test_upsert_that_names_no_block_leaves_both_alone(db_session):
    await _seed(db_session)

    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="rewritten",
        notes_consumed=2,
        model_name="stub-model",
        dreamed_at=_DREAMED_AT,
    )
    await db_session.commit()

    row = await get_guild_memory_blob(db_session, _GUILD)
    assert row.content == "rewritten"
    assert row.behavior == _BEHAVIOR
    assert row.personality == _PERSONALITY


async def test_first_upsert_without_blocks_starts_them_empty(db_session):
    record = await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content=_MEMORY,
        notes_consumed=1,
        model_name=None,
        dreamed_at=_DREAMED_AT,
    )
    assert (record.behavior, record.personality) == ("", "")


async def test_revision_records_both_blocks(db_session):
    record = await record_memory_revision(
        db_session,
        guild_id=_GUILD,
        content=_MEMORY,
        behavior=_BEHAVIOR,
        personality=_PERSONALITY,
        revision=1,
        notes_consumed=1,
        model_name=None,
    )
    assert (record.behavior, record.personality) == (_BEHAVIOR, _PERSONALITY)


# -- validation ------------------------------------------------------------------


def test_omitted_blocks_are_kept_verbatim():
    blocks = compose_blocks(DreamOutput(memory=_MEMORY), _context(), retries_left=2)
    assert (blocks.behavior, blocks.personality) == (_BEHAVIOR, _PERSONALITY)
    assert blocks.refusals == ()
    assert blocks.memory == _MEMORY


def test_behavior_is_accepted_at_exactly_its_limit():
    exact = "b" * MAX_BEHAVIOR_CHARS
    blocks = compose_blocks(
        DreamOutput(memory=_MEMORY, behavior=exact), _context(), retries_left=2
    )
    assert blocks.behavior == exact


def test_behavior_one_over_its_limit_is_retried_then_refused():
    output = DreamOutput(memory=_MEMORY, behavior="b" * (MAX_BEHAVIOR_CHARS + 1))
    with pytest.raises(ModelRetry, match="750"):
        compose_blocks(output, _context(), retries_left=1)
    blocks = compose_blocks(output, _context(), retries_left=0)
    assert blocks.behavior == _BEHAVIOR
    assert blocks.refusals == ("behavior",)


def test_personality_is_accepted_at_exactly_its_limit_on_a_first_night():
    exact = "p" * MAX_PERSONALITY_CHARS
    blocks = compose_blocks(
        DreamOutput(memory=_MEMORY, personality=exact),
        _context(previous_personality=""),
        retries_left=2,
    )
    assert blocks.personality == exact


def test_personality_one_over_its_limit_is_retried_then_refused():
    output = DreamOutput(
        memory=_MEMORY,
        personality="p" * (MAX_PERSONALITY_CHARS + 1),
        personality_reason="Months of evidence.",
    )
    with pytest.raises(ModelRetry, match="250"):
        compose_blocks(output, _context(), retries_left=1)
    assert compose_blocks(output, _context(), retries_left=0).personality == _PERSONALITY


@pytest.mark.parametrize("empty", ["", "   \n"])
def test_an_empty_revision_cannot_wipe_an_established_block(empty):
    output = DreamOutput(memory=_MEMORY, behavior=empty, personality=empty)
    with pytest.raises(ModelRetry, match="erase"):
        compose_blocks(output, _context(), retries_left=1)
    blocks = compose_blocks(output, _context(), retries_left=0)
    assert (blocks.behavior, blocks.personality) == (_BEHAVIOR, _PERSONALITY)


def test_an_empty_block_can_stay_empty():
    blocks = compose_blocks(
        DreamOutput(memory=_MEMORY, behavior="", personality=""),
        _context(previous_behavior="", previous_personality=""),
        retries_left=1,
    )
    assert (blocks.behavior, blocks.personality) == ("", "")


def test_behavior_revision_needs_no_reason():
    revised = _BEHAVIOR + " Keep jokes out of #help."
    blocks = compose_blocks(
        DreamOutput(memory=_MEMORY, behavior=revised), _context(), retries_left=1
    )
    assert blocks.behavior == revised


def test_changing_an_established_personality_needs_a_reason():
    output = DreamOutput(memory=_MEMORY, personality="Loud and chaotic.")
    with pytest.raises(ModelRetry, match="personality_reason"):
        compose_blocks(output, _context(), retries_left=1)
    assert compose_blocks(output, _context(), retries_left=0).personality == _PERSONALITY

    reasoned = output.model_copy(
        update={"personality_reason": "Weeks of people asking me to be louder."}
    )
    assert compose_blocks(reasoned, _context(), retries_left=0).personality == (
        "Loud and chaotic."
    )


def test_restating_the_personality_is_not_a_change():
    blocks = compose_blocks(
        DreamOutput(memory=_MEMORY, personality=f"  {_PERSONALITY}\n"),
        _context(),
        retries_left=1,
    )
    assert blocks.personality == _PERSONALITY


def test_identity_trait_moves_into_a_revised_block():
    blocks = compose_blocks(
        DreamOutput(
            memory=_MEMORY,
            behavior=_BEHAVIOR + " I let others finish.",
            identity_moves=["I let others finish."],
        ),
        _context(previous_blob=_IDENTITY + "\n\n" + _MEMORY),
        retries_left=1,
    )
    assert blocks.memory.startswith("## Identity & Voice\n- I use dry humor.\n\n")
    assert "I let others finish." not in blocks.memory
    assert blocks.behavior.endswith("I let others finish.")


def test_identity_trait_cannot_move_into_an_unchanged_block():
    output = DreamOutput(memory=_MEMORY, identity_moves=["I let others finish."])
    context = _context(previous_blob=_IDENTITY + "\n\n" + _MEMORY)
    with pytest.raises(ModelRetry, match="identity_moves"):
        compose_blocks(output, context, retries_left=1)
    # Out of retries the trait stays where it was rather than vanishing.
    assert compose_blocks(output, context, retries_left=0).memory.startswith(_IDENTITY)


def test_identity_moves_need_the_trait_itself_in_the_revised_block():
    # An unrelated behavior edit is not somewhere for either trait to go.
    output = DreamOutput(
        memory=_MEMORY,
        behavior="Keep jokes out of #help.",
        identity_moves=["I use dry humor.", "I let others finish."],
    )
    context = _context(previous_blob=_IDENTITY + "\n\n" + _MEMORY)
    with pytest.raises(ModelRetry, match="I use dry humor"):
        compose_blocks(output, context, retries_left=1)
    blocks = compose_blocks(output, context, retries_left=0)
    assert blocks.memory.startswith(_IDENTITY)
    assert blocks.refusals == ("identity_moves", "identity_moves")


def test_identity_moves_are_judged_one_trait_at_a_time():
    blocks = compose_blocks(
        DreamOutput(
            memory=_MEMORY,
            behavior=_BEHAVIOR + " i LET others   finish.",
            identity_moves=["I let others finish.", "I use dry humor."],
        ),
        _context(previous_blob=_IDENTITY + "\n\n" + _MEMORY),
        retries_left=0,
    )
    assert blocks.memory.startswith("## Identity & Voice\n- I use dry humor.\n\n")
    assert "I let others finish." not in blocks.memory
    assert blocks.refusals == ("identity_moves",)


def test_a_trait_cannot_move_into_a_personality_that_was_refused():
    output = DreamOutput(
        memory=_MEMORY,
        behavior=_BEHAVIOR + " Keep jokes out of #help.",
        personality="I use dry humor.",
        identity_moves=["I use dry humor."],
    )
    context = _context(previous_blob=_IDENTITY + "\n\n" + _MEMORY)
    blocks = compose_blocks(output, context, retries_left=0)
    assert blocks.personality == _PERSONALITY
    assert blocks.memory.startswith(_IDENTITY)
    assert blocks.refusals == ("personality", "identity_moves")


def test_a_trait_already_in_the_block_has_not_moved():
    output = DreamOutput(
        memory=_MEMORY,
        behavior="I let others finish. Keep jokes out of #help.",
        identity_moves=["I let others finish."],
    )
    context = _context(
        previous_blob=_IDENTITY + "\n\n" + _MEMORY,
        previous_behavior="I let others finish.",
    )
    assert compose_blocks(output, context, retries_left=0).memory.startswith(_IDENTITY)


def test_identity_trait_that_does_not_exist_cannot_move():
    with pytest.raises(ModelRetry):
        compose_blocks(
            DreamOutput(
                memory=_MEMORY,
                behavior=_BEHAVIOR + " I never said this.",
                identity_moves=["I never said this."],
            ),
            _context(previous_blob=_IDENTITY),
            retries_left=0,
        )


# -- the prompt the dream reads ----------------------------------------------------


def test_dream_prompt_shows_all_three_blocks_labelled():
    message = build_dream_user_message(
        previous_blob=_MEMORY,
        note_lines=["09:00Z #dev-help — kai shipped the shader."],
        day=date(2026, 8, 6),
        previous_behavior=_BEHAVIOR,
        previous_personality=_PERSONALITY,
    )
    assert f"# My personality\n\n{_PERSONALITY}" in message
    assert f"# My behavior\n\n{_BEHAVIOR}" in message
    assert f"# What I remember so far\n\n{_MEMORY}" in message
    assert chat_memory_dream.FIRST_NIGHT_NUDGE not in message


def test_dream_prompt_marks_empty_blocks_without_a_first_night_nudge():
    message = build_dream_user_message(
        previous_blob=_MEMORY, note_lines=["a note"], day=date(2026, 8, 6)
    )
    assert "# My personality\n\n(empty)" in message
    assert "# My behavior\n\n(empty)" in message
    assert chat_memory_dream.FIRST_NIGHT_NUDGE not in message


def test_dream_system_prompt_states_both_limits_and_the_retention_rules():
    prompt = chat_memory_dream.DREAM_SYSTEM_PROMPT
    assert "at most 750 characters" in prompt
    assert "at most 250 characters" in prompt
    assert "return `null` for a block to keep it" in prompt
    assert "`personality_reason`" in prompt


# -- a whole night ------------------------------------------------------------------


async def test_dream_writes_revised_blocks_and_records_them(db_session):
    await _seed(db_session)
    await _write_note(db_session)
    revised = _BEHAVIOR + " Keep jokes out of #help."

    result, agent = await _dream(
        db_session,
        DreamOutput(
            memory=_MEMORY,
            behavior=revised,
            personality="Dry and warm.",
            personality_reason="Weeks of people finding me too wordy.",
        ),
    )

    assert result.outcome is DreamOutcome.DREAMED
    assert _PERSONALITY in agent.prompts[0] and _BEHAVIOR in agent.prompts[0]
    row = await get_guild_memory_blob(db_session, _GUILD)
    assert (row.behavior, row.personality) == (revised, "Dry and warm.")
    history = (
        await db_session.execute(
            select(ChatAgentMemoryRevision).where(
                ChatAgentMemoryRevision.revision == row.revision
            )
        )
    ).scalar_one()
    assert (history.behavior, history.personality) == (revised, "Dry and warm.")


async def test_dream_that_omits_the_blocks_keeps_them(db_session):
    await _seed(db_session)
    note = await _write_note(db_session)

    result, _ = await _dream(db_session, DreamOutput(memory=_MEMORY + "\nNew line."))

    assert result.outcome is DreamOutcome.DREAMED
    row = await get_guild_memory_blob(db_session, _GUILD)
    assert row.content.endswith("New line.")
    assert (row.behavior, row.personality) == (_BEHAVIOR, _PERSONALITY)
    assert await db_session.get(ChatAgentMemoryNote, note.id) is None


async def test_refused_block_revisions_keep_everything_and_the_notes(db_session):
    # The memory half may assume the refused edit: a lesson taken out of memory
    # for a behavior block that never took it would be lost from both.
    await _seed(db_session)
    note = await _write_note(db_session)

    result, _ = await _dream(
        db_session,
        DreamOutput(
            memory=_MEMORY + "\nNew line.",
            behavior="b" * (MAX_BEHAVIOR_CHARS + 1),
            personality="Unexplained new me.",
        ),
    )

    assert result.outcome is DreamOutcome.KEPT_PREVIOUS
    row = await get_guild_memory_blob(db_session, _GUILD)
    assert row.content == _MEMORY
    assert (row.behavior, row.personality) == (_BEHAVIOR, _PERSONALITY)
    assert row.revision == 1
    assert await db_session.get(ChatAgentMemoryNote, note.id) is not None


async def test_an_identity_move_with_nowhere_to_go_keeps_everything(db_session):
    await _seed(db_session, content=_IDENTITY + "\n\n" + _MEMORY)
    note = await _write_note(db_session)

    result, _ = await _dream(
        db_session,
        DreamOutput(
            memory=_MEMORY,
            behavior=_BEHAVIOR + " Keep jokes out of #help.",
            identity_moves=["I use dry humor."],
        ),
    )

    assert result.outcome is DreamOutcome.KEPT_PREVIOUS
    row = await get_guild_memory_blob(db_session, _GUILD)
    assert row.content == _IDENTITY + "\n\n" + _MEMORY
    assert row.behavior == _BEHAVIOR
    assert await db_session.get(ChatAgentMemoryNote, note.id) is not None


async def test_degenerate_memory_keeps_every_block_and_every_note(db_session):
    long_memory = _MEMORY + "\n" + "kai still hates cmake.\n" * 12
    await _seed(db_session, content=long_memory)
    note = await _write_note(db_session)

    result, _ = await _dream(
        db_session,
        DreamOutput(memory="ok.", behavior="Totally new.", personality="New."),
    )

    assert result.outcome is DreamOutcome.KEPT_PREVIOUS
    row = await get_guild_memory_blob(db_session, _GUILD)
    assert row.content == long_memory
    assert (row.behavior, row.personality) == (_BEHAVIOR, _PERSONALITY)
    assert await db_session.get(ChatAgentMemoryNote, note.id) is not None


async def test_first_dream_can_start_both_blocks(db_session):
    await _write_note(db_session)

    result, agent = await _dream(
        db_session,
        DreamOutput(memory=_MEMORY, behavior=_BEHAVIOR, personality=_PERSONALITY),
    )

    assert result.outcome is DreamOutcome.DREAMED
    assert chat_memory_dream.FIRST_NIGHT_NUDGE in agent.prompts[0]
    row = await get_guild_memory_blob(db_session, _GUILD)
    assert (row.behavior, row.personality) == (_BEHAVIOR, _PERSONALITY)


# -- the real validator loop ------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_retries_an_over_limit_behavior_then_keeps_the_old_one(monkeypatch):
    monkeypatch.setattr(chat_memory_dream, "_dream_agent", None)
    output = DreamOutput(memory=_MEMORY, behavior="b" * (MAX_BEHAVIOR_CHARS + 1))
    model = TestModel(custom_output_args=output.model_dump())
    monkeypatch.setattr(chat_memory_dream, "build_model_for", lambda _: model)

    result = await get_dream_agent().run("Distill today's notes.", deps=_context())

    # Every retry was spent, and the last attempt resolves to the old block.
    retries = [
        part
        for message in result.all_messages()
        for part in getattr(message, "parts", [])
        if part.part_kind == "retry-prompt"
    ]
    assert len(retries) == chat_memory_dream.DREAM_OUTPUT_RETRIES
    blocks = compose_blocks(result.output, _context(), retries_left=0)
    assert (blocks.behavior, blocks.refusals) == (_BEHAVIOR, ("behavior",))
    monkeypatch.setattr(chat_memory_dream, "_dream_agent", None)
