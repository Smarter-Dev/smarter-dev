"""Tests for the admin purge of one person from a guild's chat memory.

The model is always a stub or a scripted FunctionModel: these tests assert on
rows, on what the agent was given and on what the code refuses — never on
prose quality. Two synthetic members throughout: ``kai`` is being purged,
``nia`` is not, and an unrelated running bit must survive every purge.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from smarter_dev.shared.privacy_purge import PurgeTarget
from smarter_dev.web.chat_memory_purge import NoteEdit
from smarter_dev.web.chat_memory_purge import PurgeContext
from smarter_dev.web.chat_memory_purge import PurgeOutput
from smarter_dev.web.chat_memory_purge import PurgeRefused
from smarter_dev.web.chat_memory_purge import UnresolvedItem
from smarter_dev.web.chat_memory_purge import build_purge_agent
from smarter_dev.web.chat_memory_purge import compose_purge
from smarter_dev.web.chat_memory_purge import purge_guild_memory
from smarter_dev.web.crud import create_memory_note
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import record_memory_revision
from smarter_dev.web.crud import upsert_guild_memory_blob
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.models import ChatAgentMemoryRevision

_GUILD = "123456789012345678"
_CHANNEL = "555000111222333444"
_KAI_ID = "111111111111111111"
_NIA_ID = "222222222222222222"
_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

KAI = PurgeTarget.build(_KAI_ID, ["kai", "Kai the Rustacean", " kai "])

IDENTITY = "## Identity & Voice\n- Dry, warm, direct."
LORE = "## Lore & Running Bits\n- The tabs-versus-spaces truce of August."
NIA_LINE = f"- nia (id {_NIA_ID}) opens with a wind-up every time."
KAI_LINE = f"- kai (id {_KAI_ID}) is deep in embedded rust."

MEMORY_WITH_KAI = f"{IDENTITY}\n\n## People & Relationships\n{NIA_LINE}\n{KAI_LINE}\n\n{LORE}"
MEMORY_WITHOUT_KAI = f"{IDENTITY}\n\n## People & Relationships\n{NIA_LINE}\n\n{LORE}"
BEHAVIOR = "Wait to be asked before explaining."
BEHAVIOR_WITH_KAI = "Wait to be asked before explaining. kai wants code, not prose."
PERSONALITY = "The one who remembered the thing nobody else did."

KAI_NOTE = "kai shipped the shader in #dev-help."
MIXED_NOTE = "kai and nia argued about cmake; nia won."
MIXED_NOTE_CLEAN = "nia argued about cmake and won."
NIA_NOTE = "nia is building a synth."


def _all_text(*parts: str) -> str:
    return "\n".join(parts)


# -- the scripted agent ----------------------------------------------------------


@dataclass
class _Result:
    output: PurgeOutput


@dataclass
class _ScriptedPurgeAgent:
    """Removes kai the way a well-behaved model would, from whatever it is given."""

    prompts: list[str] = field(default_factory=list)
    leave_id_in_memory: bool = False
    raise_error: bool = False

    async def run(self, user_prompt: str, *, deps: PurgeContext) -> _Result:
        self.prompts.append(user_prompt)
        if self.raise_error:
            raise RuntimeError("provider is down")
        memory = deps.memory.replace(f"\n{KAI_LINE}", "")
        if self.leave_id_in_memory:
            memory = deps.memory
        behavior = deps.behavior.replace(" kai wants code, not prose.", "")
        notes = []
        for note_id, content in deps.notes:
            if content == KAI_NOTE:
                notes.append(NoteEdit(id=note_id, action="drop"))
            elif content == MIXED_NOTE:
                notes.append(NoteEdit(id=note_id, action="rewrite", text=MIXED_NOTE_CLEAN))
            else:
                notes.append(NoteEdit(id=note_id, action="keep"))
        return _Result(
            PurgeOutput(
                memory=memory,
                behavior=behavior,
                personality=deps.personality,
                notes=notes,
            )
        )


# -- seeding --------------------------------------------------------------------


async def _seed(session, *, memory=MEMORY_WITH_KAI, behavior=BEHAVIOR_WITH_KAI):
    await upsert_guild_memory_blob(
        session,
        guild_id=_GUILD,
        content=memory,
        behavior=behavior,
        personality=PERSONALITY,
        notes_consumed=3,
        model_name="stub-model",
        dreamed_at=_NOW - timedelta(hours=12),
    )
    record = await get_guild_memory_blob(session, _GUILD)
    # Two retained revisions: the current blocks and an older night.
    await record_memory_revision(
        session,
        guild_id=_GUILD,
        content=f"{IDENTITY}\n\n## People & Relationships\n{KAI_LINE}",
        behavior=BEHAVIOR,
        personality=PERSONALITY,
        revision=record.revision - 1 if record.revision > 1 else 0,
        notes_consumed=2,
        model_name="stub-model",
    )
    await record_memory_revision(
        session,
        guild_id=_GUILD,
        content=memory,
        behavior=behavior,
        personality=PERSONALITY,
        revision=record.revision,
        notes_consumed=3,
        model_name="stub-model",
    )
    for content in (KAI_NOTE, MIXED_NOTE, NIA_NOTE):
        await create_memory_note(
            session,
            guild_id=_GUILD,
            channel_id=_CHANNEL,
            channel_name="dev-help",
            content=content,
            created_at=_NOW - timedelta(hours=1),
            day_start=_NOW.replace(hour=0),
        )
    await session.commit()


async def _snapshot(session) -> tuple:
    memory = await get_guild_memory_blob(session, _GUILD)
    notes = sorted(
        note.content for note in (await session.scalars(select(ChatAgentMemoryNote))).all()
    )
    revisions = sorted(
        (r.revision, r.content, r.behavior, r.personality)
        for r in (await session.scalars(select(ChatAgentMemoryRevision))).all()
    )
    return (
        (memory.content, memory.behavior, memory.personality, memory.revision)
        if memory
        else None,
        tuple(notes),
        tuple(revisions),
    )


async def _everything_stored(session) -> str:
    memory = await get_guild_memory_blob(session, _GUILD)
    notes = (await session.scalars(select(ChatAgentMemoryNote))).all()
    revisions = (await session.scalars(select(ChatAgentMemoryRevision))).all()
    return _all_text(
        memory.content,
        memory.behavior,
        memory.personality,
        *(n.content for n in notes),
        *(f"{r.content}\n{r.behavior}\n{r.personality}" for r in revisions),
    )


# -- the target ------------------------------------------------------------------


def test_target_matches_the_id_as_a_whole_number_and_names_as_whole_words():
    assert KAI.names == ("kai", "Kai the Rustacean")
    assert KAI.id_hits(f"kai (id {_KAI_ID})") == 1
    assert KAI.id_hits(f"id 9{_KAI_ID}") == 0
    assert KAI.name_hits("Kai said hi; KAI again") == 2
    assert KAI.name_hits("kaiser and makai") == 0


# -- one guild, end to end -------------------------------------------------------


async def test_purge_removes_the_person_from_every_layer_and_keeps_everyone_else(db_session):
    await _seed(db_session)
    before = await get_guild_memory_blob(db_session, _GUILD)
    revision_before = before.revision
    agent = _ScriptedPurgeAgent()

    result = await purge_guild_memory(
        db_session, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
    )
    await db_session.commit()

    assert result.outcome == "purged"
    assert result.notes_dropped == 1
    assert result.notes_rewritten == 1
    assert result.revisions_rewritten >= 1
    stored = await _everything_stored(db_session)
    assert not KAI.id_hits(stored)
    assert not KAI.name_hits(stored)
    # Everyone and everything else survives, word for word.
    assert NIA_LINE in stored
    assert LORE in stored
    assert IDENTITY in stored
    assert NIA_NOTE in stored
    assert MIXED_NOTE_CLEAN in stored
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert memory.content == MEMORY_WITHOUT_KAI
    assert memory.behavior == BEHAVIOR
    assert memory.personality == PERSONALITY
    assert memory.revision == revision_before + 1
    # The target reached the agent only through the purge prompt.
    assert all(_KAI_ID in prompt for prompt in agent.prompts)


async def test_purging_a_clean_guild_again_changes_nothing(db_session):
    await _seed(db_session)
    await purge_guild_memory(
        db_session, guild_id=_GUILD, target=KAI, now=_NOW, agent=_ScriptedPurgeAgent()
    )
    await db_session.commit()
    after_first = await _snapshot(db_session)

    result = await purge_guild_memory(
        db_session, guild_id=_GUILD, target=KAI, now=_NOW, agent=_ScriptedPurgeAgent()
    )
    await db_session.commit()

    assert result.outcome == "unchanged"
    assert await _snapshot(db_session) == after_first


async def test_a_guild_with_no_memory_never_calls_the_model(db_session):
    agent = _ScriptedPurgeAgent()
    result = await purge_guild_memory(
        db_session, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
    )
    assert result.outcome == "empty"
    assert agent.prompts == []


@pytest.mark.parametrize(
    "agent",
    [_ScriptedPurgeAgent(leave_id_in_memory=True), _ScriptedPurgeAgent(raise_error=True)],
    ids=["id-left-behind", "provider-error"],
)
async def test_a_failed_purge_leaves_every_row_as_it_was(db_session, agent):
    await _seed(db_session)
    before = await _snapshot(db_session)

    with pytest.raises((PurgeRefused, RuntimeError)):
        await purge_guild_memory(
            db_session, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
        )
    await db_session.rollback()

    assert await _snapshot(db_session) == before


# -- what the validator refuses ---------------------------------------------------


def _context(**overrides) -> PurgeContext:
    values = {
        "target": KAI,
        "memory": MEMORY_WITH_KAI,
        "behavior": BEHAVIOR,
        "personality": PERSONALITY,
        "notes": (("n1", NIA_NOTE),),
    }
    values.update(overrides)
    return PurgeContext(**values)


def _output(**overrides) -> PurgeOutput:
    values = {
        "memory": MEMORY_WITHOUT_KAI,
        "behavior": BEHAVIOR,
        "personality": PERSONALITY,
        "notes": [NoteEdit(id="n1", action="keep")],
    }
    values.update(overrides)
    return PurgeOutput(**values)


def test_a_clean_purge_is_accepted():
    blocks = compose_purge(_output(), _context(), retries_left=0)
    assert blocks.memory == MEMORY_WITHOUT_KAI


@pytest.mark.parametrize(
    "output",
    [
        _output(memory=MEMORY_WITH_KAI),
        _output(behavior=f"{BEHAVIOR} Ask {_KAI_ID} first."),
        _output(notes=[NoteEdit(id="n1", action="rewrite", text=f"{NIA_NOTE} kai too.")]),
        _output(personality=""),
        _output(memory="ok."),
        _output(notes=[]),
        _output(notes=[NoteEdit(id="n9", action="keep")]),
        _output(memory=MEMORY_WITHOUT_KAI + "\n" + "x" * 2000),
        _output(
            unresolved=[UnresolvedItem(location="memory", reason=f"maybe {_KAI_ID}")]
        ),
    ],
    ids=[
        "id-left",
        "id-added",
        "name-in-note",
        "unrelated-block-emptied",
        "memory-collapsed",
        "note-missing",
        "unknown-note",
        "over-limit",
        "id-in-reason",
    ],
)
def test_unclean_output_is_retried_then_refused(output):
    with pytest.raises(ModelRetry):
        compose_purge(output, _context(), retries_left=1)
    with pytest.raises(PurgeRefused):
        compose_purge(output, _context(), retries_left=0)


def test_a_shared_name_is_accepted_only_when_the_agent_says_why():
    other_kai = MEMORY_WITHOUT_KAI + "\n- kai from the other server runs the meetup."
    with pytest.raises(PurgeRefused):
        compose_purge(_output(memory=other_kai), _context(), retries_left=0)
    blocks = compose_purge(
        _output(
            memory=other_kai,
            unresolved=[UnresolvedItem(location="memory", reason="a different member shares the name")],
        ),
        _context(),
        retries_left=0,
    )
    assert blocks.unresolved[0].location == "memory"


def test_a_block_that_was_only_about_the_person_may_become_empty():
    blocks = compose_purge(
        _output(behavior=""),
        _context(behavior="kai wants code, not prose."),
        retries_left=0,
    )
    assert blocks.behavior == ""


# -- the real agent wiring --------------------------------------------------------


async def test_the_agent_is_asked_again_when_its_first_answer_keeps_the_id():
    """Unmocked validator path: pydantic-ai retries on the refusal, then accepts."""
    calls: list[int] = []

    def respond(messages, info: AgentInfo) -> ModelResponse:
        calls.append(1)
        memory = MEMORY_WITH_KAI if len(calls) == 1 else MEMORY_WITHOUT_KAI
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "memory": memory,
                        "behavior": BEHAVIOR,
                        "personality": PERSONALITY,
                        "notes": [{"id": "n1", "action": "keep"}],
                    },
                )
            ]
        )

    agent = build_purge_agent(FunctionModel(respond))
    result = await agent.run("purge", deps=_context())

    assert len(calls) == 2
    assert result.output.memory == MEMORY_WITHOUT_KAI
