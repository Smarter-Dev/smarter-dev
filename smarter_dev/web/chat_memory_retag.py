"""Rewrite stored chat-agent memory into the member tag form, once (#104).

Before #104 the agent named people as ``username (id N)``; now it writes
``<N:username>`` so the public page can mask them. This converts what is
already stored, for every guild: the memory, behavior and personality blocks
and every pending note. A ``<@N>`` mention becomes a tag too when its name is
known, from an old-form or tagged reference to the same id in the same text or
from the username that id started a conversation under in the guild; one
whose name is not known is left as it is. Tags already there, id-less ones
included, are never touched and none is invented, so a second run converts
nothing. Plain text rewrite, no model call.

A rewrite can change a column's length. When any column of a guild would go
over its cap, the guild is refused whole and left as it was, never truncated.

Applying writes each guild in its own transaction, under the lock the dream
and the admin purge share, and records a memory revision as the dream does,
so the admin page's history shows the rewrite. ``last_dream_at`` is left as
it was. Only counts are reported, never the text.

The admin chat-memory page tags one name by hand the same way
(:func:`tag_name_in_blocks`, #105): a bare name the agent wrote that the page
hides a block for becomes a tag in the three blocks, with a revision recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.member_tags import MAX_TAG_NAME_CHARS
from smarter_dev.shared.member_tags import names_outside_tags
from smarter_dev.shared.member_tags import referenced_members
from smarter_dev.shared.member_tags import retag
from smarter_dev.shared.member_tags import tag_name
from smarter_dev.web.chat_agent_public import member_names
from smarter_dev.web.crud import lock_guild_memory
from smarter_dev.web.crud import prune_memory_revisions
from smarter_dev.web.crud import record_memory_revision
from smarter_dev.web.models import MAX_BEHAVIOR_CHARS
from smarter_dev.web.models import MAX_MEMORY_BLOB_CHARS
from smarter_dev.web.models import MAX_MEMORY_NOTE_CHARS
from smarter_dev.web.models import MAX_PERSONALITY_CHARS
from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentGuildMemory
from smarter_dev.web.models import ChatAgentMemoryNote

RETAG_MODEL_NAME = "retag (#104)"
# Each memory column and its cap; notes share one cap.
BLOCK_LIMITS = {
    "content": MAX_MEMORY_BLOB_CHARS,
    "behavior": MAX_BEHAVIOR_CHARS,
    "personality": MAX_PERSONALITY_CHARS,
}
NOTES = "notes"


def retag_text(text: str, engagement_names: dict[str, str]) -> tuple[str, int]:
    """:func:`retag` with mentions resolved from ``text`` itself first, then
    from the guild's engagement usernames."""
    names = dict(engagement_names)
    names.update(
        (ref.user_id, ref.username)
        for ref in referenced_members(text)
        if ref.user_id and ref.username
    )
    return retag(text, names)


@dataclass
class GuildRetag:
    """One guild's rewrite: counts per column, and whether it may be written."""

    guild_id: str
    converted: dict[str, int] = field(default_factory=dict)
    # Known members still named outside a tag after the rewrite, per column.
    untagged: dict[str, int] = field(default_factory=dict)
    # Columns that would go over their cap; any refuses the guild.
    over_cap: list[str] = field(default_factory=list)
    blocks: dict[str, str] = field(default_factory=dict)
    notes: dict[object, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.converted.values())

    @property
    def refused(self) -> bool:
        return bool(self.over_cap)


async def _engagement_names(session: AsyncSession, guild_id: str) -> dict[str, str]:
    result = await session.execute(
        select(
            ChatAgentEngagement.activation_user_id,
            ChatAgentEngagement.activation_username,
        )
        .where(ChatAgentEngagement.guild_id == guild_id)
        .order_by(ChatAgentEngagement.started_at)
    )
    # The latest username an id went by wins.
    return {user_id: name for user_id, name in result if user_id and name}


async def plan_guild(session: AsyncSession, guild_id: str) -> GuildRetag:
    """What rewriting ``guild_id`` would change; writes nothing."""
    plan = GuildRetag(guild_id=guild_id)
    memory = (
        await session.execute(
            select(ChatAgentGuildMemory).where(
                ChatAgentGuildMemory.guild_id == guild_id
            )
        )
    ).scalar_one_or_none()
    notes = (
        await session.scalars(
            select(ChatAgentMemoryNote).where(ChatAgentMemoryNote.guild_id == guild_id)
        )
    ).all()
    engagement_names = await _engagement_names(session, guild_id)

    stored = {
        column: (getattr(memory, column) or "") if memory is not None else ""
        for column in BLOCK_LIMITS
    }
    rewritten: dict[str, str] = {}
    for column, text in stored.items():
        new, count = retag_text(text, engagement_names)
        plan.converted[column] = count
        rewritten[column] = new
        if count:
            plan.blocks[column] = new
        if len(new) > BLOCK_LIMITS[column]:
            plan.over_cap.append(column)
    plan.converted[NOTES] = 0
    for note in notes:
        new, count = retag_text(note.content, engagement_names)
        plan.converted[NOTES] += count
        if count:
            plan.notes[note.id] = new
        if len(new) > MAX_MEMORY_NOTE_CHARS and NOTES not in plan.over_cap:
            plan.over_cap.append(NOTES)

    known = member_names(
        *rewritten.values(),
        *(plan.notes.get(note.id, note.content) for note in notes),
        usernames=engagement_names.values(),
    )
    for column, text in rewritten.items():
        plan.untagged[column] = len(names_outside_tags(text, known))
    plan.untagged[NOTES] = sum(
        len(names_outside_tags(plan.notes.get(note.id, note.content), known))
        for note in notes
    )
    return plan


async def apply_guild(session: AsyncSession, guild_id: str) -> GuildRetag:
    """Rewrite ``guild_id`` and commit, unless it is refused or unchanged.

    Planned under the guild's memory lock, so a dream or purge cannot change
    the blocks between the read and the write. The caller's session must have
    no transaction open on another guild.
    """
    await lock_guild_memory(session, guild_id)
    plan = await plan_guild(session, guild_id)
    if plan.refused or plan.total == 0:
        await session.rollback()
        return plan
    if plan.blocks:
        memory = (
            await session.execute(
                select(ChatAgentGuildMemory).where(
                    ChatAgentGuildMemory.guild_id == guild_id
                )
            )
        ).scalar_one()
        for column, text in plan.blocks.items():
            setattr(memory, column, text)
        memory.revision += 1
        await record_memory_revision(
            session,
            guild_id=guild_id,
            content=memory.content,
            behavior=memory.behavior or "",
            personality=memory.personality or "",
            revision=memory.revision,
            notes_consumed=0,
            model_name=RETAG_MODEL_NAME,
        )
        await prune_memory_revisions(session, guild_id)
    if plan.notes:
        for note in (
            await session.scalars(
                select(ChatAgentMemoryNote).where(
                    ChatAgentMemoryNote.id.in_(list(plan.notes))
                )
            )
        ).all():
            note.content = plan.notes[note.id]
    await session.commit()
    return plan


async def guild_ids(session: AsyncSession) -> list[str]:
    """Every guild with a memory row or a pending note."""
    with_memory = (await session.scalars(select(ChatAgentGuildMemory.guild_id))).all()
    with_notes = (
        await session.scalars(select(ChatAgentMemoryNote.guild_id).distinct())
    ).all()
    return sorted({*with_memory, *with_notes})


# -- tagging one name by hand (#105) -------------------------------------------

ADMIN_TAG_MODEL_NAME = "admin tag (#105)"
# A Discord id, as the purge contract spells it.
_SNOWFLAKE = re.compile(r"[0-9]{15,22}")
# What may not sit in a tag's name; an id-less tag cannot hold a colon either.
_NOT_IN_TAG = re.compile(r"[<>\n\r]")
MIN_TAG_NAME_CHARS = 2


def tag_request_problem(username: str, user_id: str) -> str | None:
    """Why the admin's "tag this name" form cannot be applied, or ``None``."""
    if len(username) < MIN_TAG_NAME_CHARS:
        return f"Enter a name of at least {MIN_TAG_NAME_CHARS} characters."
    if len(username) > MAX_TAG_NAME_CHARS:
        return f"A name can be at most {MAX_TAG_NAME_CHARS} characters."
    if _NOT_IN_TAG.search(username):
        return "A name cannot hold < or > or a line break."
    if user_id and not _SNOWFLAKE.fullmatch(user_id):
        return "A Discord id is 15 to 22 digits."
    if not user_id and ":" in username:
        return "A name with a colon needs a Discord id."
    return None


@dataclass
class NameTagging:
    """What tagging one name in a guild's three blocks did."""

    tagged: dict[str, int] = field(default_factory=dict)
    # Columns that would go over their cap; any refuses the whole change.
    over_cap: list[str] = field(default_factory=list)
    found: bool = True

    @property
    def total(self) -> int:
        return sum(self.tagged.values())


async def tag_name_in_blocks(
    session: AsyncSession, guild_id: str, username: str, user_id: str | None
) -> NameTagging:
    """Tag every whole-word ``username`` in the guild's memory, behavior and
    personality, and commit, recording a memory revision as the retag does.

    Under the guild's memory lock, so a dream or purge cannot write between
    the read and the write. Notes are left alone. A change that would put any
    block over its cap is refused whole, and nothing is written when there is
    nothing to tag.
    """
    result = NameTagging()
    await lock_guild_memory(session, guild_id)
    memory = (
        await session.execute(
            select(ChatAgentGuildMemory).where(
                ChatAgentGuildMemory.guild_id == guild_id
            )
        )
    ).scalar_one_or_none()
    if memory is None:
        result.found = False
        await session.rollback()
        return result
    rewritten: dict[str, str] = {}
    for column, limit in BLOCK_LIMITS.items():
        new, count = tag_name(getattr(memory, column) or "", username, user_id)
        result.tagged[column] = count
        if count:
            rewritten[column] = new
            if len(new) > limit:
                result.over_cap.append(column)
    if result.over_cap or not rewritten:
        await session.rollback()
        return result
    for column, text in rewritten.items():
        setattr(memory, column, text)
    memory.revision += 1
    await record_memory_revision(
        session,
        guild_id=guild_id,
        content=memory.content,
        behavior=memory.behavior or "",
        personality=memory.personality or "",
        revision=memory.revision,
        notes_consumed=0,
        model_name=ADMIN_TAG_MODEL_NAME,
    )
    await prune_memory_revisions(session, guild_id)
    await session.commit()
    return result
