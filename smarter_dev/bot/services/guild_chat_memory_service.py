"""Bot-side service for the chat agent's persistent per-guild memory.

Wraps the web API's ``/guilds/{guild_id}/chat-memory`` routes so the chat engine
can load a guild's long-term memory blob and today's notes in one round trip at
activation, and so anything bot-side can keep a note without touching the DB.
Mirrors :mod:`smarter_dev.bot.services.model_override_service` in shape; the
difference that matters is the failure contract.

**Nothing here raises.** This runs on the activation path of every engagement,
and a memory outage must cost the bot its memory for one conversation, never the
conversation itself — a swallowed reply is far worse than a bot that has
temporarily forgotten. So an API failure, an unparseable payload, or the global
kill switch all degrade to :data:`EMPTY_SNAPSHOT`, and a failed save comes back
as an unsaved :class:`NoteSaveResult` the ``remember`` tool turns into a warm,
truthful sentence.

**No caching in the MVP.** One GET per activation is cheap, and the notes half of
the bundle must be fresh: a note kept in one channel should be visible to the
next channel's activation minutes later, and a cache would make "the bot forgot
what it just told me it would remember" a routine occurrence. When a cache does
land it should be **blob-only** — the blob changes once a night, so it can be
held with a TTL of ``min(900, seconds_until_utc_midnight)``, clamped to the day
boundary so a dream's rewrite is never served stale — while the notes stay on
the wire.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from smarter_dev.bot.agents.chat_models import MemoryNote
from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.cache_manager import CacheManager
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.shared.guild_event_log import chat_memory_enabled

logger = logging.getLogger(__name__)

__all__ = [
    "EMPTY_SNAPSHOT",
    "GuildChatMemoryService",
    "GuildMemorySnapshot",
    "NoteSaveResult",
]

# Why a save didn't happen, when the server never got a say. Server-side
# refusals ("duplicate", "daily_cap") come back on the wire and are passed
# through untouched.
DISABLED_REASON = "disabled"
API_FAILURE_REASON = "api_error"


@dataclass(frozen=True)
class GuildMemorySnapshot:
    """Everything one activation needs from a guild's persistent memory.

    ``long_term_memory`` is the dream-written blob (``None`` when the guild has
    never been dreamed, or when its memory is switched off) and ``notes`` are the
    thoughts the agent kept since midnight UTC, capped and windowed server-side.
    Frozen, and ``notes`` is a tuple, because this is handed straight to the
    prompt builders — nothing downstream should be able to edit the bot's memory
    on its way to being rendered.
    """

    long_term_memory: str | None = None
    updated_at: datetime | None = None
    revision: int | None = None
    memory_enabled: bool = True
    notes: tuple[MemoryNote, ...] = field(default_factory=tuple)


# The "I have nothing to remember right now" snapshot: a new guild, a guild with
# memory switched off, and a guild whose memory the API couldn't serve all look
# identical to the prompt, which renders no memory blocks at all for any of them.
EMPTY_SNAPSHOT = GuildMemorySnapshot()


@dataclass(frozen=True)
class NoteSaveResult:
    """Whether a note was kept, and if not, why — never an exception.

    ``reason`` is ``"duplicate"`` or ``"daily_cap"`` when the server declined,
    :data:`DISABLED_REASON` when the global kill switch is off, and
    :data:`API_FAILURE_REASON` when the call itself failed.
    """

    saved: bool
    reason: str | None = None
    note_id: str | None = None


def _parsed_timestamp(raw_value: Any) -> datetime | None:
    """An aware datetime for an ISO-8601 string, or ``None`` if it isn't one."""
    if not isinstance(raw_value, str) or not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        logger.debug("chat memory: unparseable timestamp %r", raw_value)
        return None


def _parsed_note(payload: dict[str, Any]) -> MemoryNote | None:
    """A view model for one note row, or ``None`` if the row is unusable."""
    created_at = _parsed_timestamp(payload.get("created_at"))
    content = payload.get("content")
    if created_at is None or not isinstance(content, str) or not content.strip():
        return None
    return MemoryNote(
        text=content,
        channel_id=payload.get("channel_id"),
        channel_name=payload.get("channel_name"),
        created_at=created_at,
    )


def _parsed_snapshot(payload: Any) -> GuildMemorySnapshot:
    """Build a snapshot from the bundle response body.

    A row the schema doesn't recognise is deployment skew, so an individual bad
    note is dropped rather than costing the whole snapshot.
    """
    if not isinstance(payload, dict) or "memory_enabled" not in payload:
        raise ValueError("chat memory bundle is missing its expected fields")
    content = payload.get("content")
    notes = tuple(
        note
        for raw_note in payload.get("notes") or []
        if isinstance(raw_note, dict) and (note := _parsed_note(raw_note)) is not None
    )
    return GuildMemorySnapshot(
        long_term_memory=content if isinstance(content, str) and content else None,
        updated_at=_parsed_timestamp(payload.get("updated_at")),
        revision=payload.get("revision"),
        memory_enabled=bool(payload.get("memory_enabled")),
        notes=notes,
    )


class GuildChatMemoryService(BaseService):
    """Read a guild's memory bundle and keep new notes, via the web API."""

    def __init__(
        self, api_client: APIClient, cache_manager: CacheManager | None = None
    ):
        super().__init__(
            api_client, cache_manager, service_name="GuildChatMemoryService"
        )

    @staticmethod
    def _bundle_path(guild_id: str) -> str:
        return f"/guilds/{guild_id}/chat-memory"

    @staticmethod
    def _notes_path(guild_id: str) -> str:
        return f"/guilds/{guild_id}/chat-memory/notes"

    async def load_snapshot(self, guild_id: str) -> GuildMemorySnapshot:
        """The guild's blob + today's notes, or :data:`EMPTY_SNAPSHOT`.

        One of exactly two places the global ``CHAT_MEMORY_ENABLED`` switch is
        read (the other is ``guild_event_log.append_event``), so a deployment can
        turn the whole system off without every caller remembering to ask.
        """
        if not chat_memory_enabled():
            return EMPTY_SNAPSHOT
        try:
            response = await self._api_client.get(self._bundle_path(guild_id))
            return _parsed_snapshot(response.json())
        except (APIError, ValidationError, ValueError, TypeError):
            logger.warning(
                "Could not load chat memory for guild %s — running without it",
                guild_id,
                exc_info=True,
            )
            return EMPTY_SNAPSHOT

    async def save_note(
        self,
        guild_id: str,
        *,
        channel_id: str,
        content: str,
        channel_name: str | None = None,
        engagement_id: UUID | str | None = None,
    ) -> NoteSaveResult:
        """Keep one note for ``guild_id``, reporting refusals rather than raising.

        A same-day duplicate or a guild past its daily cap is a normal 200 with
        ``saved: false``; the reason is passed straight through so the caller can
        say something true about why nothing was kept.
        """
        if not chat_memory_enabled():
            return NoteSaveResult(saved=False, reason=DISABLED_REASON)
        payload = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "content": content,
            "engagement_id": None if engagement_id is None else str(engagement_id),
        }
        try:
            body = (
                await self._api_client.post(
                    self._notes_path(guild_id), json_data=payload
                )
            ).json()
        except APIError:
            logger.warning(
                "Could not save a chat memory note for guild %s", guild_id, exc_info=True
            )
            return NoteSaveResult(saved=False, reason=API_FAILURE_REASON)
        if not isinstance(body, dict) or not body.get("saved"):
            reason = body.get("reason") if isinstance(body, dict) else None
            return NoteSaveResult(saved=False, reason=reason or API_FAILURE_REASON)
        note_id = body.get("id")
        return NoteSaveResult(
            saved=True, note_id=None if note_id is None else str(note_id)
        )
