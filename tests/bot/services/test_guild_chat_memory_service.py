"""Tests for GuildChatMemoryService — the bot's read/write path to guild memory.

The governing requirement is that this service NEVER raises. It runs on the hot
chat activation path, and a memory outage has to cost the bot its memory for one
turn, not the turn itself — so every failure mode here is asserted to degrade to
:data:`EMPTY_SNAPSHOT` (or an unsaved :class:`NoteSaveResult`) rather than
propagate. The global ``CHAT_MEMORY_ENABLED`` kill switch is checked here too:
one of exactly two chokepoints in the whole system.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest

from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.guild_chat_memory_service import EMPTY_SNAPSHOT
from smarter_dev.bot.services.guild_chat_memory_service import GuildChatMemoryService
from smarter_dev.bot.services.guild_chat_memory_service import GuildMemorySnapshot
from smarter_dev.bot.services.guild_chat_memory_service import NoteSaveResult
from smarter_dev.shared.guild_event_log import CHAT_MEMORY_ENABLED_ENV
from tests.bot.services.conftest import create_mock_response

GUILD = "111"
BUNDLE_PATH = f"/guilds/{GUILD}/chat-memory"
NOTES_PATH = f"/guilds/{GUILD}/chat-memory/notes"


def _bundle(
    *,
    content: str | None = "## Who's here\n- alice (id 1) builds shaders.",
    revision: int | None = 4,
    memory_enabled: bool = True,
    notes: list[dict] | None = None,
) -> dict:
    return {
        "guild_id": GUILD,
        "content": content,
        "revision": revision,
        "updated_at": "2026-08-06T00:20:00+00:00",
        "memory_enabled": memory_enabled,
        "notes_since": "2026-08-06T00:00:00+00:00",
        "notes": notes if notes is not None else [],
    }


def _note(
    content: str = "alice (id 1) got soft shadows working and was giddy about it.",
    *,
    channel_name: str | None = "dev-help",
) -> dict:
    return {
        "id": str(uuid4()),
        "channel_id": "222",
        "channel_name": channel_name,
        "content": content,
        "created_at": "2026-08-06T09:41:00+00:00",
    }


@pytest.fixture
async def service(mock_api_client, mock_cache_manager) -> GuildChatMemoryService:
    memory_service = GuildChatMemoryService(mock_api_client, mock_cache_manager)
    await memory_service.initialize()
    return memory_service


# --------------------------------------------------------------------------- #
# load_snapshot
# --------------------------------------------------------------------------- #


async def test_load_snapshot_reads_the_bundle_endpoint(service, mock_api_client):
    mock_api_client.get.return_value = create_mock_response(
        200, _bundle(notes=[_note()])
    )

    snapshot = await service.load_snapshot(GUILD)

    mock_api_client.get.assert_awaited_once_with(BUNDLE_PATH)
    assert isinstance(snapshot, GuildMemorySnapshot)
    assert snapshot.long_term_memory.startswith("## Who's here")
    assert snapshot.revision == 4
    assert snapshot.updated_at == datetime(2026, 8, 6, 0, 20, tzinfo=UTC)
    assert snapshot.memory_enabled is True
    assert len(snapshot.notes) == 1
    assert snapshot.notes[0].channel_name == "dev-help"
    assert snapshot.notes[0].created_at == datetime(2026, 8, 6, 9, 41, tzinfo=UTC)
    assert "soft shadows" in snapshot.notes[0].text


async def test_load_snapshot_handles_a_guild_with_no_memory_yet(
    service, mock_api_client
):
    # The bundle answers 200 with nulls for a guild that has never been dreamed.
    mock_api_client.get.return_value = create_mock_response(200, _bundle(content=None, revision=None))

    snapshot = await service.load_snapshot(GUILD)

    assert snapshot.long_term_memory is None
    assert snapshot.notes == ()


async def test_load_snapshot_returns_empty_snapshot_on_api_error(
    service, mock_api_client
):
    mock_api_client.get.side_effect = APIError("boom", status_code=500)

    assert await service.load_snapshot(GUILD) is EMPTY_SNAPSHOT


async def test_load_snapshot_returns_empty_snapshot_on_404(service, mock_api_client):
    # A 404 should not happen (the bundle is a 200-with-nulls route), but an old
    # deployment or a routing mistake must still cost only the memory.
    mock_api_client.get.side_effect = APIError("not found", status_code=404)

    assert await service.load_snapshot(GUILD) is EMPTY_SNAPSHOT


async def test_load_snapshot_returns_empty_snapshot_on_unusable_payload(
    service, mock_api_client
):
    # A payload the schema doesn't recognise is a deployment skew, not a reason
    # to fail an activation.
    mock_api_client.get.return_value = create_mock_response(200, {"unexpected": True})

    assert await service.load_snapshot(GUILD) is EMPTY_SNAPSHOT


async def test_load_snapshot_honours_per_guild_switch(service, mock_api_client):
    mock_api_client.get.return_value = create_mock_response(
        200, _bundle(content=None, revision=None, memory_enabled=False)
    )

    snapshot = await service.load_snapshot(GUILD)

    assert snapshot.memory_enabled is False
    assert snapshot.long_term_memory is None
    assert snapshot.notes == ()


async def test_load_snapshot_skips_the_wire_when_globally_disabled(
    service, mock_api_client, monkeypatch
):
    monkeypatch.setenv(CHAT_MEMORY_ENABLED_ENV, "0")

    assert await service.load_snapshot(GUILD) is EMPTY_SNAPSHOT
    mock_api_client.get.assert_not_awaited()


# --------------------------------------------------------------------------- #
# save_note
# --------------------------------------------------------------------------- #


async def test_save_note_posts_the_note_payload(service, mock_api_client):
    engagement_id = uuid4()
    mock_api_client.post.return_value = create_mock_response(
        200, {"saved": True, "reason": None, "id": str(uuid4())}
    )

    result = await service.save_note(
        GUILD,
        channel_id="222",
        content="alice (id 1) is into shaders.",
        channel_name="dev-help",
        engagement_id=engagement_id,
    )

    mock_api_client.post.assert_awaited_once_with(
        NOTES_PATH,
        json_data={
            "channel_id": "222",
            "channel_name": "dev-help",
            "content": "alice (id 1) is into shaders.",
            "engagement_id": str(engagement_id),
        },
    )
    assert result.saved is True
    assert result.reason is None


async def test_save_note_reports_a_server_refusal_as_a_reason(
    service, mock_api_client
):
    mock_api_client.post.return_value = create_mock_response(
        200, {"saved": False, "reason": "duplicate"}
    )

    result = await service.save_note(GUILD, channel_id="222", content="same thought")

    assert result == NoteSaveResult(saved=False, reason="duplicate")


async def test_save_note_never_raises_on_api_error(service, mock_api_client):
    mock_api_client.post.side_effect = APIError("boom", status_code=503)

    result = await service.save_note(GUILD, channel_id="222", content="a thought")

    assert result.saved is False
    assert result.reason == "api_error"


async def test_save_note_skips_the_wire_when_globally_disabled(
    service, mock_api_client, monkeypatch
):
    monkeypatch.setenv(CHAT_MEMORY_ENABLED_ENV, "0")

    result = await service.save_note(GUILD, channel_id="222", content="a thought")

    assert result.saved is False
    assert result.reason == "disabled"
    mock_api_client.post.assert_not_awaited()
