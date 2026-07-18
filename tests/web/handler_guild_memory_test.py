"""Tests for guild-scoped shared memory — the GuildMemory store, its per-key
change tracking, the load/persist DB round-trip, and the guild_memory_* external
functions wired through the runtime (admin handlers only)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from smarter_dev.web.admin_actions import AdminActor
from smarter_dev.web.handler_budget import CapExceeded
from smarter_dev.web.handler_guild_memory import (
    GuildMemory,
    load_guild_memory,
    persist_guild_memory,
)
from smarter_dev.web.handler_runtime import run_handler_script


# -- GuildMemory unit ----------------------------------------------------------


def test_set_get_roundtrip_and_dirty():
    mem = GuildMemory()
    assert mem.dirty is False
    assert mem.get("x") is None
    assert mem.get("x", 0) == 0
    mem.set("x", 5)
    assert mem.get("x") == 5
    assert mem.dirty is True


def test_seeds_from_initial_without_marking_dirty():
    mem = GuildMemory({"count": 3})
    assert mem.get("count") == 3
    assert mem.dirty is False  # loading prior state isn't a change
    assert mem.writes() == {}
    assert mem.deletes() == []


def test_delete_returns_whether_removed():
    mem = GuildMemory({"a": 1})
    assert mem.delete("missing") is False
    assert mem.dirty is False
    assert mem.delete("a") is True
    assert mem.dirty is True
    assert mem.get("a") is None


def test_all_returns_independent_deep_copy():
    mem = GuildMemory({"nested": {"k": [1]}})
    snap = mem.all()
    snap["nested"]["k"].append(2)
    assert mem.get("nested") == {"k": [1]}


def test_non_serializable_value_raises_and_leaves_state_intact():
    mem = GuildMemory({"keep": 1})
    with pytest.raises(ValueError):
        mem.set("bad", {1, 2, 3})  # a set isn't JSON
    assert mem.get("bad") is None
    assert mem.get("keep") == 1
    assert mem.dirty is False  # rejected write doesn't dirty the store


def test_whole_store_size_cap_raises_guild_memory_size():
    mem = GuildMemory(max_bytes=64)
    with pytest.raises(CapExceeded) as exc:
        mem.set("big", "x" * 200)
    assert exc.value.cap == "guild_memory_size"
    assert mem.dirty is False


def test_key_longer_than_column_raises_before_persist():
    # The key column is VARCHAR(64); an over-long key must fail loud at set()
    # time (a soft CapExceeded the script can catch), not blow up the audit
    # commit later with a Postgres value-too-long error.
    mem = GuildMemory({"keep": 1})
    with pytest.raises(CapExceeded) as exc:
        mem.set("x" * 65, 1)
    assert exc.value.cap == "guild_memory_key_size"
    assert mem.dirty is False  # rejected write doesn't dirty the store
    assert mem.get("x" * 65) is None
    # A key exactly at the limit is accepted.
    mem.set("x" * 64, 2)
    assert mem.get("x" * 64) == 2


def test_writes_and_deletes_track_touched_keys_only():
    mem = GuildMemory({"seed": 1})
    mem.set("a", 10)
    mem.set("b", 20)
    assert mem.writes() == {"a": 10, "b": 20}  # seed untouched -> not rewritten
    assert mem.deletes() == []

    # set-then-delete on the same key leaves it ONLY in deletes.
    mem.delete("a")
    assert "a" not in mem.writes()
    assert "a" in mem.deletes()

    # delete-then-set on the same key leaves it ONLY in writes.
    mem.delete("seed")
    assert "seed" in mem.deletes()
    mem.set("seed", 2)
    assert mem.writes()["seed"] == 2
    assert "seed" not in mem.deletes()


# -- load / persist DB round-trip ---------------------------------------------


async def test_persist_and_load_roundtrip(db_session):
    await persist_guild_memory(db_session, "G1", {"k": {"v": 1}}, [])
    await db_session.commit()
    assert await load_guild_memory(db_session, "G1") == {"k": {"v": 1}}


async def test_persist_upserts_and_deletes_per_key(db_session):
    await persist_guild_memory(db_session, "G1", {"a": 1, "b": 2}, [])
    await db_session.commit()
    # Update a, delete b, add c — only touched keys change.
    await persist_guild_memory(db_session, "G1", {"a": 9, "c": 3}, ["b"])
    await db_session.commit()
    assert await load_guild_memory(db_session, "G1") == {"a": 9, "c": 3}


async def test_load_is_scoped_to_the_guild(db_session):
    await persist_guild_memory(db_session, "G1", {"a": 1}, [])
    await persist_guild_memory(db_session, "G2", {"a": 2}, [])
    await db_session.commit()
    assert await load_guild_memory(db_session, "G1") == {"a": 1}
    assert await load_guild_memory(db_session, "G2") == {"a": 2}


async def test_persist_first_writes_a_key_that_already_exists_upserts(db_session):
    # A concurrent fire may have inserted the key after this fire loaded its
    # (empty) snapshot, so this fire treats the key as new. The persist must
    # upsert last-write-wins via ON CONFLICT, never raise a UNIQUE IntegrityError.
    await persist_guild_memory(db_session, "G1", {"k": "first"}, [])
    await db_session.commit()
    await persist_guild_memory(db_session, "G1", {"k": "second"}, [])
    await db_session.commit()
    assert await load_guild_memory(db_session, "G1") == {"k": "second"}


# -- runtime integration -------------------------------------------------------


@dataclass
class _FakeEmitter:
    messages: list = field(default_factory=list)

    async def create_message(
        self, channel_id: str, content: str, ping_role_id: str | None = None
    ) -> str:
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"

    async def add_reaction(self, channel_id, message_id, emoji) -> None:
        pass


@dataclass
class _StubLimiter:
    async def hit(self, key: str, limit: int) -> bool:
        return True


def _admin_actor() -> AdminActor:
    return AdminActor(bot_token="tok", guild_id="G1")


async def _run(script, *, actor=None, guild_memory=None):
    return await run_handler_script(
        script,
        {"trigger_type": "message", "message_content": "hi"},
        channel_id="C1",
        guild_id="G1",
        emitter=_FakeEmitter(),
        limiter=_StubLimiter(),
        actor=actor,
        guild_memory=guild_memory,
    )


async def test_guild_memory_functions_absent_for_standard_handler():
    # No actor -> the guild_memory_* names are undefined in the sandbox.
    script = 'await guild_memory_set("x", 1)\n'
    r = await _run(script, actor=None)
    assert r.outcome == "error"
    assert r.guild_memory_changed is False


async def test_guild_memory_persists_across_an_admin_fire():
    script = (
        'n = await guild_memory_get("count", 0)\n'
        'await guild_memory_set("count", n + 1)\n'
    )
    r1 = await _run(script, actor=_admin_actor(), guild_memory={})
    assert r1.outcome == "ok"
    assert r1.guild_memory_changed is True
    assert r1.guild_memory_writes == {"count": 1}
    assert r1.guild_memory_deletes == []

    # Second fire seeded with the saved store.
    r2 = await _run(script, actor=_admin_actor(), guild_memory={"count": 1})
    assert r2.guild_memory_writes == {"count": 2}


async def test_guild_memory_delete_surfaces_in_result_deletes():
    script = 'await guild_memory_delete("gone")\n'
    r = await _run(script, actor=_admin_actor(), guild_memory={"gone": 1, "keep": 2})
    assert r.outcome == "ok"
    assert r.guild_memory_deletes == ["gone"]
    assert r.guild_memory_writes == {}
    assert r.guild_memory_changed is True


async def test_guild_memory_all_snapshot_from_script():
    script = (
        'await guild_memory_set("a", 1)\n'
        'snap = await guild_memory_all()\n'
        'await send_message(str(sorted(snap.keys())))\n'
    )
    r = await _run(script, actor=_admin_actor(), guild_memory={"seed": 0})
    assert r.outcome == "ok"
    assert r.guild_memory_writes == {"a": 1}


async def test_guild_memory_size_cap_surfaces_as_cap_exceeded():
    script = 'await guild_memory_set("blob", "x" * 20000)\n'
    r = await _run(script, actor=_admin_actor(), guild_memory={})
    assert r.outcome == "cap_exceeded"
    assert r.cap == "guild_memory_size"
    assert r.guild_memory_changed is False
