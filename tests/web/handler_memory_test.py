"""Tests for per-handler persistent memory — the HandlerMemory store and the
memory_* external functions wired through the runtime."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from smarter_dev.web.handler_budget import CapExceeded
from smarter_dev.web.handler_memory import HandlerMemory
from smarter_dev.web.handler_runtime import run_handler_script


# -- HandlerMemory unit --------------------------------------------------------


def test_set_get_roundtrip_and_dirty():
    mem = HandlerMemory()
    assert mem.dirty is False
    assert mem.get("x") is None
    assert mem.get("x", 0) == 0
    mem.set("x", 5)
    assert mem.get("x") == 5
    assert mem.dirty is True


def test_seeds_from_initial_without_marking_dirty():
    mem = HandlerMemory({"count": 3})
    assert mem.get("count") == 3
    assert mem.dirty is False  # loading prior state isn't a change


def test_delete_returns_whether_removed():
    mem = HandlerMemory({"a": 1})
    assert mem.delete("missing") is False
    assert mem.dirty is False
    assert mem.delete("a") is True
    assert mem.dirty is True
    assert mem.get("a") is None


def test_all_returns_independent_deep_copy():
    mem = HandlerMemory({"nested": {"k": [1]}})
    snap = mem.all()
    snap["nested"]["k"].append(2)
    # Mutating the snapshot must not touch the store.
    assert mem.get("nested") == {"k": [1]}


def test_non_serializable_value_raises_and_leaves_state_intact():
    mem = HandlerMemory({"keep": 1})
    with pytest.raises(ValueError):
        mem.set("bad", {1, 2, 3})  # a set isn't JSON
    assert mem.get("bad") is None
    assert mem.get("keep") == 1
    assert mem.dirty is False  # rejected write doesn't dirty the store


def test_size_cap_raises_capexceeded():
    mem = HandlerMemory(max_bytes=64)
    with pytest.raises(CapExceeded) as exc:
        mem.set("big", "x" * 200)
    assert exc.value.cap == "memory_size"
    assert mem.dirty is False


# -- runtime integration -------------------------------------------------------


@dataclass
class _FakeEmitter:
    messages: list = field(default_factory=list)

    async def create_message(self, channel_id: str, content: str) -> str:
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"

    async def add_reaction(self, channel_id, message_id, emoji) -> None:
        pass


@dataclass
class _StubLimiter:
    async def hit(self, key: str, limit: int) -> bool:
        return True


async def _run(script, *, memory=None):
    return await run_handler_script(
        script,
        {"trigger_type": "message", "message_content": "hi"},
        channel_id="C1",
        guild_id="G1",
        emitter=_FakeEmitter(),
        limiter=_StubLimiter(),
        memory=memory,
    )


async def test_memory_persists_across_a_fire():
    script = (
        'n = await memory_get("count", 0)\n'
        'await memory_set("count", n + 1)\n'
    )
    # First fire from empty.
    r1 = await _run(script, memory={})
    assert r1.outcome == "ok"
    assert r1.memory_changed is True
    assert r1.memory == {"count": 1}

    # Second fire seeded with the saved memory.
    r2 = await _run(script, memory=r1.memory)
    assert r2.memory == {"count": 2}


async def test_unchanged_memory_is_not_flagged_dirty():
    script = 'v = await memory_get("count", 0)\nawait send_message(str(v))\n'
    r = await _run(script, memory={"count": 7})
    assert r.outcome == "ok"
    assert r.memory_changed is False  # only read -> no write-back
    assert r.memory == {"count": 7}


async def test_memory_all_and_delete_from_script():
    script = (
        'await memory_set("a", 1)\n'
        'await memory_set("b", 2)\n'
        'snap = await memory_all()\n'
        'await memory_delete("a")\n'
        'await send_message(str(sorted(snap.keys())))\n'
    )
    r = await _run(script, memory={})
    assert r.outcome == "ok"
    assert r.memory == {"b": 2}


async def test_memory_size_cap_surfaces_as_cap_exceeded():
    # 16 KB default cap; write something far larger.
    script = 'await memory_set("blob", "x" * 20000)\n'
    r = await _run(script, memory={})
    assert r.outcome == "cap_exceeded"
    assert r.cap == "memory_size"
