"""Tests for the agent-run dream endpoints.

Run against a real session: the point is that the shared validation refuses
what the cron's would, writes nothing when it does, and spends the notes only
on an accepted night.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient
from litestar.testing import create_test_client
from sqlalchemy import select

from smarter_dev.web.api_native import chat_memory as chat_memory_module
from smarter_dev.web.api_native import chat_memory_dream as module
from smarter_dev.web.chat_bot_opt_out import opt_out
from smarter_dev.web.chat_memory_dream import dream_cutoff
from smarter_dev.web.crud import create_memory_note
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import upsert_guild_memory_blob
from smarter_dev.web.models import ChatAgentMemoryNote

_GUILD = "123456789012345678"
_NOW = datetime(2026, 8, 7, 0, 20, tzinfo=UTC)
_CUTOFF = dream_cutoff(_NOW)
_BASE = f"/api/guilds/{_GUILD}/chat-memory/dream"


@pytest.fixture
def client(db_session) -> Iterator[TestClient]:
    original = list(chat_memory_module.BOT_API_GUARDS)
    chat_memory_module.BOT_API_GUARDS.clear()
    try:
        with (
            patch.object(module, "datetime", wraps=datetime) as clock,
            create_test_client(
                route_handlers=[module.ChatMemoryDreamController],
                plugins=[PydanticPlugin()],
                dependencies={
                    "db_session": Provide(lambda: db_session, sync_to_thread=False)
                },
            ) as test_client,
        ):
            clock.now.return_value = _NOW
            yield test_client
    finally:
        chat_memory_module.BOT_API_GUARDS[:] = original


async def _seed(db_session, *, notes=("the shader shipped.",)):
    await upsert_guild_memory_blob(
        db_session,
        guild_id=_GUILD,
        content="## Who's here\nthe shader crowd",
        notes_consumed=1,
        model_name="stub",
        dreamed_at=_CUTOFF - timedelta(days=1),
    )
    for text in notes:
        created = _CUTOFF - timedelta(hours=5)
        await create_memory_note(
            db_session,
            guild_id=_GUILD,
            channel_id="555000111222333444",
            channel_name="dev-help",
            content=text,
            created_at=created,
            day_start=created.replace(hour=0, minute=0),
        )
    await db_session.commit()


def _body(inputs, **overrides):
    body = {
        "cutoff": inputs["cutoff"],
        "base_revision": inputs["base_revision"],
        "note_ids": [n["id"] for n in inputs["notes"]],
        "inputs_digest": inputs["inputs_digest"],
        "memory": "## Who's here\nthe shader crowd ships.",
    }
    body.update(overrides)
    return body


async def test_inputs_carry_blocks_notes_and_limits(client, db_session):
    await _seed(db_session)

    inputs = client.get(_BASE).json()

    assert inputs["needed"] is True
    assert inputs["base_revision"] == 1
    assert inputs["limits"]["facts"] == 1000
    assert len(inputs["notes"]) == 1
    assert "the shader shipped." in inputs["user_message"]


async def test_a_guild_with_a_memory_row_and_no_notes_is_needed(client, db_session):
    await _seed(db_session, notes=())

    assert _GUILD in client.get("/api/chat-memory/dream/guilds").json()["guild_ids"]
    inputs = client.get(_BASE).json()

    assert inputs["needed"] is True
    assert inputs["notes"] == []
    assert "You kept no notes today" in inputs["user_message"]
    result = client.post(_BASE, json=_body(inputs, facts="Server is Pine.")).json()
    assert result["accepted"] is True
    # Dreamed tonight, so it is not offered again.
    assert _GUILD not in client.get("/api/chat-memory/dream/guilds").json()["guild_ids"]


async def test_a_no_notes_dream_goes_stale_when_a_note_arrives(client, db_session):
    await _seed(db_session, notes=())
    inputs = client.get(_BASE).json()
    created = _CUTOFF - timedelta(hours=1)
    await create_memory_note(
        db_session,
        guild_id=_GUILD,
        channel_id="555000111222333444",
        channel_name="dev-help",
        content="a late note",
        created_at=created,
        day_start=created.replace(hour=0, minute=0),
    )
    await db_session.commit()

    result = client.post(_BASE, json=_body(inputs)).json()

    assert (result["accepted"], result["reason"]) == (False, "stale")


async def test_a_guild_with_no_memory_row_and_no_notes_is_not_needed(client, db_session):
    inputs = client.get(_BASE).json()

    assert inputs["needed"] is False
    assert inputs["reason"] == "no_notes"


async def test_submit_stores_facts_and_spends_the_notes(client, db_session):
    await _seed(db_session)
    inputs = client.get(_BASE).json()

    result = client.post(_BASE, json=_body(inputs, facts="Server is Pine.")).json()

    assert result["accepted"] is True, result
    assert result["notes_consumed"] == 1
    db_session.expire_all()
    assert (await get_guild_memory_blob(db_session, _GUILD)).facts == "Server is Pine."
    assert not (await db_session.scalars(select(ChatAgentMemoryNote.id))).all()


async def test_over_limit_facts_are_refused_and_write_nothing(client, db_session):
    await _seed(db_session)
    inputs = client.get(_BASE).json()

    result = client.post(_BASE, json=_body(inputs, facts="x" * 1001)).json()

    assert result["accepted"] is False
    assert result["reason"] == "invalid"
    assert "1000" in result["message"]
    db_session.expire_all()
    assert (await get_guild_memory_blob(db_session, _GUILD)).revision == 1
    assert (await db_session.scalars(select(ChatAgentMemoryNote.id))).all()


async def test_a_moved_revision_is_stale(client, db_session):
    await _seed(db_session)
    inputs = client.get(_BASE).json()

    result = client.post(_BASE, json=_body(inputs, base_revision=0)).json()

    assert result["accepted"] is False
    assert result["reason"] == "stale"


async def test_a_different_note_set_is_stale(client, db_session):
    await _seed(db_session)
    inputs = client.get(_BASE).json()

    result = client.post(_BASE, json=_body(inputs, note_ids=[])).json()

    assert result["reason"] == "stale"


async def test_a_note_rewritten_in_place_between_read_and_submit_is_stale(
    client, db_session
):
    """A purge rewrites note text without moving the revision or the ids."""
    await _seed(db_session)
    inputs = client.get(_BASE).json()
    note = (await db_session.scalars(select(ChatAgentMemoryNote))).one()
    note.content = "a member shipped the shader."
    await db_session.commit()

    result = client.post(_BASE, json=_body(inputs, facts="Server is Pine.")).json()

    assert result["accepted"] is False
    assert result["reason"] == "stale"
    db_session.expire_all()
    assert (await get_guild_memory_blob(db_session, _GUILD)).revision == 1
    assert (await db_session.scalars(select(ChatAgentMemoryNote.id))).all()


async def test_a_missing_digest_is_refused(client, db_session):
    await _seed(db_session)
    inputs = client.get(_BASE).json()
    body = _body(inputs)
    del body["inputs_digest"]

    assert client.post(_BASE, json=body).status_code == 422


async def test_the_digest_is_stable_for_unchanged_inputs(client, db_session):
    await _seed(db_session)

    first = client.get(_BASE).json()["inputs_digest"]

    assert first and client.get(_BASE).json()["inputs_digest"] == first


async def test_an_old_cutoff_is_stale(client, db_session):
    await _seed(db_session)
    inputs = client.get(_BASE).json()
    old = (_CUTOFF - timedelta(days=5)).isoformat()

    assert (
        client.post(_BASE, json=_body(inputs, cutoff=old)).json()["reason"] == "stale"
    )


async def test_guild_list_names_guilds_with_notes(client, db_session):
    await _seed(db_session)

    listed = client.get("/api/chat-memory/dream/guilds").json()

    assert listed["guild_ids"] == [_GUILD]


def test_dream_routes_reject_calls_without_the_bot_token():
    """The real BOT_API_GUARDS, not the emptied list the other tests use."""
    with create_test_client(
        route_handlers=[module.ChatMemoryDreamController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: None, sync_to_thread=False)},
    ) as guarded:
        assert guarded.get("/api/chat-memory/dream/guilds").status_code == 401
        assert guarded.get(_BASE).status_code == 401
        assert guarded.post(_BASE, json={}).status_code == 401


_OPTED_OUT = "222222222222222222"


async def test_facts_about_an_opted_out_member_are_withheld_and_carried_over(
    client, db_session
):
    await _seed(db_session)
    memory = await get_guild_memory_blob(db_session, _GUILD)
    memory.facts = f"Server is Pine.\n<{_OPTED_OUT}:bob> owns the build box."
    await opt_out(db_session, _OPTED_OUT)
    await db_session.commit()

    inputs = client.get(_BASE).json()

    assert "Server is Pine." in inputs["user_message"]
    assert "build box" not in inputs["user_message"]
    result = client.post(_BASE, json=_body(inputs, facts="Server is Oak.")).json()
    assert result["accepted"] is True, result
    db_session.expire_all()
    stored = (await get_guild_memory_blob(db_session, _GUILD)).facts
    assert stored == f"Server is Oak.\n<{_OPTED_OUT}:bob> owns the build box."


async def test_an_opt_out_between_read_and_submit_is_stale(client, db_session):
    await _seed(db_session)
    memory = await get_guild_memory_blob(db_session, _GUILD)
    memory.facts = f"Server is Pine.\n<{_OPTED_OUT}:bob> owns the build box."
    await db_session.commit()
    inputs = client.get(_BASE).json()
    # The line was shown to the model; the member opts out before the answer.
    assert "build box" in inputs["user_message"]
    await opt_out(db_session, _OPTED_OUT)
    await db_session.commit()

    result = client.post(_BASE, json=_body(inputs, facts="Server is Oak.")).json()

    assert result["accepted"] is False
    assert result["reason"] == "stale"
    db_session.expire_all()
    assert "Server is Pine." in (await get_guild_memory_blob(db_session, _GUILD)).facts
