"""Tests for the channel error-notice posted when a handler fire errors."""

from __future__ import annotations

from dataclasses import dataclass, field

from smarter_dev.web.handler_notify import (
    MAX_ERROR_DETAIL,
    format_error_notice,
    notify_handler_error,
)


@dataclass
class _FakeEmitter:
    messages: list = field(default_factory=list)

    async def create_message(self, channel_id: str, content: str) -> str:
        self.messages.append((channel_id, content))
        return "m1"


@dataclass
class _StubLimiter:
    allow: bool = True
    hits: list = field(default_factory=list)

    async def hit(self, key: str, limit: int) -> bool:
        self.hits.append((key, limit))
        return self.allow


def test_format_collapses_whitespace_and_truncates():
    notice = format_error_notice("runtime: boom\n  with   newlines")
    assert "runtime: boom with newlines" in notice
    assert "⚠️" in notice

    long = format_error_notice("x" * 1000)
    # The detail inside the code block is capped; the whole message stays bounded.
    assert "x" * MAX_ERROR_DETAIL not in long
    assert "…" in long


def test_format_handles_missing_error():
    assert "unknown error" in format_error_notice(None)


async def test_posts_when_within_throttle():
    emitter = _FakeEmitter()
    limiter = _StubLimiter(allow=True)
    posted = await notify_handler_error(
        emitter=emitter, limiter=limiter, handler_id="h1",
        channel_id="C1", error="runtime: boom",
    )
    assert posted is True
    assert emitter.messages[0][0] == "C1"
    assert "boom" in emitter.messages[0][1]
    assert limiter.hits[0][0] == "hcap:errnotice:h1"


async def test_suppressed_when_throttled():
    emitter = _FakeEmitter()
    limiter = _StubLimiter(allow=False)
    posted = await notify_handler_error(
        emitter=emitter, limiter=limiter, handler_id="h1",
        channel_id="C1", error="boom",
    )
    assert posted is False
    assert emitter.messages == []


async def test_no_channel_is_a_noop():
    emitter = _FakeEmitter()
    limiter = _StubLimiter(allow=True)
    posted = await notify_handler_error(
        emitter=emitter, limiter=limiter, handler_id="h1",
        channel_id="", error="boom",
    )
    assert posted is False
    assert emitter.messages == []
    assert limiter.hits == []  # didn't even spend a throttle slot


async def test_emitter_failure_is_swallowed():
    @dataclass
    class _BoomEmitter:
        async def create_message(self, channel_id, content):
            raise RuntimeError("discord down")

    posted = await notify_handler_error(
        emitter=_BoomEmitter(), limiter=_StubLimiter(allow=True),
        handler_id="h1", channel_id="C1", error="boom",
    )
    assert posted is False  # best-effort: never raises
