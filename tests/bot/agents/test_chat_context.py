"""Tests for the chat_context builders.

These cover the hikari-API plumbing that unit tests for the engine don't —
in particular, ``_fetch_messages_before`` which uses hikari's
``fetch_messages(channel_id, before=...)`` (the ``before`` is a kwarg, not
a method on the returned iterator). A regression here broke all initial
activations in production.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from smarter_dev.bot.agents.chat_context import _fetch_messages_before


class _AsyncIter:
    """A minimal LazyIterator-like that yields its source list and has
    a ``.limit(n)`` method matching hikari's chainable shape."""

    def __init__(self, items):
        self._items = list(items)

    def limit(self, n):  # noqa: D401 — mirroring hikari's API
        return _AsyncIter(self._items[:n])

    def __aiter__(self):
        async def _gen():
            for item in self._items:
                yield item
        return _gen()


@pytest.mark.asyncio
async def test_fetch_messages_before_uses_kwarg_and_reverses():
    """Regression: hikari's ``fetch_messages`` takes ``before`` as a kwarg,
    NOT as a chainable method on the iterator. The earlier implementation
    used ``.before(...)`` and crashed every initial activation in prod."""
    msg_newer = SimpleNamespace(id=200)
    msg_older = SimpleNamespace(id=100)
    # Hikari returns newest-first.
    bot = MagicMock()
    captured: dict = {}

    def fake_fetch_messages(channel_id, *, before=None):
        captured["channel_id"] = channel_id
        captured["before"] = before
        return _AsyncIter([msg_newer, msg_older])

    bot.rest = MagicMock()
    bot.rest.fetch_messages = fake_fetch_messages

    out = await _fetch_messages_before(bot, channel_id=42, before_id=999, limit=10)

    assert captured["channel_id"] == 42
    assert captured["before"] == 999
    # Output is oldest-first.
    assert [m.id for m in out] == [100, 200]
