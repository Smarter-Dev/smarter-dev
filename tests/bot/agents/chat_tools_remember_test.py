"""Tests for the chat agent's ``remember`` tool.

``remember`` is the agent's own hand on its mid-term memory: it POSTs one
first-person note to the chat-memory API and answers with a warm sentence.
Two properties matter more than the happy path and are pinned here:

- **It never raises.** Every refusal — the per-turn ceiling, a duplicate, a
  guild past its daily cap, an API outage — comes back as prose the agent can
  read, the same discipline the tool budget uses.
- **It is silent.** No ``> -#`` status line ever reaches the channel. Status
  lines exist to explain latency; a friend announcing each thing they commit to
  memory is unsettling, and the memory is discoverable by just asking the bot.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from smarter_dev.bot.agents.chat_tools import MAX_MEMORIES_PER_TURN
from smarter_dev.bot.agents.chat_tools import REMEMBER_API_FAILURE
from smarter_dev.bot.agents.chat_tools import REMEMBER_DAILY_CAP
from smarter_dev.bot.agents.chat_tools import REMEMBER_DUPLICATE
from smarter_dev.bot.agents.chat_tools import REMEMBER_EMPTY
from smarter_dev.bot.agents.chat_tools import REMEMBER_SAVED
from smarter_dev.bot.agents.chat_tools import REMEMBER_TRIMMED
from smarter_dev.bot.agents.chat_tools import REMEMBER_TURN_LIMIT
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.chat_tools import chat_tool_functions
from smarter_dev.bot.agents.chat_tools import remember
from smarter_dev.web.models import MAX_MEMORY_NOTE_CHARS

REMEMBER_DOCSTRING = (
    "Keep something. Use it when a moment is worth still knowing tomorrow — who "
    "someone is and what they're into, a joke that landed, an opinion you formed, "
    "something you're curious about, how a conversation left you. Write it in "
    "first person, one thought per call, the way you'd tell a friend about your "
    "day; name people as `username (id 123)`. Not for errands, not for "
    "summarizing what you just said, and not for anything private or sensitive "
    "someone would rather you forgot. Tonight you'll re-read everything you kept "
    "today and decide what stays with you for good."
)


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _FakeAPI:
    """Fake APIClient recording every note POST it is handed."""

    def __init__(self, *results: dict):
        self._results = list(results) or [{"saved": True}]
        self.posts: list[tuple[str, dict]] = []

    async def post(self, path, json_data=None, **_kwargs):
        self.posts.append((path, json_data or {}))
        return _Response(self._results[min(len(self.posts) - 1, len(self._results) - 1)])


class _RaisingAPI:
    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    async def post(self, path, json_data=None, **_kwargs):
        self.posts.append((path, json_data or {}))
        raise RuntimeError("api is down")


def _ctx(api, **deps_overrides) -> SimpleNamespace:
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    deps_kwargs = {
        "bot": bot,
        "channel_id": 4242,
        "guild_id": 99,
        "api_client": api,
        "channel_name": "dev-help",
        "engagement_id": UUID("11111111-2222-3333-4444-555555555555"),
        **deps_overrides,
    }
    return SimpleNamespace(deps=ChatDeps(**deps_kwargs))


def test_remember_is_registered_as_a_chat_tool():
    assert remember in chat_tool_functions()


def test_remember_docstring_is_the_approved_prompt_text():
    assert remember.__doc__ == REMEMBER_DOCSTRING


async def test_remember_posts_the_note_and_confirms_warmly():
    api = _FakeAPI({"saved": True, "id": "abc"})
    ctx = _ctx(api)

    result = await remember(ctx, "alice (id 1) is deep in shader work and loves it.")

    assert result == REMEMBER_SAVED
    assert len(api.posts) == 1
    path, payload = api.posts[0]
    assert path == "/guilds/99/chat-memory/notes"
    assert payload == {
        "channel_id": "4242",
        "channel_name": "dev-help",
        "content": "alice (id 1) is deep in shader work and loves it.",
        "engagement_id": "11111111-2222-3333-4444-555555555555",
    }
    assert ctx.deps.memories_saved_this_turn == 1
    assert ctx.deps.saved_memory_texts == [
        "alice (id 1) is deep in shader work and loves it."
    ]


async def test_remember_writes_nothing_when_the_global_kill_switch_is_off(monkeypatch):
    """The kill-switch chokepoint the tool owns: with ``CHAT_MEMORY_ENABLED``
    off, reads and event-logging already stop at their own chokepoints, and the
    ``remember`` tool — which POSTs directly rather than through the memory
    service — must stop writing too, answering in prose as always."""
    monkeypatch.setenv("CHAT_MEMORY_ENABLED", "false")
    api = _FakeAPI({"saved": True})
    ctx = _ctx(api)

    result = await remember(ctx, "carol (id 3) ships on fridays and regrets it.")

    assert result == REMEMBER_API_FAILURE
    assert api.posts == []
    assert ctx.deps.memories_saved_this_turn == 0


async def test_remember_never_posts_a_status_line_to_the_channel():
    api = _FakeAPI({"saved": True})
    ctx = _ctx(api)

    await remember(ctx, "bob (id 2) has opinions about tabs.")

    ctx.deps.bot.rest.create_message.assert_not_called()


async def test_remember_clamps_an_overlong_note_and_says_so():
    api = _FakeAPI({"saved": True})
    ctx = _ctx(api)

    result = await remember(ctx, "x" * (MAX_MEMORY_NOTE_CHARS + 200))

    assert result == REMEMBER_TRIMMED
    _, payload = api.posts[0]
    assert len(payload["content"]) == MAX_MEMORY_NOTE_CHARS


async def test_remember_refuses_past_the_per_turn_ceiling_without_posting():
    api = _FakeAPI({"saved": True})
    ctx = _ctx(api, memories_saved_this_turn=MAX_MEMORIES_PER_TURN)

    result = await remember(ctx, "one more thought")

    assert result == REMEMBER_TURN_LIMIT
    assert api.posts == []


async def test_remember_refuses_a_repeat_of_something_kept_this_run():
    api = _FakeAPI({"saved": True})
    ctx = _ctx(api)

    first = await remember(ctx, "kai (id 7) hates cmake.")
    second = await remember(ctx, "  Kai (id 7) hates cmake.  ")

    assert first == REMEMBER_SAVED
    assert second == REMEMBER_DUPLICATE
    assert len(api.posts) == 1
    assert ctx.deps.memories_saved_this_turn == 1


async def test_remember_keeps_nothing_for_blank_text():
    api = _FakeAPI({"saved": True})
    ctx = _ctx(api)

    assert await remember(ctx, "   ") == REMEMBER_EMPTY
    assert api.posts == []


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("duplicate", REMEMBER_DUPLICATE),
        ("daily_cap", REMEMBER_DAILY_CAP),
        ("something_new", REMEMBER_API_FAILURE),
    ],
)
async def test_remember_translates_a_server_refusal(reason, expected):
    api = _FakeAPI({"saved": False, "reason": reason})
    ctx = _ctx(api)

    result = await remember(ctx, "a thought the server declined")

    assert result == expected
    assert ctx.deps.memories_saved_this_turn == 0
    assert ctx.deps.saved_memory_texts == []


async def test_remember_answers_in_prose_when_the_api_fails():
    api = _RaisingAPI()
    ctx = _ctx(api)

    result = await remember(ctx, "something worth keeping")

    assert result == REMEMBER_API_FAILURE
    assert ctx.deps.memories_saved_this_turn == 0


async def test_remember_omits_the_engagement_link_when_there_is_none():
    api = _FakeAPI({"saved": True})
    ctx = _ctx(api, engagement_id=None, channel_name=None)

    await remember(ctx, "a note from outside any engagement")

    _, payload = api.posts[0]
    assert payload["engagement_id"] is None
    assert payload["channel_name"] is None
