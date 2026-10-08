"""Web-side writers of Discord-sourced memory honour the opt-out (#100).

The gate (``smarter_dev.web.privacy_gate``) reads the block list from the
database. A memory note about or from someone on it is never kept, and the
nightly dream neither reads notes about them nor rewrites or reintroduces
lines about them. Notes about them are consumed unread on the usual
schedule; opting out deletes nothing else. Each test has a negative control. Real SQLite tables, stub dream model, synthetic members only: kai
(opted out) and nia.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy import select

from smarter_dev.web.chat_bot_opt_out import opt_in
from smarter_dev.web.chat_bot_opt_out import opt_out
from smarter_dev.web.chat_memory_dream import DreamContext
from smarter_dev.web.chat_memory_dream import DreamOutcome
from smarter_dev.web.chat_memory_dream import DreamOutput
from smarter_dev.web.chat_memory_dream import run_guild_dream
from smarter_dev.web.crud import create_memory_note
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import upsert_guild_memory_blob
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.privacy_gate import load_gate

KAI = "111111111111111111"
NIA = "222222222222222222"
_GUILD = "123456789012345678"
_CHANNEL = "555000111222333444"
_CUTOFF = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
_MORNING = _CUTOFF - timedelta(hours=15)


async def _note(session, content: str, *, about=()) -> ChatAgentMemoryNote | None:
    return await create_memory_note(
        session,
        guild_id=_GUILD,
        channel_id=_CHANNEL,
        channel_name="dev-help",
        content=content,
        created_at=_MORNING,
        day_start=_MORNING.replace(hour=0),
        about_user_ids=about,
    )


async def _note_texts(session) -> list[str]:
    return sorted((await session.scalars(select(ChatAgentMemoryNote.content))).all())


# -- the gate -------------------------------------------------------------------


async def test_the_gate_answers_from_the_database_as_it_is_now(db_session):
    assert not (await load_gate(db_session)).refuses(KAI)

    await opt_out(db_session, KAI)
    gate = await load_gate(db_session)
    assert gate.refuses(KAI) and not gate.refuses(NIA, None, "")
    assert gate.redact(f"<@{KAI}> and {NIA}") == f"@[blocked user] and {NIA}"

    await opt_in(db_session, KAI)
    assert not (await load_gate(db_session)).refuses(KAI)


# -- memory notes ------------------------------------------------------------------


async def test_a_note_about_an_opted_out_member_is_never_kept(db_session):
    await opt_out(db_session, KAI)

    assert await _note(db_session, f"kai (id {KAI}) loves shaders") is None
    assert await _note(db_session, f"<@{KAI}> wound me up") is None
    assert await _note(db_session, "a quiet day", about=[NIA, KAI]) is None
    assert await _note_texts(db_session) == []

    # Controls: the same notes about someone not on the list are kept.
    assert await _note(db_session, f"nia (id {NIA}) loves shaders") is not None
    assert await _note(db_session, "a quiet day", about=[NIA]) is not None
    assert len(await _note_texts(db_session)) == 2


# -- the dream -------------------------------------------------------------------


@dataclass
class _Dream:
    """A stub dream model that records what it was shown."""

    memory: str
    behavior: str | None = None
    prompts: list[str] = field(default_factory=list)
    contexts: list[DreamContext] = field(default_factory=list)

    async def run(self, user_prompt: str, *, deps: DreamContext):
        self.prompts.append(user_prompt)
        self.contexts.append(deps)
        output = DreamOutput(memory=self.memory, behavior=self.behavior)
        return type("Result", (), {"output": output})()


async def _seed(db_session, *, blob: str, behavior: str = "") -> None:
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content=blob,
        behavior=behavior,
        personality="",
        notes_consumed=0,
        model_name="stub",
        dreamed_at=_CUTOFF - timedelta(days=1),
    )
    # Written while nobody had opted out.
    await _note(db_session, f"kai (id {KAI}) was quiet today")
    await _note(db_session, f"nia (id {NIA}) shipped the jam build")
    await db_session.commit()


async def _dream(db_session, agent):
    return await run_guild_dream(
        db_session,
        guild_id=_GUILD,
        cutoff=_CUTOFF,
        dreamed_at=_CUTOFF + timedelta(minutes=20),
        agent=agent,
    )


_BLOB = f"## People\n- kai (id {KAI}) loves shaders\n- nia (id {NIA}) runs the jam"


async def test_the_dream_reads_nothing_about_an_opted_out_member(db_session):
    await _seed(db_session, blob=_BLOB, behavior=f"Tease kai ({KAI}) gently.\nBe brief.")
    await opt_out(db_session, KAI)
    agent = _Dream(memory=f"## People\n- nia (id {NIA}) shipped the jam build")

    result = await _dream(db_session, agent)

    assert result.outcome is DreamOutcome.DREAMED
    (prompt,) = agent.prompts
    assert KAI not in prompt and "was quiet today" not in prompt and "loves shaders" not in prompt
    assert "shipped the jam build" in prompt and "runs the jam" in prompt
    assert KAI not in agent.contexts[0].previous_blob
    assert all(KAI not in note for note in agent.contexts[0].notes)

    # Their line is carried over byte for byte, not rewritten or deleted ...
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert f"- kai (id {KAI}) loves shaders" in memory.content
    assert memory.content.startswith(f"## People\n- nia (id {NIA}) shipped the jam build")
    # ... and a block the model left alone comes back exactly as stored.
    assert memory.behavior == f"Tease kai ({KAI}) gently.\nBe brief."
    # The note about them was not read, but it is consumed with the rest.
    assert await _note_texts(db_session) == []


async def test_without_an_opt_out_the_dream_reads_everything(db_session):
    """Control for the test above: same seed, nobody on the list."""
    await _seed(db_session, blob=_BLOB)
    agent = _Dream(memory=f"## People\n- kai (id {KAI}) and nia (id {NIA}) both shipped")

    result = await _dream(db_session, agent)

    assert result.outcome is DreamOutcome.DREAMED
    assert "was quiet today" in agent.prompts[0] and "loves shaders" in agent.prompts[0]
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert memory.content == f"## People\n- kai (id {KAI}) and nia (id {NIA}) both shipped"
    assert await _note_texts(db_session) == []


async def test_a_dream_that_reintroduces_an_opted_out_id_is_not_saved(db_session):
    await _seed(db_session, blob=_BLOB)
    await opt_out(db_session, KAI)
    agent = _Dream(memory=f"## People\n- kai (id {KAI}) is back")

    result = await _dream(db_session, agent)

    assert result.outcome is DreamOutcome.KEPT_PREVIOUS
    assert (await get_guild_memory_blob(db_session, _GUILD)).content == _BLOB
    assert len(await _note_texts(db_session)) == 2  # nothing consumed


async def test_a_night_with_only_notes_about_opted_out_members_calls_no_model(db_session):
    await _seed(db_session, blob=_BLOB)
    await opt_out(db_session, KAI)
    await opt_out(db_session, NIA)
    agent = _Dream(memory="unused")

    result = await _dream(db_session, agent)

    assert result.outcome is DreamOutcome.SKIPPED_NO_NOTES
    assert agent.prompts == []
    # Consumed unread, so they do not pile up night after night.
    assert await _note_texts(db_session) == []
    assert (await get_guild_memory_blob(db_session, _GUILD)).content == _BLOB


def _column_limits() -> dict[str, int]:
    from smarter_dev.web.models import ChatAgentGuildMemory

    columns = ChatAgentGuildMemory.__table__.c
    return {name: columns[name].type.length for name in ("content", "behavior", "personality")}


async def test_carried_over_lines_still_fit_the_columns(db_session):
    """SQLite does not enforce String(n); Postgres does. A carried-over line
    must not push a block past its column on the night the model writes up to
    the full cap."""
    limits = _column_limits()
    kai_line = f"- kai (id {KAI}) " + "loves shaders " * 40
    await _seed(
        db_session,
        blob=f"## People\n{kai_line}\n- nia (id {NIA}) runs the jam",
        behavior=f"Tease kai ({KAI}) " + "gently " * 50 + "\nBe brief.",
    )
    await opt_out(db_session, KAI)
    full = "\n".join(f"- nia (id {NIA}) shipped build {i}" for i in range(200))
    agent = _Dream(memory=full[: limits["content"]], behavior="Be brief. Be kind.")

    result = await _dream(db_session, agent)

    assert result.outcome is DreamOutcome.DREAMED
    assert agent.contexts[0].memory_limit < limits["content"]  # room was kept
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert kai_line in memory.content and len(memory.content) <= limits["content"]
    assert f"Tease kai ({KAI})" in memory.behavior
    assert len(memory.behavior) <= limits["behavior"]
    assert len(memory.personality) <= limits["personality"]


async def test_without_an_opt_out_the_model_gets_the_whole_cap(db_session):
    """Control for the test above: nothing withheld, nothing reserved."""
    from smarter_dev.web.models import MAX_BEHAVIOR_CHARS
    from smarter_dev.web.models import MAX_MEMORY_BLOB_CHARS
    from smarter_dev.web.models import MAX_PERSONALITY_CHARS

    await _seed(db_session, blob=_BLOB)
    agent = _Dream(memory=f"## People\n- nia (id {NIA}) shipped")

    await _dream(db_session, agent)

    context = agent.contexts[0]
    assert (context.memory_limit, context.behavior_limit, context.personality_limit) == (
        MAX_MEMORY_BLOB_CHARS,
        MAX_BEHAVIOR_CHARS,
        MAX_PERSONALITY_CHARS,
    )


def test_the_retry_asks_for_the_room_left_not_the_column():
    import pytest
    from pydantic_ai import ModelRetry

    from smarter_dev.web.chat_memory_dream import compose_blocks

    context = DreamContext(previous_blob="", notes=["a note"], memory_limit=100)
    with pytest.raises(ModelRetry, match="100-character"):
        compose_blocks(DreamOutput(memory="x" * 101), context, retries_left=1)
    blocks = compose_blocks(DreamOutput(memory="line\n" * 30), context, retries_left=0)
    assert len(blocks.memory) <= 100


# -- handler memories ---------------------------------------------------------------


async def test_handler_memory_keeps_nothing_new_about_an_opted_out_member(db_session):
    from smarter_dev.web.handler_memory import persist_handler_memory
    from smarter_dev.web.models import ChannelHandler

    handler = ChannelHandler(
        guild_id=_GUILD,
        channel_id=_CHANNEL,
        name="counter",
        trigger_type="message",
        settings={},
        description="counts",
        script="pass",
        created_by=NIA,
        memory={f"seen:{KAI}": 1},
    )
    db_session.add(handler)
    await db_session.flush()
    await opt_out(db_session, KAI)

    await persist_handler_memory(
        db_session,
        ChannelHandler,
        handler.id,
        {
            f"seen:{KAI}": 2,
            f"warn:{KAI}": 1,
            f"seen:{NIA}": 1,
            "last": f"<@{KAI}> said hi to <@{NIA}>",
        },
        changed=True,
    )

    assert handler.memory == {
        f"seen:{KAI}": 1,  # what was stored stays; nothing new is written
        f"seen:{NIA}": 1,  # control
        "last": f"@[blocked user] said hi to <@{NIA}>",
    }


async def test_guild_handler_memory_keeps_nothing_new_about_an_opted_out_member(
    db_session,
):
    from smarter_dev.web.handler_guild_memory import load_guild_memory
    from smarter_dev.web.handler_guild_memory import persist_guild_memory

    await persist_guild_memory(db_session, _GUILD, {f"warn:{KAI}": 1}, [])
    await opt_out(db_session, KAI)

    await persist_guild_memory(
        db_session,
        _GUILD,
        {
            f"warn:{KAI}": 2,
            f"warn:{NIA}": 1,
            "roster": [KAI, NIA],
            "ids": [int(KAI), int(NIA)],
        },
        [],
    )

    assert await load_guild_memory(db_session, _GUILD) == {
        f"warn:{KAI}": 1,
        f"warn:{NIA}": 1,
        "roster": ["[blocked user]", NIA],
        "ids": ["[blocked user]", int(NIA)],
    }
