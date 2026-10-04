"""Tests for the admin purge of one person from a guild's chat memory.

The model is always a stub or a scripted FunctionModel: these tests assert on
rows, on what the agent was given and on what the code refuses — never on
prose quality. Two synthetic members throughout: ``kai`` is being purged,
``nia`` is not, and an unrelated running bit must survive every purge.
"""

from __future__ import annotations

import contextlib
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

from smarter_dev.shared.database import async_sessionmaker
from smarter_dev.shared.privacy_purge import PurgeTarget
from smarter_dev.web.chat_memory_purge import PurgeContext
from smarter_dev.web.chat_memory_purge import PurgeOutput
from smarter_dev.web.chat_memory_purge import PurgeRefused
from smarter_dev.web.chat_memory_purge import SegmentEdit
from smarter_dev.web.chat_memory_purge import UnresolvedItem
from smarter_dev.web.chat_memory_purge import build_purge_agent
from smarter_dev.web.chat_memory_purge import build_purge_user_message
from smarter_dev.web.chat_memory_purge import compose_purge
from smarter_dev.web.chat_memory_purge import purge_guild_memory
from smarter_dev.web.chat_memory_purge import segments
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
MIXED_NOTE_CLEAN = "nia argued about cmake; nia won."
NIA_NOTE = "nia is building a synth."


def _all_text(*parts: str) -> str:
    return "\n".join(parts)


# -- the scripted agent ----------------------------------------------------------


@dataclass
class _Result:
    output: PurgeOutput


def scripted_edits(deps: PurgeContext, *, keep_ids: bool = False) -> list[SegmentEdit]:
    """What a well-behaved model decides: a segment mixing kai with nia is
    rewritten to be only about nia, any other segment naming kai is removed."""
    edits = []
    for segs in deps.editable_by_location().values():
        for seg in segs:
            if keep_ids and deps.target.id_hits(seg.text):
                edits.append(SegmentEdit(id=seg.id, action="keep"))
            elif "nia" in seg.body:
                edits.append(
                    SegmentEdit(id=seg.id, action="rewrite", text=seg.body.replace("kai and ", ""))
                )
            else:
                edits.append(SegmentEdit(id=seg.id, action="remove"))
    return edits


@dataclass
class _ScriptedPurgeAgent:
    """Removes kai the way a well-behaved model would, from whatever it is given."""

    prompts: list[str] = field(default_factory=list)
    contexts: list[PurgeContext] = field(default_factory=list)
    leave_id_in_memory: bool = False
    raise_error: bool = False
    unresolved_for_revisions: bool = False
    before_answer: object = None

    async def run(self, user_prompt: str, *, deps: PurgeContext) -> _Result:
        self.prompts.append(user_prompt)
        self.contexts.append(deps)
        if self.before_answer is not None:
            await self.before_answer()
        if self.raise_error:
            raise RuntimeError("provider is down")
        unresolved = []
        if self.unresolved_for_revisions and not deps.notes and "memory" in deps.editable:
            unresolved.append(
                UnresolvedItem(location="memory", reason="a line may be about them unnamed")
            )
        return _Result(
            PurgeOutput(
                edits=scripted_edits(deps, keep_ids=self.leave_id_in_memory),
                unresolved=unresolved,
            )
        )


@pytest.fixture
def session_factory(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def factory():
        async with maker() as session:
            yield session

    return factory


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
    session.expire_all()
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
    session.expire_all()
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


async def test_purge_removes_the_person_from_every_layer_and_keeps_everyone_else(
    db_session, session_factory
):
    await _seed(db_session)
    before = await get_guild_memory_blob(db_session, _GUILD)
    revision_before = before.revision
    agent = _ScriptedPurgeAgent()

    result = await purge_guild_memory(
        session_factory, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
    )

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
    db_session.expire_all()
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert memory.content == MEMORY_WITHOUT_KAI
    assert memory.behavior == BEHAVIOR
    assert memory.personality == PERSONALITY
    assert memory.revision == revision_before + 1
    # The target reached the agent only through the purge prompt.
    assert all(_KAI_ID in prompt for prompt in agent.prompts)


async def test_purging_a_clean_guild_again_changes_nothing(db_session, session_factory):
    await _seed(db_session)
    await purge_guild_memory(
        session_factory, guild_id=_GUILD, target=KAI, now=_NOW, agent=_ScriptedPurgeAgent()
    )
    after_first = await _snapshot(db_session)
    agent = _ScriptedPurgeAgent()

    result = await purge_guild_memory(
        session_factory, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
    )

    assert result.outcome == "unchanged"
    assert await _snapshot(db_session) == after_first
    # Nothing mentions kai any more: only the notes batch went to the model,
    # with no block shown as editable.
    assert len(agent.contexts) == 1
    assert agent.contexts[0].editable == ()


async def test_a_guild_with_no_memory_never_calls_the_model(db_session, session_factory):
    agent = _ScriptedPurgeAgent()
    result = await purge_guild_memory(
        session_factory, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
    )
    assert result.outcome == "empty"
    assert agent.prompts == []


@pytest.mark.parametrize(
    "agent",
    [_ScriptedPurgeAgent(leave_id_in_memory=True), _ScriptedPurgeAgent(raise_error=True)],
    ids=["id-left-behind", "provider-error"],
)
async def test_a_failed_purge_leaves_every_row_as_it_was(db_session, session_factory, agent):
    await _seed(db_session)
    before = await _snapshot(db_session)

    with pytest.raises((PurgeRefused, RuntimeError)):
        await purge_guild_memory(
            session_factory, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
        )

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


def _ids(context: PurgeContext) -> list[str]:
    return [seg.id for segs in context.editable_by_location().values() for seg in segs]


def _output(context: PurgeContext | None = None, **overrides) -> PurgeOutput:
    context = context or _context()
    values = {"edits": scripted_edits(context)}
    values.update(overrides)
    return PurgeOutput(**values)


def test_a_clean_purge_is_accepted():
    blocks = compose_purge(_output(), _context(), retries_left=0)
    assert blocks.memory == MEMORY_WITHOUT_KAI
    assert blocks.changed_blocks == {"memory"}
    assert blocks.behavior == BEHAVIOR and blocks.personality == PERSONALITY


def _first_id() -> str:
    return _ids(_context())[0]


@pytest.mark.parametrize(
    "edits",
    [
        [SegmentEdit(id=_first_id(), action="keep")],
        [SegmentEdit(id=_first_id(), action="rewrite", text=f"someone ({_KAI_ID}) likes rust")],
        [SegmentEdit(id=_first_id(), action="rewrite", text="kai likes rust")],
        [SegmentEdit(id=_first_id(), action="rewrite", text="(empty)")],
        [SegmentEdit(id=_first_id(), action="rewrite", text="two\nlines")],
        [SegmentEdit(id=_first_id(), action="rewrite", text="")],
        [],
        [SegmentEdit(id=_first_id(), action="remove")] * 2,
        [SegmentEdit(id=_first_id(), action="remove"), SegmentEdit(id="memory:0", action="remove")],
        [SegmentEdit(id=_first_id(), action="remove"), SegmentEdit(id="behavior:0", action="remove")],
    ],
    ids=[
        "keeps-the-id",
        "rewrite-adds-the-id",
        "rewrite-keeps-the-name",
        "placeholder",
        "rewrite-adds-a-line",
        "empty-rewrite",
        "segment-missing",
        "segment-twice",
        "heading-not-editable",
        "unshown-block",
    ],
)
def test_unclean_output_is_retried_then_refused(edits):
    output = PurgeOutput(edits=edits)
    with pytest.raises(ModelRetry):
        compose_purge(output, _context(), retries_left=1)
    with pytest.raises(PurgeRefused):
        compose_purge(output, _context(), retries_left=0)


def test_unresolved_reasons_never_carry_the_id_or_a_name():
    for reason in (f"maybe {_KAI_ID}", "maybe Kai"):
        output = _output(unresolved=[UnresolvedItem(location="memory", reason=reason)])
        with pytest.raises(PurgeRefused):
            compose_purge(output, _context(), retries_left=0)


def test_a_shared_name_is_kept_only_when_the_agent_says_why():
    other = MEMORY_WITHOUT_KAI + "\n- kai from the other server runs the meetup."
    context = _context(memory=other)
    keep = PurgeOutput(edits=[SegmentEdit(id=_ids(context)[0], action="keep")])
    with pytest.raises(PurgeRefused):
        compose_purge(keep, context, retries_left=0)
    blocks = compose_purge(
        PurgeOutput(
            edits=keep.edits,
            unresolved=[UnresolvedItem(location="memory", reason="a different member shares the name")],
        ),
        context,
        retries_left=0,
    )
    assert blocks.memory == other and not blocks.changed_blocks


def test_a_block_that_was_only_about_the_person_may_become_empty():
    context = _context(behavior="kai wants code, not prose.")
    blocks = compose_purge(_output(context), context, retries_left=0)
    assert blocks.behavior == ""


def test_unresolved_locations_must_name_a_real_place():
    output = _output(unresolved=[UnresolvedItem(location="<script>", reason="shared name")])
    with pytest.raises(PurgeRefused):
        compose_purge(output, _context(), retries_left=0)


# -- the real agent wiring --------------------------------------------------------


async def test_the_agent_is_asked_again_when_its_first_answer_keeps_the_id():
    """Unmocked validator path: pydantic-ai retries on the refusal, then accepts."""
    calls: list[int] = []
    seg_id = _first_id()

    def respond(messages, info: AgentInfo) -> ModelResponse:
        calls.append(1)
        action = "keep" if len(calls) == 1 else "remove"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"edits": [{"id": seg_id, "action": action}]},
                )
            ]
        )

    agent = build_purge_agent(FunctionModel(respond))
    result = await agent.run("purge", deps=_context())

    assert len(calls) == 2
    assert compose_purge(result.output, _context(), retries_left=0).memory == MEMORY_WITHOUT_KAI


# -- the reviewer's cases: the edit design makes them impossible (review M1) --------


def test_bystanders_sharing_a_line_with_the_person_survive():
    line = "- Bob runs the meetup; kai co-hosts; Carol handles the wiki."
    listing = "  - Regulars: bob, kai, carol."
    memory = f"## People\n{line}\n{listing}\n- nia stays."
    context = _context(memory=memory)
    assert [seg.text for seg in context.editable_by_location()["memory"]] == ["kai co-hosts", "kai"]
    blocks = compose_purge(_output(context), context, retries_left=0)
    assert blocks.memory == (
        "## People\n- Bob runs the meetup; Carol handles the wiki.\n"
        "  - Regulars: bob, carol.\n- nia stays."
    )


def test_nothing_can_be_added_reordered_flattened_or_deduplicated():
    memory = (
        "## People\n- kai is here.\n  - nested: nia likes kai.\n- same line.\n- same line.\n"
        "## Lore\n- the truce."
    )
    context = _context(memory=memory)

    def edits(rewrite: str) -> PurgeOutput:
        out = []
        for seg in context.editable_by_location()["memory"]:
            if "nia" in seg.body:
                out.append(SegmentEdit(id=seg.id, action="rewrite", text=rewrite))
            else:
                out.append(SegmentEdit(id=seg.id, action="remove"))
        return PurgeOutput(edits=out)

    # An invented sentence riding on a rewrite is refused.
    with pytest.raises(PurgeRefused):
        compose_purge(edits("- nested: nia likes rust. Invented."), context, retries_left=0)
    # A rewrite that flattens the nesting keeps its indentation anyway; order,
    # duplicates and every other byte are the original's.
    blocks = compose_purge(edits("- nested: nia likes go."), context, retries_left=0)
    assert blocks.memory == (
        "## People\n  - nested: nia likes go.\n- same line.\n- same line.\n"
        "## Lore\n- the truce."
    )


def test_a_block_naming_the_person_once_cannot_be_blanked():
    context = _context(behavior=BEHAVIOR_WITH_KAI)
    blocks = compose_purge(_output(context), context, retries_left=0)
    assert blocks.behavior == BEHAVIOR
    # There is no edit that can touch the other sentence.
    other = segments("behavior", BEHAVIOR_WITH_KAI, KAI)[0]
    with pytest.raises(PurgeRefused):
        compose_purge(
            PurgeOutput(edits=[*_output(context).edits, SegmentEdit(id=other.id, action="remove")]),
            context,
            retries_left=0,
        )


def test_a_note_that_does_not_name_the_person_is_never_changed():
    context = _context()
    for edit in (
        SegmentEdit(id="note:n1:0", action="remove"),
        SegmentEdit(id="note:n1", action="remove"),
    ):
        output = _output(
            edits=[*scripted_edits(context), edit],
            unresolved=[UnresolvedItem(location="note:n1", reason="about them unnamed")],
        )
        with pytest.raises(PurgeRefused):
            compose_purge(output, context, retries_left=0)
    flagged = _output(unresolved=[UnresolvedItem(location="note:n1", reason="about them unnamed")])
    blocks = compose_purge(flagged, context, retries_left=0)
    assert not blocks.dropped_notes and not blocks.rewritten_notes
    assert blocks.unresolved[0].location == "note:n1"


def test_an_empty_block_stays_empty_and_unshown_blocks_are_not_in_the_prompt():
    context = _context(behavior="")
    prompt = build_purge_user_message(context)
    assert "(empty)" not in prompt and PERSONALITY not in prompt
    blocks = compose_purge(_output(context), context, retries_left=0)
    assert blocks.behavior == "" and blocks.personality == PERSONALITY


async def test_a_refused_purge_leaves_the_bytes_untouched(db_session, session_factory):
    await _seed(db_session)
    before = await _snapshot(db_session)

    class _Invents(_ScriptedPurgeAgent):
        async def run(self, user_prompt, *, deps):
            edits = scripted_edits(deps)
            edits.append(SegmentEdit(id="memory:0", action="rewrite", text="## New heading"))
            return _Result(PurgeOutput(edits=edits))

    with pytest.raises(PurgeRefused):
        await purge_guild_memory(
            session_factory, guild_id=_GUILD, target=KAI, now=_NOW, agent=_Invents()
        )
    assert await _snapshot(db_session) == before


async def test_a_revision_without_the_person_gets_no_model_call_and_no_write(
    db_session, session_factory
):
    clean_rev = f"{IDENTITY}\n\n## People & Relationships\n{NIA_LINE}"
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content=MEMORY_WITH_KAI,
        behavior=BEHAVIOR,
        personality=PERSONALITY,
        notes_consumed=1,
        model_name="stub-model",
        dreamed_at=_NOW - timedelta(hours=12),
    )
    await record_memory_revision(
        db_session,
        guild_id=_GUILD,
        content=clean_rev,
        behavior=BEHAVIOR,
        personality=PERSONALITY,
        revision=0,
        notes_consumed=1,
        model_name="stub-model",
    )
    await db_session.commit()
    rev_row = (await db_session.scalars(select(ChatAgentMemoryRevision))).one()
    updated_before = rev_row.updated_at
    agent = _ScriptedPurgeAgent()

    result = await purge_guild_memory(
        session_factory, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
    )

    assert result.outcome == "purged"
    assert result.model_calls == 1  # the live blocks only
    db_session.expire_all()
    revisions = (await db_session.scalars(select(ChatAgentMemoryRevision))).all()
    kept = [r for r in revisions if r.revision == 0][0]
    assert kept.content == clean_rev
    assert kept.updated_at == updated_before


# -- unresolved items from revision passes (review item 7) -------------------------


async def test_unresolved_items_from_revision_passes_reach_the_step(db_session, session_factory):
    await _seed(db_session)
    agent = _ScriptedPurgeAgent(unresolved_for_revisions=True)

    result = await purge_guild_memory(
        session_factory, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
    )

    locations = [item["location"] for item in result.as_step()["unresolved"]]
    assert any(loc.startswith("revision:") and loc.endswith("/memory") for loc in locations)


# -- every note, in batches (review item 8) ----------------------------------------


async def test_every_note_is_reviewed_in_batches_oldest_included(
    db_session, session_factory, monkeypatch
):
    from smarter_dev.web import chat_memory_purge

    monkeypatch.setattr(chat_memory_purge, "PURGE_NOTES_BATCH", 2)
    contents = [KAI_NOTE, NIA_NOTE, "nia likes tea.", "nia likes rust.", "nia ships."]
    for days_ago, content in enumerate(contents):
        await create_memory_note(
            db_session,
            guild_id=_GUILD,
            channel_id=_CHANNEL,
            content=content,
            created_at=_NOW - timedelta(days=30 - days_ago),
            day_start=_NOW - timedelta(days=30 - days_ago, hours=1),
        )
    await db_session.commit()
    agent = _ScriptedPurgeAgent()

    result = await purge_guild_memory(
        session_factory, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
    )

    assert result.model_calls == 3
    assert result.notes_reviewed == 5
    assert result.notes_dropped == 1
    assert [len(c.notes) for c in agent.contexts] == [2, 2, 1]
    db_session.expire_all()
    left = sorted(n.content for n in (await db_session.scalars(select(ChatAgentMemoryNote))).all())
    assert KAI_NOTE not in left and len(left) == 4


# -- model calls outside the lock, compare-and-set write (review answer A) ----------


async def test_the_model_runs_before_the_guild_lock_is_taken(
    db_session, session_factory, monkeypatch
):
    from smarter_dev.web import chat_memory_purge

    events: list[str] = []

    async def record_lock(session, guild_id):
        events.append("lock")

    monkeypatch.setattr(chat_memory_purge, "lock_guild_memory", record_lock)
    await _seed(db_session)

    async def mark():
        events.append("model")

    await purge_guild_memory(
        session_factory,
        guild_id=_GUILD,
        target=KAI,
        now=_NOW,
        agent=_ScriptedPurgeAgent(before_answer=mark),
    )

    assert events.count("lock") == 1
    assert events[-1] == "lock" and "model" in events


async def test_a_dream_during_the_purge_makes_it_redo_the_guild(db_session, session_factory):
    await _seed(db_session)
    dreamed = []

    async def dream_once():
        if dreamed:
            return
        dreamed.append(True)
        async with session_factory() as session:
            memory = await get_guild_memory_blob(session, _GUILD)
            memory.content = memory.content + "\n- a new line from tonight."
            memory.revision += 1
            await session.commit()

    agent = _ScriptedPurgeAgent(before_answer=dream_once)
    result = await purge_guild_memory(
        session_factory, guild_id=_GUILD, target=KAI, now=_NOW, agent=agent
    )

    assert result.outcome == "purged"
    db_session.expire_all()
    memory = await get_guild_memory_blob(db_session, _GUILD)
    # The dream's line survives: the purge redid the guild from it.
    assert "- a new line from tonight." in memory.content
    assert KAI_LINE not in memory.content


async def test_memory_that_keeps_changing_is_left_alone(db_session, session_factory):
    from smarter_dev.web.chat_memory_purge import PurgeConflict

    await _seed(db_session)

    async def dream_every_time():
        async with session_factory() as session:
            memory = await get_guild_memory_blob(session, _GUILD)
            memory.revision += 1
            await session.commit()

    before = await _snapshot(db_session)
    with pytest.raises(PurgeConflict):
        await purge_guild_memory(
            session_factory,
            guild_id=_GUILD,
            target=KAI,
            now=_NOW,
            agent=_ScriptedPurgeAgent(before_answer=dream_every_time),
        )
    after = await _snapshot(db_session)
    # Only the simulated dreams moved the revision; nothing of the purge landed.
    assert after[0][:3] == before[0][:3] and after[1:] == before[1:]


async def test_a_stopped_run_writes_nothing(db_session, session_factory):
    from smarter_dev.web.chat_memory_purge import PurgeStopped

    await _seed(db_session)
    before = await _snapshot(db_session)

    async def not_current(session):
        return False

    with pytest.raises(PurgeStopped):
        await purge_guild_memory(
            session_factory,
            guild_id=_GUILD,
            target=KAI,
            now=_NOW,
            agent=_ScriptedPurgeAgent(),
            still_current=not_current,
        )
    assert await _snapshot(db_session) == before


# -- the final notes pass ------------------------------------------------------------


async def test_the_final_notes_pass_reviews_new_and_mentioning_notes_only(
    db_session, session_factory
):
    from smarter_dev.web.chat_memory_purge import purge_guild_notes

    for content, created in (
        ("nia is old news.", _NOW - timedelta(days=3)),
        (KAI_NOTE, _NOW - timedelta(days=3)),
        ("someone new asked about embedded rust.", _NOW + timedelta(minutes=5)),
    ):
        await create_memory_note(
            db_session,
            guild_id=_GUILD,
            channel_id=_CHANNEL,
            content=content,
            created_at=created,
            day_start=created - timedelta(hours=1),
        )
    await db_session.commit()
    agent = _ScriptedPurgeAgent()

    result = await purge_guild_notes(
        session_factory, guild_id=_GUILD, target=KAI, now=_NOW, since=_NOW, agent=agent
    )

    reviewed = sorted(content for content_pair in agent.contexts for _, content in content_pair.notes)
    assert reviewed == sorted([KAI_NOTE, "someone new asked about embedded rust."])
    assert result.notes_dropped == 1


def test_a_segment_carrying_the_id_cannot_be_kept_even_with_a_reason():
    output = PurgeOutput(
        edits=[SegmentEdit(id=_first_id(), action="keep")],
        unresolved=[UnresolvedItem(location="memory", reason="shares a name")],
    )
    with pytest.raises(PurgeRefused):
        compose_purge(output, _context(), retries_left=0)
