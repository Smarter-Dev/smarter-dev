"""What the public ``/chat-agent`` page may show of the agent's memory (#103).

The memory block is about the people in a guild, so the page never shows it.
The nightly dream writes a fourth block beside it, the same memory with every
person removed, and both the dream and the page hold that block (and the
personality and behavior blocks) to one check: no Discord id, no mention, and
no name of a member the agent knows.

The names are the ones in reach without asking Discord: everyone the memory
and the day's notes name in the dream's own ``username (id 123)`` form,
including the lines about opted-out people that the dream carries over unread,
and everyone who has started a conversation with the agent in the guild.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentGuildMemory
from smarter_dev.web.privacy_gate import load_gate

# A snowflake, as the purge contract spells it (15-22 digits).
_SNOWFLAKE = re.compile(r"(?<!\d)\d{15,22}(?!\d)")
# The dream's way of naming a person, whatever the id's length.
_ID_TAG = re.compile(r"\(\s*id\s*\d+\s*\)", re.IGNORECASE)
_NAMED_WITH_ID = re.compile(r"([\w.\-]{2,32})\s*\(\s*id\s*\d+\s*\)", re.IGNORECASE)
_MENTION = "<@"
# Shorter than this a name is a letter, not a person.
MIN_NAME_CHARS = 2


def member_names(*texts: str, usernames: Iterable[str] = ()) -> frozenset[str]:
    """Every name in ``texts`` written as ``name (id N)``, plus ``usernames``,
    casefolded."""
    names = {
        match.group(1).strip(".-").casefold()
        for text in texts
        for match in _NAMED_WITH_ID.finditer(text or "")
    }
    names.update(name.strip().casefold() for name in usernames if name)
    return frozenset(name for name in names if len(name) >= MIN_NAME_CHARS)


def _names_someone(text: str, names: frozenset[str]) -> bool:
    folded = text.casefold()
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", folded) for name in names)


def public_text_problem(text: str, names: frozenset[str]) -> str | None:
    """Why ``text`` may not be shown publicly, or ``None`` if it may."""
    if _SNOWFLAKE.search(text) or _ID_TAG.search(text):
        return "it carries a Discord id"
    if _MENTION in text:
        return "it carries a mention"
    if _names_someone(text, names):
        return "it names a member"
    return None


async def engagement_usernames(session: AsyncSession, guild_id: str) -> list[str]:
    """Everyone who has started a conversation with the agent in the guild."""
    result = await session.execute(
        select(ChatAgentEngagement.activation_username)
        .where(ChatAgentEngagement.guild_id == guild_id)
        .distinct()
    )
    return [name for name in result.scalars() if name]


async def known_member_names(
    session: AsyncSession, guild_id: str, *texts: str
) -> frozenset[str]:
    """:func:`member_names` over ``texts`` and the guild's engagement usernames."""
    return member_names(*texts, usernames=await engagement_usernames(session, guild_id))


async def public_guild_memory(session: AsyncSession) -> ChatAgentGuildMemory | None:
    """The guild the page is switched on for, if any."""
    result = await session.execute(
        select(ChatAgentGuildMemory)
        .where(ChatAgentGuildMemory.public_page_enabled.is_(True))
        .order_by(ChatAgentGuildMemory.guild_id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def set_public_page(
    session: AsyncSession, memory: ChatAgentGuildMemory, *, enabled: bool
) -> None:
    """Switch the page on for ``memory``'s guild (and off for every other), or off.

    The page shows one guild, so switching one on switches the rest off.
    The caller commits.
    """
    if enabled:
        await session.execute(
            update(ChatAgentGuildMemory)
            .where(ChatAgentGuildMemory.guild_id != memory.guild_id)
            .values(public_page_enabled=False)
        )
    memory.public_page_enabled = enabled


@dataclass(frozen=True)
class PublicBlock:
    """One block as the public page would show it: ``problem`` hides it."""

    text: str
    problem: str | None

    @property
    def shown(self) -> str:
        return self.text if self.problem is None else ""


async def check_public_blocks(
    session: AsyncSession, memory: ChatAgentGuildMemory
) -> dict[str, PublicBlock]:
    """Personality, behavior and public memory, each checked against every
    member the guild's stored blocks and engagements name, and the opt-out
    gate."""
    gate = await load_gate(session)
    names = await known_member_names(
        session,
        memory.guild_id,
        memory.content or "",
        memory.behavior or "",
        memory.personality or "",
    )
    blocks: dict[str, PublicBlock] = {}
    for name, text in (
        ("personality", memory.personality),
        ("behavior", memory.behavior),
        ("public_memory", memory.public_content),
    ):
        text = (text or "").strip()
        problem = public_text_problem(text, names) if text else None
        if problem is None and gate.carries_blocked_id(text):
            problem = "it carries a blocked id"
        blocks[name] = PublicBlock(text=text, problem=problem)
    return blocks
