"""The one-off rewrite of stored memory into member tags (#104)."""

from __future__ import annotations

import importlib.util
import logging
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from smarter_dev.shared.member_tags import retag
from smarter_dev.web.chat_memory_retag import RETAG_MODEL_NAME
from smarter_dev.web.chat_memory_retag import apply_guild
from smarter_dev.web.chat_memory_retag import plan_guild
from smarter_dev.web.chat_memory_retag import retag_text
from smarter_dev.web.crud import create_memory_note
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import list_memory_revisions
from smarter_dev.web.crud import upsert_guild_memory_blob
from smarter_dev.web.models import MAX_BEHAVIOR_CHARS
from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentMemoryNote

REPO = Path(__file__).resolve().parents[2]
KAI = "111111111111111111"
NIA = "222222222222222222"
MAL = "333333333333333333"
_GUILD = "123456789012345678"
_CHANNEL = "555000111222333444"
_DREAMED = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)


# -- the converter --------------------------------------------------------------


def test_the_old_form_becomes_a_tag():
    assert retag(f"- kai (id {KAI}) loves shaders, NIA(id 7) too") == (
        f"- <{KAI}:kai> loves shaders, <7:NIA> too",
        2,
    )


def test_a_mention_becomes_a_tag_only_when_its_name_is_known():
    text = f"<@{KAI}> and <@!{NIA}> and kai (id {KAI})"

    assert retag_text(text, {}) == (
        f"<{KAI}:kai> and <@!{NIA}> and <{KAI}:kai>",
        2,
    )
    assert retag_text(text, {NIA: "nia"})[0] == (
        f"<{KAI}:kai> and <{NIA}:nia> and <{KAI}:kai>"
    )


def test_the_texts_own_name_beats_the_engagement_name():
    assert retag_text(f"<@{KAI}> is kai (id {KAI})", {KAI: "old"})[0] == (
        f"<{KAI}:kai> is <{KAI}:kai>"
    )


def test_tags_are_left_alone_and_none_is_invented():
    text = f"<{KAI}:kai>, <:sam> and plain kai, <:party:123>"

    assert retag_text(text, {KAI: "kai"}) == (text, 0)


def test_a_second_pass_converts_nothing():
    once, count = retag_text(f"kai (id {KAI}) met <@{NIA}>", {NIA: "nia"})

    assert count == 2
    assert retag_text(once, {NIA: "nia"}) == (once, 0)


# -- one guild ------------------------------------------------------------------


async def _seed(db_session, *, behavior: str = f"Answer kai (id {KAI}) first."):
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content=f"## People\n- kai (id {KAI}) loves shaders\n- <@{MAL}> lurks",
        behavior=behavior,
        personality="Dry and warm.",
        notes_consumed=0,
        model_name="stub",
        dreamed_at=_DREAMED,
    )
    note = await create_memory_note(
        db_session,
        guild_id=_GUILD,
        channel_id=_CHANNEL,
        channel_name="dev-help",
        content=f"nia (id {NIA}) shipped; kai cheered",
        created_at=_DREAMED + timedelta(hours=9),
        day_start=_DREAMED,
    )
    db_session.add(
        ChatAgentEngagement(
            guild_id=_GUILD,
            channel_id=_CHANNEL,
            activation_user_id=MAL,
            activation_username="mallory",
            activation_message_id="444444444444444444",
        )
    )
    await db_session.commit()
    return note.id


async def test_the_dry_run_counts_and_writes_nothing(db_session):
    note_id = await _seed(db_session)

    plan = await plan_guild(db_session, _GUILD)
    await db_session.rollback()

    assert plan.converted == {"content": 2, "behavior": 1, "personality": 0, "notes": 1}
    # "kai cheered" in the note is still a known name outside a tag.
    assert plan.untagged == {"content": 0, "behavior": 0, "personality": 0, "notes": 1}
    assert not plan.refused
    db_session.expire_all()
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert f"kai (id {KAI})" in memory.content and memory.revision == 1
    assert (await db_session.get(ChatAgentMemoryNote, note_id)).content.startswith(
        "nia (id"
    )


async def test_apply_rewrites_records_a_revision_and_is_idempotent(db_session):
    note_id = await _seed(db_session)

    plan = await apply_guild(db_session, _GUILD)

    assert plan.total == 4
    db_session.expire_all()
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert memory.content == (
        f"## People\n- <{KAI}:kai> loves shaders\n- <{MAL}:mallory> lurks"
    )
    assert memory.behavior == f"Answer <{KAI}:kai> first."
    assert memory.personality == "Dry and warm."
    assert memory.revision == 2
    assert memory.last_dream_at.replace(tzinfo=UTC) == _DREAMED
    note_row = await db_session.get(ChatAgentMemoryNote, note_id)
    assert note_row.content == f"<{NIA}:nia> shipped; kai cheered"
    revisions = await list_memory_revisions(db_session, _GUILD)
    assert revisions[0].revision == 2 and revisions[0].model_name == RETAG_MODEL_NAME
    assert revisions[0].content == memory.content

    again = await apply_guild(db_session, _GUILD)
    assert again.total == 0
    db_session.expire_all()
    assert (await get_guild_memory_blob(db_session, _GUILD)).revision == 2


async def test_a_guild_that_would_go_over_a_cap_is_refused_whole(db_session):
    # Each `x (id 7)` (8 chars) becomes `<7:x>` (5); `<@7>` becomes `<7:x>` (+1).
    fills = f"kai (id {KAI}) " + f"<@{MAL}>" * 200
    behavior = fills[: MAX_BEHAVIOR_CHARS - 2]
    behavior = behavior[: behavior.rfind(">") + 1]
    note_id = await _seed(db_session, behavior=behavior)
    assert len(behavior) <= MAX_BEHAVIOR_CHARS
    assert len(retag_text(behavior, {MAL: "mallory"})[0]) > MAX_BEHAVIOR_CHARS

    plan = await apply_guild(db_session, _GUILD)

    assert plan.refused and plan.over_cap == ["behavior"]
    db_session.expire_all()
    memory = await get_guild_memory_blob(db_session, _GUILD)
    assert memory.behavior == behavior and memory.revision == 1
    assert f"kai (id {KAI})" in memory.content
    assert (await db_session.get(ChatAgentMemoryNote, note_id)).content.startswith(
        "nia (id"
    )


async def test_a_guild_with_only_notes_is_rewritten(db_session):
    await create_memory_note(
        db_session,
        guild_id=_GUILD,
        channel_id=_CHANNEL,
        channel_name="dev-help",
        content=f"kai (id {KAI}) shipped",
        created_at=_DREAMED,
        day_start=_DREAMED,
    )
    await db_session.commit()

    plan = await apply_guild(db_session, _GUILD)

    assert plan.converted["notes"] == 1
    db_session.expire_all()
    notes = (await db_session.scalars(select(ChatAgentMemoryNote))).all()
    assert [n.content for n in notes] == [f"<{KAI}:kai> shipped"]
    assert await get_guild_memory_blob(db_session, _GUILD) is None


# -- the script and its Job -------------------------------------------------------


def _script():
    path = REPO / "scripts" / "retag_chat_agent_memory.py"
    spec = importlib.util.spec_from_file_location("retag_chat_agent_memory", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_script_is_a_dry_run_by_default_and_logs_counts_only(
    db_session, monkeypatch, caplog
):
    await _seed(db_session)
    script = _script()

    @asynccontextmanager
    async def factory():
        yield db_session

    monkeypatch.setattr(script, "get_db_session_context", factory)
    with caplog.at_level(logging.INFO, logger="retag_chat_agent_memory"):
        assert await script.main([]) == 0

    db_session.expire_all()
    assert (await get_guild_memory_blob(db_session, _GUILD)).revision == 1
    logged = caplog.text
    assert "would rewrite" in logged and "content=2" in logged
    for private in (KAI, NIA, MAL, "kai", "nia", "mallory", "shaders"):
        assert private not in logged

    with caplog.at_level(logging.INFO, logger="retag_chat_agent_memory"):
        assert await script.main(["--apply"]) == 0
    db_session.expire_all()
    assert (await get_guild_memory_blob(db_session, _GUILD)).revision == 2


async def test_the_script_fails_an_apply_that_refused_a_guild(db_session, monkeypatch):
    behavior = (f"<@{MAL}>" * 200)[:MAX_BEHAVIOR_CHARS]
    behavior = behavior[: behavior.rfind(">") + 1]
    await _seed(db_session, behavior=behavior)
    script = _script()

    @asynccontextmanager
    async def factory():
        yield db_session

    monkeypatch.setattr(script, "get_db_session_context", factory)

    assert await script.main([]) == 0
    assert await script.main(["--apply"]) == 1


def test_the_job_runs_the_script_from_the_web_image():
    manifest = yaml.safe_load(
        (REPO / "k8s" / "oneoff-retag-chat-agent-memory.yaml").read_text()
    )
    spec = manifest["spec"]
    container = spec["template"]["spec"]["containers"][0]

    assert manifest["kind"] == "Job" and spec["backoffLimit"] == 0
    assert container["image"] == "zzmmrmn/smarter-dev-website:<IMAGE_VERSION>"
    assert "scripts/retag_chat_agent_memory.py <ARGS>" in container["command"][-1]


async def test_the_script_takes_one_mode():
    with pytest.raises(SystemExit):
        await _script().main(["--dry-run", "--apply"])
