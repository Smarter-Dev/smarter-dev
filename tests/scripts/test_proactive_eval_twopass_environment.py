"""Tests for the two-pass bot's channel environment and instruction store."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval.simulation import FixtureMessage  # noqa: E402
from scripts.proactive_eval.twopass import environment  # noqa: E402

T = datetime(2026, 7, 20, 10, 0, 0, tzinfo=UTC)


def _message(message_id: str, offset_seconds: int, *, author_id: str = "1",
             display: str = "alice", bot: bool = False) -> FixtureMessage:
    return FixtureMessage(
        id=message_id,
        timestamp=T + timedelta(seconds=offset_seconds),
        author_id=author_id,
        author_name=display,
        author_display=display,
        is_bot=bot,
        content=f"message {message_id}",
        reply_to_id=None,
        mention_user_ids=(),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=0,
    )


def _environment(count: int = 10) -> environment.ChannelEnvironment:
    return environment.ChannelEnvironment(
        visible=[_message(str(n), n) for n in range(count)],
        bot_user_id="B1",
    )


def test_lookup_finds_message_or_none():
    env = _environment()
    assert env.lookup("3").id == "3"
    assert env.lookup("nope") is None


def test_history_returns_trailing_messages():
    env = _environment()
    assert [m.id for m in env.history(3)] == ["7", "8", "9"]


def test_history_before_a_message_id():
    env = _environment()
    assert [m.id for m in env.history(2, before_id="5")] == ["3", "4"]


def test_slice_around_clamps_at_edges():
    env = _environment()
    assert [m.id for m in env.slice_around("1", radius=3)] == [
        "0", "1", "2", "3", "4",
    ]


def test_render_uses_stable_tags_and_ids():
    env = environment.ChannelEnvironment(
        visible=[
            _message("1", 0, author_id="9", display="zoe"),
            _message("2", 1, author_id="4", display="amy"),
            _message("3", 2, author_id="9", display="zoe"),
        ],
        bot_user_id="B1",
    )
    rendered = env.render(env.visible[1:])
    # zoe appeared first over the whole visible list, so she keeps tag A
    # even when the rendered slice starts with amy.
    assert "[id=2] B·amy" in rendered
    assert "[id=3] A·zoe" in rendered


def test_instruction_store_sets_ttl_entries_and_counts():
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    store = environment.InstructionStore(seed="SEED RULES")
    assert "SEED RULES" in store.current(now=now)
    assert store.updates == 0

    entry = store.set_instruction(
        "wake me if zoe posts benchmark results", ttl_seconds=3600, now=now
    )
    assert store.updates == 1
    assert entry.instruction_id == "w1"
    rendered = store.current(now=now)
    assert "zoe posts benchmark results" in rendered
    assert "w1" in rendered

    assert store.clear_instruction("w1") is True
    assert store.clear_instruction("w1") is False
    assert "zoe posts benchmark" not in store.current(now=now)
    assert store.updates == 2


def test_instruction_store_prunes_expired_entries():
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    store = environment.InstructionStore(seed="SEED")
    store.set_instruction("short lived", ttl_seconds=60, now=now)
    store.set_instruction("long lived", ttl_seconds=7200, now=now)

    later = now + timedelta(minutes=30)
    expired = store.prune_expired(now=later)
    assert [e.text for e in expired] == ["short lived"]
    assert "short lived" not in store.current(now=later)
    assert "long lived" in store.current(now=later)


def test_instruction_store_caps_entries_and_ttl():
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    store = environment.InstructionStore(seed="SEED")
    for n in range(environment.MAX_WATCH_INSTRUCTIONS):
        store.set_instruction(f"entry {n}", ttl_seconds=600, now=now)
    with pytest.raises(ValueError, match="at most"):
        store.set_instruction("one too many", ttl_seconds=600, now=now)

    capped = environment.InstructionStore(seed="SEED").set_instruction(
        "very long", ttl_seconds=10**9, now=now
    )
    assert capped.expires_at <= now + timedelta(
        seconds=environment.MAX_WATCH_INSTRUCTION_TTL_SECONDS
    )


def test_instruction_store_round_trips_storage_and_reads_legacy_text():
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    store = environment.InstructionStore(seed="SEED")
    store.set_instruction("watch for tech news questions", ttl_seconds=3600, now=now)
    stored = store.to_stored()

    loaded = environment.InstructionStore.from_stored("SEED", stored, now=now)
    assert "watch for tech news questions" in loaded.current(now=now)
    # New ids continue after the loaded ones.
    assert loaded.set_instruction("x", ttl_seconds=60, now=now).instruction_id == "w2"

    legacy = environment.InstructionStore.from_stored(
        "SEED", "plain legacy addendum text", now=now
    )
    assert "plain legacy addendum text" in legacy.current(now=now)

    assert environment.InstructionStore.from_stored("SEED", "", now=now).entries == []
