"""Service-to-service memory contract for the site's assistant and public page."""

from __future__ import annotations

import base64
import binascii
import re
from datetime import UTC
from datetime import datetime
from typing import Annotated
from uuid import UUID

from litestar import Controller
from litestar import get
from litestar import post
from litestar.middleware import DefineMiddleware
from litestar.params import Parameter
from pydantic import ConfigDict
from pydantic import Field
from skrift.auth.guards import Permission
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.member_tags import referenced_members
from smarter_dev.shared.privacy_purge import SNOWFLAKE_PATTERN
from smarter_dev.web.api_native.chat_memory import utc_day_start
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.errors import plain_error
from smarter_dev.web.api_native.rate_limiting import MultiTierRateLimitMiddleware
from smarter_dev.web.api_native.schemas import BaseAPIModel
from smarter_dev.web.api_native.schemas import ChatMemoryNoteSaveResult
from smarter_dev.web.api_native.site_guards import SITE_API_GUARDS
from smarter_dev.web.chat_agent_public import blocked_member_names
from smarter_dev.web.chat_agent_public import cached_public_blocks
from smarter_dev.web.chat_agent_public import public_guild_memory
from smarter_dev.web.crud import count_notes_since
from smarter_dev.web.crud import create_memory_note
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import lock_guild_memory
from smarter_dev.web.models import MAX_MEMORY_NOTE_CHARS
from smarter_dev.web.models import MAX_NOTES_PER_GUILD_PER_DAY
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.models import ChatBotPurgeRequest
from smarter_dev.web.privacy_gate import OptOutGate
from smarter_dev.web.privacy_gate import load_gate

SITE_CHANNEL_ID = "site"
_MAX_GUILD_ID_CHARS = ChatAgentMemoryNote.__table__.c.guild_id.type.length
_SCAN_PAGE = 200
MEMBER_NOTE_SCAN_LIMIT = 1000


class SiteNoteCreate(BaseAPIModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=MAX_MEMORY_NOTE_CHARS)


class SiteBlocksRead(BaseAPIModel):
    guild_id: str
    content: str | None = None
    behavior: str | None = None
    personality: str | None = None
    last_dream_at: datetime | None = None
    revision: int | None = None
    memory_enabled: bool = True


class SiteNoteRead(BaseAPIModel):
    id: UUID
    created_at: datetime
    channel_name: str | None
    content: str
    source: str


class SiteMemberRead(BaseAPIModel):
    notes: list[SiteNoteRead]
    next_before: str | None = None


class SitePublicRead(BaseAPIModel):
    guild_id: str
    personality: str
    behavior: str
    memory: str
    last_dream_at: datetime | None
    revision: int
    # Whether each block has anything to show once masked, as the /chat-agent
    # page's "not shown right now" notice reads it; viewer-independent.
    has_personality: bool
    has_behavior: bool
    has_memory: bool


def _discord_id(value: str) -> str:
    if not re.fullmatch(SNOWFLAKE_PATTERN, value):
        raise plain_error(422, "Invalid Discord ID")
    return value


def _guild_id(value: str) -> str:
    _discord_id(value)
    if len(value) > _MAX_GUILD_ID_CHARS:
        raise plain_error(422, "Invalid guild ID")
    return value


def _cursor(note: ChatAgentMemoryNote) -> str:
    at = note.created_at
    if at.tzinfo is None:  # SQLite round trips omit the UTC offset.
        at = at.replace(tzinfo=UTC)
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{note.id}".encode()).decode()


def _parse_cursor(before: str) -> tuple[datetime, UUID]:
    try:
        at, note_id = (
            base64.b64decode(before, altchars=b"-_", validate=True).decode().split("|")
        )
        moment = datetime.fromisoformat(at)
        if moment.tzinfo is None:
            raise ValueError("Timezone required")
        if not (
            datetime.min.replace(tzinfo=UTC)
            <= moment
            <= datetime.max.replace(tzinfo=UTC)
        ):
            raise ValueError("Cursor is outside the UTC datetime range")
        return moment.astimezone(UTC), UUID(note_id)
    except (ValueError, UnicodeError, binascii.Error, OverflowError) as error:
        raise plain_error(422, "Invalid before cursor") from error


def _older_than(moment: datetime, note_id: UUID):
    return or_(
        ChatAgentMemoryNote.created_at < moment,
        and_(
            ChatAgentMemoryNote.created_at == moment, ChatAgentMemoryNote.id < note_id
        ),
    )


def _without_blocked_lines(text: str, gate: OptOutGate, names: frozenset[str]) -> str:
    return "\n".join(
        line
        for line in text.splitlines()
        if not gate.carries_blocked_member(line, names)
    ).strip()


async def _member_gate(session: AsyncSession, user_id: str) -> OptOutGate:
    _discord_id(user_id)
    gate = await load_gate(session)
    # An open request remains unavailable through failure and human review,
    # even if its block-list row has not been written yet.
    purge = await session.scalar(
        select(ChatBotPurgeRequest.id)
        .where(
            ChatBotPurgeRequest.discord_user_id == user_id,
            ChatBotPurgeRequest.status != "closed",
        )
        .limit(1)
    )
    if gate.is_blocked(user_id) or purge is not None:
        raise plain_error(404, "Member memory not found")
    return gate


class SiteMemoryController(Controller):
    path = "/api/site/guilds/{guild_id:str}/memory"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS
    middleware = [DefineMiddleware(MultiTierRateLimitMiddleware)]
    # Private memory and personalized public renders must never enter a CDN cache.
    response_headers = {"Cache-Control": "private, no-store"}

    @get("/blocks", guards=[*SITE_API_GUARDS, Permission("site-memory")])
    async def blocks(self, db_session: AsyncSession, guild_id: str) -> SiteBlocksRead:
        _guild_id(guild_id)
        memory = await get_guild_memory_blob(db_session, guild_id)
        if memory is None:
            return SiteBlocksRead(guild_id=guild_id)
        if not memory.memory_enabled:
            return SiteBlocksRead(guild_id=guild_id, memory_enabled=False)
        gate = await load_gate(db_session)
        texts = [memory.content or "", memory.behavior or "", memory.personality or ""]
        names = await blocked_member_names(db_session, gate, guild_id, *texts)
        content, behavior, personality = (
            _without_blocked_lines(text, gate, names) or None for text in texts
        )
        return SiteBlocksRead(
            guild_id=guild_id,
            content=content,
            behavior=behavior,
            personality=personality,
            revision=memory.revision,
            last_dream_at=memory.last_dream_at,
        )

    @get(
        "/members/{discord_user_id:str}",
        guards=[*SITE_API_GUARDS, Permission("site-memory")],
    )
    async def member_notes(
        self,
        db_session: AsyncSession,
        guild_id: str,
        discord_user_id: str,
        limit: Annotated[int, Parameter(ge=1, le=100)] = 25,
        before: Annotated[str | None, Parameter(max_length=200)] = None,
    ) -> SiteMemberRead:
        _guild_id(guild_id)
        gate = await _member_gate(db_session, discord_user_id)
        position = _parse_cursor(before) if before is not None else None
        found: list[tuple[ChatAgentMemoryNote, str]] = []
        scanned = 0
        scan_cursor = None
        while len(found) <= limit and scanned < MEMBER_NOTE_SCAN_LIMIT:
            batch_limit = min(_SCAN_PAGE, MEMBER_NOTE_SCAN_LIMIT - scanned)
            query = select(ChatAgentMemoryNote).where(
                ChatAgentMemoryNote.guild_id == guild_id,
                # Only a candidate filter; the shared parser checks exact tags
                # below, so a substring, bare ID or another member cannot match.
                ChatAgentMemoryNote.content.contains(discord_user_id),
            )
            if position is not None:
                query = query.where(_older_than(*position))
            rows = list(
                (
                    await db_session.scalars(
                        query.order_by(
                            ChatAgentMemoryNote.created_at.desc(),
                            ChatAgentMemoryNote.id.desc(),
                        ).limit(batch_limit)
                    )
                ).all()
            )
            scanned += len(rows)
            if not rows:
                break
            names = await blocked_member_names(
                db_session, gate, guild_id, *(row.content for row in rows)
            )
            for row in rows:
                content = _without_blocked_lines(row.content, gate, names)
                if any(
                    ref.user_id == discord_user_id
                    for ref in referenced_members(content)
                ):
                    found.append((row, content))
                    if len(found) > limit:
                        break
            position = rows[-1].created_at, rows[-1].id
            if len(rows) < batch_limit:
                break
            if scanned == MEMBER_NOTE_SCAN_LIMIT:
                # Exact matching/privacy can hide every candidate in the batch.
                # Continue after what we examined, even for an empty visible page.
                scan_cursor = _cursor(rows[-1])
                break
        return SiteMemberRead(
            notes=[
                SiteNoteRead(
                    id=row.id,
                    created_at=row.created_at,
                    channel_name=row.channel_name,
                    content=content,
                    source="site" if row.channel_id == SITE_CHANNEL_ID else "discord",
                )
                for row, content in found[:limit]
            ],
            next_before=_cursor(found[limit - 1][0])
            if len(found) > limit
            else scan_cursor,
        )

    @post(
        "/members/{discord_user_id:str}/notes",
        status_code=200,
        guards=[*SITE_API_GUARDS, Permission("site-memory")],
    )
    async def create_note(
        self,
        db_session: AsyncSession,
        guild_id: str,
        discord_user_id: str,
        data: SiteNoteCreate,
    ) -> ChatMemoryNoteSaveResult:
        _guild_id(guild_id)
        await lock_guild_memory(db_session, guild_id)
        gate = await _member_gate(db_session, discord_user_id)
        content = data.content
        if not any(
            ref.user_id == discord_user_id for ref in referenced_members(content)
        ):
            # Keep the association in the same format every memory reader knows,
            # without inventing a username or a separate memory layer.
            content = f"<@{discord_user_id}> {content}"
        if len(content) > MAX_MEMORY_NOTE_CHARS:
            raise plain_error(422, "Note including member reference is too long")
        names = await blocked_member_names(db_session, gate, guild_id, content)
        if gate.carries_blocked_member(content, names):
            raise plain_error(404, "Member memory not found")
        now = datetime.now(UTC)
        day_start = utc_day_start(now)
        note = await create_memory_note(
            db_session,
            guild_id=guild_id,
            channel_id=SITE_CHANNEL_ID,
            channel_name="site",
            content=content,
            created_at=now,
            day_start=day_start,
            about_user_ids=[discord_user_id],
            opt_out_checked=True,
        )
        if note is None:
            count = await count_notes_since(db_session, guild_id, day_start)
            return ChatMemoryNoteSaveResult(
                saved=False,
                reason=(
                    "daily_cap" if count >= MAX_NOTES_PER_GUILD_PER_DAY else "duplicate"
                ),
            )
        result = ChatMemoryNoteSaveResult(
            saved=True, id=note.id, created_at=note.created_at
        )
        await db_session.commit()
        return result

    @get("/public", guards=[*SITE_API_GUARDS, Permission("site-memory")])
    async def public(
        self,
        db_session: AsyncSession,
        guild_id: str,
        viewer_discord_ids: Annotated[str, Parameter(max_length=2300)] = "",
    ) -> SitePublicRead:
        _guild_id(guild_id)
        return await _public_memory(db_session, viewer_discord_ids, guild_id)


async def _public_memory(
    db_session: AsyncSession, viewer_discord_ids: str, guild_id: str | None = None
) -> SitePublicRead:
    viewer_ids = frozenset(
        _discord_id(value.strip())
        for value in viewer_discord_ids.split(",")
        if value.strip()
    )
    memory = await public_guild_memory(db_session)
    if memory is None or (guild_id is not None and memory.guild_id != guild_id):
        raise plain_error(404, "Public memory not found")
    blocks = await cached_public_blocks(db_session, memory)
    return SitePublicRead(
        guild_id=memory.guild_id,
        revision=memory.revision,
        last_dream_at=memory.last_dream_at,
        **{name: block.shown(viewer_ids) for name, block in blocks.items()},
        **{f"has_{name}": bool(block.masked) for name, block in blocks.items()},
    )


class SitePublicMemoryController(Controller):
    path = "/api/site/memory"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS
    middleware = [DefineMiddleware(MultiTierRateLimitMiddleware)]
    response_headers = {"Cache-Control": "private, no-store"}

    @get("/public", guards=[*SITE_API_GUARDS, Permission("site-memory")])
    async def public(
        self,
        db_session: AsyncSession,
        viewer_discord_ids: Annotated[str, Parameter(max_length=2300)] = "",
    ) -> SitePublicRead:
        return await _public_memory(db_session, viewer_discord_ids)
