"""Bot API for the chat agent's persistent per-guild memory.

Two routes, modelled on ``model_overrides.py``:

- ``GET  /api/guilds/{guild_id}/chat-memory`` — the whole activation payload in
  one round trip: the long-term blob plus the notes the agent kept since
  midnight UTC.
- ``POST /api/guilds/{guild_id}/chat-memory/notes`` — the ``remember`` tool
  keeping one thought.

Two decisions are load-bearing and live here rather than in the bot:

**The bundle GET answers 200 with nulls, never 404.** A guild that has never
been dreamed is the ordinary first-activation case; the bot's memory service is
required never to raise, so an error status for "nothing yet" would turn every
new server's first message into a swallowed failure that looks exactly like a
real outage.

**The day boundary and the note cap are computed server-side only.** The window
is midnight UTC of the current day and the cap is
:data:`~smarter_dev.web.models.MEMORY_NOTES_CONTEXT_LIMIT`; neither is a query
parameter. Both are part of the memory design's contract with the prompts (the
dream consumes exactly what the day produced, and 25 notes is what the prompt
budget affords), so a caller must not be able to widen either.

A refused save is not an error: a same-day duplicate or a guild past its daily
cap answers 200 with ``{"saved": false, "reason": ...}``, which the tool turns
into a warm, truthful sentence.
"""

from __future__ import annotations

from datetime import UTC, datetime

from litestar import Controller, get, post
from litestar.status_codes import HTTP_200_OK
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.schemas import (
    ChatMemoryBundleRead,
    ChatMemoryNoteCreate,
    ChatMemoryNoteRead,
    ChatMemoryNoteSaveResult,
)
from smarter_dev.web.crud import (
    count_notes_since,
    create_memory_note,
    get_guild_memory_blob,
    list_notes_since,
)
from smarter_dev.web.models import (
    MAX_NOTES_PER_GUILD_PER_DAY,
    MEMORY_NOTES_CONTEXT_LIMIT,
)

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute.
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


def utc_day_start(moment: datetime) -> datetime:
    """Midnight UTC of the day ``moment`` falls in.

    The one definition of "today" for mid-term memory: notes are loaded from it,
    the daily cap and the duplicate check are windowed by it, and the dream
    consumes everything before it.
    """
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


class ChatMemoryController(Controller):
    """Long-term blob + mid-term notes for one guild's chat agent."""

    path = "/api/guilds/{guild_id:str}/chat-memory"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get(status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_chat_memory(
        self,
        db_session: AsyncSession,
        guild_id: str,
    ) -> ChatMemoryBundleRead:
        """Return the guild's blob and today's notes — 200 with nulls if there are none."""
        notes_since = utc_day_start(datetime.now(UTC))
        blob = await get_guild_memory_blob(db_session, guild_id)
        if blob is not None and not blob.memory_enabled:
            # The per-guild forget button: withhold everything, and say so, so
            # the bot can tell "switched off" from "nothing to remember yet".
            return ChatMemoryBundleRead(
                guild_id=guild_id,
                content=None,
                revision=None,
                updated_at=None,
                memory_enabled=False,
                notes_since=notes_since,
                notes=[],
            )
        notes = await list_notes_since(
            db_session,
            guild_id,
            since=notes_since,
            limit=MEMORY_NOTES_CONTEXT_LIMIT,
        )
        return ChatMemoryBundleRead(
            guild_id=guild_id,
            # An existing-but-empty blob reads back as null so the prompt omits
            # the block entirely rather than handing the agent its own amnesia.
            content=(blob.content or None) if blob is not None else None,
            revision=blob.revision if blob is not None else None,
            updated_at=blob.updated_at if blob is not None else None,
            memory_enabled=True,
            notes_since=notes_since,
            notes=[ChatMemoryNoteRead.model_validate(note) for note in notes],
        )

    @post("/notes", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def create_chat_memory_note(
        self,
        db_session: AsyncSession,
        guild_id: str,
        data: ChatMemoryNoteCreate,
    ) -> ChatMemoryNoteSaveResult:
        """Keep one note, or report why it wasn't kept — always 200."""
        now = datetime.now(UTC)
        day_start = utc_day_start(now)
        note = await create_memory_note(
            db_session,
            guild_id=guild_id,
            channel_id=data.channel_id,
            channel_name=data.channel_name,
            content=data.content,
            engagement_id=data.engagement_id,
            created_at=now,
            day_start=day_start,
        )
        if note is None:
            saved_today = await count_notes_since(db_session, guild_id, day_start)
            reason = (
                "daily_cap"
                if saved_today >= MAX_NOTES_PER_GUILD_PER_DAY
                else "duplicate"
            )
            return ChatMemoryNoteSaveResult(saved=False, reason=reason)
        # Serialize before commit to avoid session-detachment issues with the
        # Skrift-injected session (mirrors the model-override controller).
        result = ChatMemoryNoteSaveResult(
            saved=True, id=note.id, created_at=note.created_at
        )
        await db_session.commit()
        return result
