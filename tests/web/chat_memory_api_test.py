"""Wire-contract tests for the chat-memory bot API.

The bundle GET is the single round trip a chat activation makes, so its
contract matters more than most: it must answer **200 with nulls** for a guild
that has never been dreamed (a 404 would make every first activation look like
a failure to the bot's never-raises service), and both the midnight-UTC day
boundary and the 25-note cap must be decided here — the bot may not ask for a
different window.

Endpoint tests run with the guards emptied (auth is asserted separately against
the real ``BOT_API_GUARDS``) and the crud layer mocked, matching the style of
``tests/web/test_api_native/``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import TestClient, create_test_client
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native import chat_memory as chat_memory_module
from smarter_dev.web.api_native.chat_memory import ChatMemoryController, utc_day_start
from smarter_dev.web.models import MAX_MEMORY_NOTE_CHARS, MEMORY_NOTES_CONTEXT_LIMIT

_GUILD = "123456789012345678"
_CHANNEL = "555000111222333444"

_BUNDLE_URL = f"/api/guilds/{_GUILD}/chat-memory"
_NOTES_URL = f"{_BUNDLE_URL}/notes"


@pytest.fixture
def session_mock() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    return session


@pytest.fixture
def chat_memory_client(session_mock: AsyncMock) -> Iterator[TestClient]:
    """Client serving the chat-memory controller with auth guards bypassed."""
    original_guards = list(chat_memory_module.BOT_API_GUARDS)
    chat_memory_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[ChatMemoryController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: session_mock, sync_to_thread=False)
            },
        ) as client:
            yield client
    finally:
        chat_memory_module.BOT_API_GUARDS[:] = original_guards


@pytest.fixture
def guarded_client() -> Iterator[TestClient]:
    """Client serving the chat-memory controller with its REAL auth guards."""
    with create_test_client(
        route_handlers=[ChatMemoryController],
        plugins=[PydanticPlugin()],
        dependencies={"db_session": Provide(lambda: Mock(), sync_to_thread=False)},
    ) as client:
        yield client


@pytest.fixture
def chat_memory_crud_mock() -> Iterator[Mock]:
    """Patch the four crud functions the chat-memory controller calls."""
    with (
        patch(
            "smarter_dev.web.api_native.chat_memory.get_guild_memory_blob",
            new=AsyncMock(return_value=None),
        ) as get_blob_mock,
        patch(
            "smarter_dev.web.api_native.chat_memory.list_notes_since",
            new=AsyncMock(return_value=[]),
        ) as list_notes_mock,
        patch(
            "smarter_dev.web.api_native.chat_memory.create_memory_note",
            new=AsyncMock(return_value=None),
        ) as create_note_mock,
        patch(
            "smarter_dev.web.api_native.chat_memory.count_notes_since",
            new=AsyncMock(return_value=0),
        ) as count_notes_mock,
    ):
        namespace = Mock()
        namespace.get_blob = get_blob_mock
        namespace.list_notes = list_notes_mock
        namespace.create_note = create_note_mock
        namespace.count_notes = count_notes_mock
        yield namespace


def _blob_row(**overrides) -> SimpleNamespace:
    fields = {
        "guild_id": _GUILD,
        "content": "## Who's here\nkai (id 7) is deep in embedded rust.",
        "revision": 4,
        "memory_enabled": True,
        "updated_at": datetime(2026, 8, 6, 0, 20, tzinfo=UTC),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _note_row(**overrides) -> SimpleNamespace:
    fields = {
        "id": uuid4(),
        "guild_id": _GUILD,
        "channel_id": _CHANNEL,
        "channel_name": "dev-help",
        "content": "alice (id 1) got soft shadows working and was giddy about it.",
        "created_at": datetime(2026, 8, 6, 9, 41, tzinfo=UTC),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# -- the day boundary ----------------------------------------------------------


def test_utc_day_start_is_midnight_utc():
    assert utc_day_start(datetime(2026, 8, 6, 23, 59, 59, tzinfo=UTC)) == datetime(
        2026, 8, 6, tzinfo=UTC
    )
    assert utc_day_start(datetime(2026, 8, 6, 0, 0, tzinfo=UTC)) == datetime(
        2026, 8, 6, tzinfo=UTC
    )


def test_utc_day_start_normalises_other_offsets_to_utc():
    # 01:30 at +05:00 is still the previous UTC day.
    moment = datetime(2026, 8, 6, 1, 30, tzinfo=UTC) + timedelta(hours=5)
    assert utc_day_start(moment.astimezone(UTC)) == datetime(2026, 8, 6, tzinfo=UTC)


# -- auth ----------------------------------------------------------------------


def test_bundle_get_requires_a_bot_api_key(guarded_client: TestClient):
    assert guarded_client.get(_BUNDLE_URL).status_code == 401


def test_note_post_requires_a_bot_api_key(guarded_client: TestClient):
    response = guarded_client.post(
        _NOTES_URL, json={"channel_id": _CHANNEL, "content": "hi"}
    )
    assert response.status_code == 401


def test_a_non_skrift_bearer_is_rejected(guarded_client: TestClient):
    response = guarded_client.get(
        _BUNDLE_URL, headers={"Authorization": "Bearer not-a-skrift-key"}
    )
    assert response.status_code == 401


# -- the bundle GET ------------------------------------------------------------


class TestGetChatMemoryBundle:
    def test_unknown_guild_is_200_with_nulls(
        self, chat_memory_client: TestClient, chat_memory_crud_mock
    ):
        response = chat_memory_client.get(_BUNDLE_URL)

        assert response.status_code == 200
        body = response.json()
        assert body["guild_id"] == _GUILD
        assert body["content"] is None
        assert body["revision"] is None
        assert body["updated_at"] is None
        assert body["memory_enabled"] is True
        assert body["notes"] == []

    def test_returns_the_blob_and_todays_notes(
        self, chat_memory_client: TestClient, chat_memory_crud_mock
    ):
        chat_memory_crud_mock.get_blob.return_value = _blob_row()
        note = _note_row()
        chat_memory_crud_mock.list_notes.return_value = [note]

        response = chat_memory_client.get(_BUNDLE_URL)

        assert response.status_code == 200
        body = response.json()
        assert body["content"].startswith("## Who's here")
        assert body["revision"] == 4
        assert body["updated_at"] == "2026-08-06T00:20:00+00:00"
        assert len(body["notes"]) == 1
        assert body["notes"][0]["id"] == str(note.id)
        assert body["notes"][0]["channel_name"] == "dev-help"
        assert body["notes"][0]["created_at"] == "2026-08-06T09:41:00+00:00"

    def test_an_empty_blob_reads_back_as_null(
        self, chat_memory_client: TestClient, chat_memory_crud_mock
    ):
        # A row that exists but has never been dreamed must not hand the bot an
        # empty <what-i-remember> block to remark on.
        chat_memory_crud_mock.get_blob.return_value = _blob_row(content="", revision=0)

        assert chat_memory_client.get(_BUNDLE_URL).json()["content"] is None

    def test_the_note_window_is_midnight_utc_and_capped_server_side(
        self, chat_memory_client: TestClient, chat_memory_crud_mock
    ):
        chat_memory_client.get(_BUNDLE_URL)

        _, kwargs = chat_memory_crud_mock.list_notes.call_args
        args, _ = chat_memory_crud_mock.list_notes.call_args
        since = kwargs.get("since", args[2] if len(args) > 2 else None)
        assert since == utc_day_start(datetime.now(UTC))
        assert kwargs["limit"] == MEMORY_NOTES_CONTEXT_LIMIT

    def test_a_client_supplied_limit_or_since_is_ignored(
        self, chat_memory_client: TestClient, chat_memory_crud_mock
    ):
        chat_memory_client.get(
            _BUNDLE_URL, params={"limit": 500, "since": "2020-01-01T00:00:00Z"}
        )

        _, kwargs = chat_memory_crud_mock.list_notes.call_args
        assert kwargs["limit"] == MEMORY_NOTES_CONTEXT_LIMIT

    def test_the_per_guild_kill_switch_empties_the_bundle(
        self, chat_memory_client: TestClient, chat_memory_crud_mock
    ):
        chat_memory_crud_mock.get_blob.return_value = _blob_row(memory_enabled=False)
        chat_memory_crud_mock.list_notes.return_value = [_note_row()]

        body = chat_memory_client.get(_BUNDLE_URL).json()

        assert body["memory_enabled"] is False
        assert body["content"] is None
        assert body["notes"] == []


# -- saving a note -------------------------------------------------------------


class TestCreateChatMemoryNote:
    def test_saves_and_commits(
        self, chat_memory_client: TestClient, chat_memory_crud_mock, session_mock
    ):
        saved = _note_row()
        chat_memory_crud_mock.create_note.return_value = saved

        response = chat_memory_client.post(
            _NOTES_URL,
            json={
                "channel_id": _CHANNEL,
                "channel_name": "dev-help",
                "content": "alice (id 1) is into raymarching.",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["saved"] is True
        assert body["reason"] is None
        assert body["id"] == str(saved.id)
        session_mock.commit.assert_awaited_once()

    def test_passes_the_engagement_and_channel_through(
        self, chat_memory_client: TestClient, chat_memory_crud_mock
    ):
        engagement_id = uuid4()
        chat_memory_crud_mock.create_note.return_value = _note_row()

        chat_memory_client.post(
            _NOTES_URL,
            json={
                "channel_id": _CHANNEL,
                "channel_name": "dev-help",
                "content": "a thought",
                "engagement_id": str(engagement_id),
            },
        )

        _, kwargs = chat_memory_crud_mock.create_note.call_args
        assert kwargs["guild_id"] == _GUILD
        assert kwargs["channel_id"] == _CHANNEL
        assert kwargs["channel_name"] == "dev-help"
        assert kwargs["engagement_id"] == engagement_id
        assert kwargs["day_start"] == utc_day_start(datetime.now(UTC))

    def test_duplicate_is_saved_false_not_an_error(
        self, chat_memory_client: TestClient, chat_memory_crud_mock, session_mock
    ):
        chat_memory_crud_mock.create_note.return_value = None
        chat_memory_crud_mock.count_notes.return_value = 3  # nowhere near the cap

        response = chat_memory_client.post(
            _NOTES_URL, json={"channel_id": _CHANNEL, "content": "already noted"}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["saved"] is False
        assert body["reason"] == "duplicate"
        assert body["id"] is None
        session_mock.commit.assert_not_awaited()

    def test_daily_cap_is_reported_as_its_own_reason(
        self, chat_memory_client: TestClient, chat_memory_crud_mock
    ):
        chat_memory_crud_mock.create_note.return_value = None
        chat_memory_crud_mock.count_notes.return_value = 200

        body = chat_memory_client.post(
            _NOTES_URL, json={"channel_id": _CHANNEL, "content": "one too many"}
        ).json()

        assert body["saved"] is False
        assert body["reason"] == "daily_cap"

    def test_over_length_content_is_422(self, chat_memory_client: TestClient):
        response = chat_memory_client.post(
            _NOTES_URL,
            json={
                "channel_id": _CHANNEL,
                "content": "x" * (MAX_MEMORY_NOTE_CHARS + 1),
            },
        )
        assert response.status_code == 422

    def test_empty_content_is_422(self, chat_memory_client: TestClient):
        response = chat_memory_client.post(
            _NOTES_URL, json={"channel_id": _CHANNEL, "content": "   "}
        )
        assert response.status_code == 422

    def test_missing_channel_id_is_422(self, chat_memory_client: TestClient):
        response = chat_memory_client.post(_NOTES_URL, json={"content": "a thought"})
        assert response.status_code == 422
