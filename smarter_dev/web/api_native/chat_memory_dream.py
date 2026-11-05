"""Bot API for an agent-run nightly dream.

The proactive agent dreams in its own history; this module is the only way it
touches the guild's memory. Three routes:

- ``GET  /api/chat-memory/dream/guilds`` — guilds that need tonight's dream.
- ``GET  /api/guilds/{guild_id}/chat-memory/dream`` — everything the dream reads
  (everything the dream reads, with the user message the model is given).
- ``POST /api/guilds/{guild_id}/chat-memory/dream`` — the dream's answer.

All validation lives in :mod:`smarter_dev.web.chat_memory_dream` and runs here
unchanged, in its strict mode: a problem the model can fix is refused with a
reason that names no stored text, so the agent can try again. A refusal is a
200 with ``accepted: false`` and nothing is written. The submission also echoes
the revision, the note ids and an ``inputs_digest`` of what it read; if any of
them no longer matches (a purge or retag rewriting text in place, an opt-out, a
new note before the cutoff), it is refused as ``stale`` and the agent re-reads.
The digest covers the revision, the cutoff, each note's id and text, the unread
note ids, the stored and the withheld text of all four blocks, and the user
message.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from litestar import Controller
from litestar import get
from litestar import post
from litestar.status_codes import HTTP_200_OK
from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import ModelRetry
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.api_native.chat_memory import BOT_API_GUARDS
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.chat_memory_dream import DreamInputs
from smarter_dev.web.chat_memory_dream import DreamOutcome
from smarter_dev.web.chat_memory_dream import DreamOutput
from smarter_dev.web.chat_memory_dream import GuildDreamResult
from smarter_dev.web.chat_memory_dream import commit_guild_dream
from smarter_dev.web.chat_memory_dream import dream_cutoff
from smarter_dev.web.chat_memory_dream import dream_day
from smarter_dev.web.chat_memory_dream import identity_traits
from smarter_dev.web.chat_memory_dream import prepare_guild_dream
from smarter_dev.web.chat_memory_dream import render_note_line
from smarter_dev.web.crud import guilds_needing_dream

# Midnights further back than this are not a night anyone may still be dreaming.
MAX_CUTOFF_AGE = timedelta(days=2)


class DreamGuildsRead(BaseModel):
    cutoff: datetime
    guild_ids: list[str]


class DreamNoteRead(BaseModel):
    id: str
    line: str


class DreamLimits(BaseModel):
    memory: int
    behavior: int
    personality: int
    facts: int


class DreamInputsRead(BaseModel):
    """What one guild's dream reads. ``needed`` false means nothing to do."""

    guild_id: str
    cutoff: datetime
    needed: bool
    reason: str | None = None
    day: str | None = None
    base_revision: int | None = None
    inputs_digest: str | None = None
    notes: list[DreamNoteRead] = Field(default_factory=list)
    memory: str = ""
    behavior: str = ""
    personality: str = ""
    facts: str = ""
    identity_traits: list[str] = Field(default_factory=list)
    limits: DreamLimits | None = None
    user_message: str = ""


class DreamSubmit(DreamOutput):
    cutoff: datetime
    base_revision: int | None = None
    note_ids: list[str]
    inputs_digest: str


class DreamSubmitResult(BaseModel):
    accepted: bool
    reason: str | None = None
    message: str | None = None
    revision: int | None = None
    notes_consumed: int = 0


def inputs_digest(inputs: DreamInputs) -> str:
    """A fingerprint of everything the dream read, as it stands right now.

    The revision and the note ids are not enough: a privacy purge or a retag
    rewrites note text (and block text) in place and bumps the revision only
    when a block changes. So the digest covers each note's id and content, the
    stored and withheld text of all four blocks, and the assembled message.
    """
    material = {
        "revision": inputs.revision,
        "cutoff": inputs.cutoff.isoformat(),
        "notes": [[str(note.id), note.content] for note in inputs.notes],
        "unread": sorted(str(note.id) for note in inputs.unread),
        "stored": [
            inputs.stored_blob,
            inputs.stored_behavior,
            inputs.stored_personality,
            inputs.stored_facts,
        ],
        "withheld": [
            inputs.withheld_blob,
            inputs.withheld_behavior,
            inputs.withheld_personality,
            inputs.withheld_facts,
        ],
        "message": inputs.user_message,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _check_cutoff(cutoff: datetime, now: datetime) -> bool:
    if cutoff.tzinfo is None:
        return False
    cutoff = cutoff.astimezone(UTC)
    midnight = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
    return cutoff == midnight and now - MAX_CUTOFF_AGE <= cutoff <= dream_cutoff(now)


def _skip_reason(result: GuildDreamResult) -> str:
    if result.outcome is DreamOutcome.SKIPPED_DISABLED:
        return "memory_disabled"
    return "no_notes"


class ChatMemoryDreamController(Controller):
    """Inputs and submission for the agent-run nightly dream."""

    path = "/api"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get("/chat-memory/dream/guilds", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def list_dream_guilds(self, db_session: AsyncSession) -> DreamGuildsRead:
        """Guilds the dream should visit for the night that just ended."""
        cutoff = dream_cutoff(datetime.now(UTC))
        return DreamGuildsRead(
            cutoff=cutoff, guild_ids=await guilds_needing_dream(db_session, cutoff)
        )

    @get(
        "/guilds/{guild_id:str}/chat-memory/dream",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def get_dream_inputs(
        self, db_session: AsyncSession, guild_id: str
    ) -> DreamInputsRead:
        """Everything this guild's dream reads, withheld lines already removed.

        A guild with a memory row is ``needed`` even with no notes: the agent
        dreams from its own history too. Not free of side effects when there is
        nothing to read: a night whose only notes were about opted-out members
        stamps ``last_dream_at`` and deletes them, and answers ``needed: false``
        (``no_notes``); a paused guild answers ``memory_disabled``, untouched.
        """
        now = datetime.now(UTC)
        cutoff = dream_cutoff(now)
        inputs = await prepare_guild_dream(
            db_session, guild_id=guild_id, cutoff=cutoff, dreamed_at=now
        )
        if isinstance(inputs, GuildDreamResult):
            return DreamInputsRead(
                guild_id=guild_id,
                cutoff=cutoff,
                needed=False,
                reason=_skip_reason(inputs),
            )
        context = inputs.context
        return DreamInputsRead(
            guild_id=guild_id,
            cutoff=cutoff,
            needed=True,
            day=dream_day(cutoff).isoformat(),
            base_revision=inputs.revision,
            inputs_digest=inputs_digest(inputs),
            notes=[
                DreamNoteRead(id=str(n.id), line=render_note_line(n))
                for n in inputs.notes
            ],
            memory=context.previous_blob,
            behavior=context.previous_behavior,
            personality=context.previous_personality,
            facts=context.previous_facts,
            identity_traits=identity_traits(context.previous_blob),
            limits=DreamLimits(
                memory=context.memory_limit,
                behavior=context.behavior_limit,
                personality=context.personality_limit,
                facts=context.facts_limit,
            ),
            user_message=inputs.user_message,
        )

    @post(
        "/guilds/{guild_id:str}/chat-memory/dream",
        status_code=HTTP_200_OK,
        guards=BOT_API_GUARDS,
    )
    async def submit_dream(
        self, db_session: AsyncSession, guild_id: str, data: DreamSubmit
    ) -> DreamSubmitResult:
        """Validate and store one night; a refusal writes nothing."""
        now = datetime.now(UTC)
        if not _check_cutoff(data.cutoff, now):
            return DreamSubmitResult(
                accepted=False,
                reason="stale",
                message="That night is over. Fetch the dream inputs again.",
            )
        cutoff = data.cutoff.astimezone(UTC)
        inputs = await prepare_guild_dream(
            db_session, guild_id=guild_id, cutoff=cutoff, dreamed_at=now
        )
        if isinstance(inputs, GuildDreamResult):
            return DreamSubmitResult(
                accepted=False,
                reason=_skip_reason(inputs),
                message="There is nothing to dream for this guild tonight.",
            )
        if (
            inputs.revision != data.base_revision
            or {str(n.id) for n in inputs.notes} != set(data.note_ids)
            or inputs_digest(inputs) != data.inputs_digest
        ):
            await db_session.rollback()
            return DreamSubmitResult(
                accepted=False,
                reason="stale",
                message=(
                    "Memory or tonight's notes changed since you read them. "
                    "Fetch the dream inputs again and redo the night."
                ),
            )
        output = DreamOutput(**data.model_dump(include=set(DreamOutput.model_fields)))
        try:
            result = await commit_guild_dream(
                db_session,
                inputs,
                output,
                dreamed_at=now,
                strict=True,
                model_name=_agent_model_name(),
            )
        except ModelRetry as retry:
            await db_session.rollback()
            return DreamSubmitResult(
                accepted=False, reason="invalid", message=str(retry)
            )
        if result.outcome is not DreamOutcome.DREAMED:
            await db_session.rollback()
            return DreamSubmitResult(
                accepted=False,
                reason="refused",
                message=(
                    "That night was not saved. Keep what is still true, "
                    "make only edits you can support from the notes, and try again."
                ),
            )
        return DreamSubmitResult(
            accepted=True,
            revision=result.revision,
            notes_consumed=result.notes_consumed,
        )


def _agent_model_name() -> str:
    return os.getenv("PROACTIVE_AGENT_MODEL", "proactive-agent")
