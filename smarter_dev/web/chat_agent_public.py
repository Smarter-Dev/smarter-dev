"""What the public ``/chat-agent`` page may show of the agent's memory (#103, #104).

The page shows the real personality, behavior and memory blocks with every
person masked: each ``<userid:username>`` tag (and the old ``username (id N)``
form, and any ``<@N>`` mention) becomes "a member". A line about someone who
opted out is left out first, as it is for every agent. What is left is held to
one check: no Discord id, no mention, no raw HTML and no name of a member the
agent knows. A block that still fails is hidden, not edited.

Masking only removes tagged references. A description that identifies someone
without a name ("the person who runs the Rust meetup") is not caught; the
dream is asked not to write those, and the admin reads the preview before
switching the page on.

A signed-in visitor whose account has linked Discord ids sees their own tags
by name; everyone else stays "a member". The masked blocks are the same for
every visitor, so they are what is cached, and the visitor's own names are put
back per request (:meth:`PublicBlock.shown`).

The names are the ones in reach without asking Discord: everyone the stored
blocks and the day's notes name in a tag or the old form, including the lines
about opted-out people that the dream carries over unread, and everyone who has
started a conversation with the agent in the guild. The page and its admin
preview reach further (#105), since a bare first name or display name the agent
picked up in conversation is not in any tag: every tag in every guild's notes
and memory revisions, the usernames and display names the guild's moderation,
forum and help records keep, and the Discord username and global name of every
site account linked to Discord.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from uuid import UUID

from skrift.db.models.oauth_account import OAuthAccount
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.member_tags import MemberRef
from smarter_dev.shared.member_tags import split_members
from smarter_dev.shared.member_tags import tagged_names
from smarter_dev.web.account_signups import account_contacts
from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentGuildMemory
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.models import ChatAgentMemoryRevision
from smarter_dev.web.models import ForumAgentResponse
from smarter_dev.web.models import HelpConversation
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.privacy_gate import OptOutGate
from smarter_dev.web.privacy_gate import load_gate

# What a masked person reads as.
MEMBER_PLACEHOLDER = "a member"
# A visitor's own mention, which carries no name.
VIEWER_PLACEHOLDER = "you"
# A snowflake, as the purge contract spells it (15-22 digits).
_SNOWFLAKE = re.compile(r"(?<!\d)\d{15,22}(?!\d)")
# The old way of naming a person, whatever the id's length.
_ID_TAG = re.compile(r"\(\s*id\s*\d+\s*\)", re.IGNORECASE)
_MENTION = "<@"
# Raw HTML: a tag opening or closing. The page's markdown renderer escapes it
# anyway; refusing it here means it is never shown at all.
_HTML_TAG = re.compile(r"<[A-Za-z/!]")
# Shorter than this a name is a letter, not a person.
MIN_NAME_CHARS = 2
# The blocks the page shows, in order.
PUBLIC_BLOCKS = ("personality", "behavior", "memory")


def member_names(*texts: str, usernames: Iterable[str] = ()) -> frozenset[str]:
    """Every name ``texts`` give a person (``<id:name>`` or ``name (id N)``),
    plus ``usernames``, casefolded."""
    names = {name.strip(".-") for name in tagged_names(*texts)}
    names.update(name.strip().casefold() for name in usernames if name)
    return frozenset(name for name in names if len(name) >= MIN_NAME_CHARS)


def names_in(text: str, names: frozenset[str]) -> list[str]:
    """Which of ``names`` ``text`` carries as a whole word, sorted."""
    folded = text.casefold()
    return sorted(
        name for name in names if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", folded)
    )


def _names_someone(text: str, names: frozenset[str]) -> bool:
    folded = text.casefold()
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", folded) for name in names)


def public_text_problem(text: str, names: frozenset[str]) -> str | None:
    """Why ``text`` may not be shown publicly, or ``None`` if it may."""
    if _SNOWFLAKE.search(text) or _ID_TAG.search(text):
        return "it carries a Discord id"
    if _MENTION in text:
        return "it carries a mention"
    if _HTML_TAG.search(text):
        return "it carries raw HTML"
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


async def blocked_member_names(
    session: AsyncSession, gate: OptOutGate, guild_id: str, *texts: str
) -> frozenset[str]:
    """The names of the people on the opt-out list as far as they are in
    reach: what ``texts`` call them in a tag or the old form, and the username
    each started a conversation in the guild under. Matches a tag with no id,
    ``<:name>``, to someone who opted out (#104)."""
    names = gate.blocked_names_in(*texts)
    result = await session.execute(
        select(
            ChatAgentEngagement.activation_user_id,
            ChatAgentEngagement.activation_username,
        )
        .where(ChatAgentEngagement.guild_id == guild_id)
        .distinct()
    )
    names.update(
        username.strip().casefold()
        for user_id, username in result
        if username and gate.is_blocked(user_id)
    )
    return frozenset(name for name in names if name)


async def known_member_names(
    session: AsyncSession, guild_id: str, *texts: str
) -> frozenset[str]:
    """:func:`member_names` over ``texts`` and the guild's engagement usernames."""
    return member_names(*texts, usernames=await engagement_usernames(session, guild_id))


# Moderation sources whose moderator is a person, not the bot's own account.
_HUMAN_MODERATION_SOURCES = ("manual", "audit_log")
# What the masked text itself says for a person: never a name to look for in it.
_PLACEHOLDER_WORDS = frozenset(
    {MEMBER_PLACEHOLDER, MEMBER_PLACEHOLDER.split()[-1], VIEWER_PLACEHOLDER}
)


async def stored_tagged_names(session: AsyncSession) -> set[str]:
    """Every name a tag or the old form gives a person in any guild's notes or
    memory revisions, casefolded."""
    names: set[str] = set()
    for content in await session.scalars(select(ChatAgentMemoryNote.content)):
        names |= tagged_names(content or "")
    revisions = await session.execute(
        select(
            ChatAgentMemoryRevision.content,
            ChatAgentMemoryRevision.behavior,
            ChatAgentMemoryRevision.personality,
        )
    )
    for row in revisions:
        names |= tagged_names(*(text or "" for text in row))
    return names


async def recorded_member_names(session: AsyncSession, guild_id: str) -> list[str]:
    """The names the guild's own records keep for its members: moderation
    targets and moderators, forum post authors' display names, help-agent
    users, and the Discord username and global name of every site account
    linked to Discord. Nothing here asks Discord."""
    names: list[str] = []
    moderation = await session.execute(
        select(
            ModerationAction.target_username,
            ModerationAction.moderator_username,
            ModerationAction.source,
        )
        .where(ModerationAction.guild_id == guild_id)
        .distinct()
    )
    for target, moderator, source in moderation:
        names.append(target)
        if source in _HUMAN_MODERATION_SOURCES:
            names.append(moderator)
    names.extend(
        await session.scalars(
            select(ForumAgentResponse.author_display_name)
            .where(ForumAgentResponse.guild_id == guild_id)
            .distinct()
        )
    )
    names.extend(
        await session.scalars(
            select(HelpConversation.user_username)
            .where(HelpConversation.guild_id == guild_id)
            .distinct()
        )
    )
    for metadata in await session.scalars(
        select(OAuthAccount.provider_metadata).where(OAuthAccount.provider == "discord")
    ):
        if isinstance(metadata, dict):
            names.extend(
                value
                for value in (metadata.get("username"), metadata.get("global_name"))
                if isinstance(value, str)
            )
    return [name for name in names if name]


async def page_member_names(
    session: AsyncSession, guild_id: str, *texts: str
) -> frozenset[str]:
    """Every name the public page hides a block for: :func:`known_member_names`
    and the wider reach of :func:`stored_tagged_names` and
    :func:`recorded_member_names` (#105)."""
    known = await known_member_names(session, guild_id, *texts)
    wider = member_names(
        usernames=[
            *await stored_tagged_names(session),
            *await recorded_member_names(session, guild_id),
        ]
    )
    return frozenset((known | wider) - _PLACEHOLDER_WORDS)


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


async def viewer_discord_ids(
    session: AsyncSession, user_id: UUID | None
) -> frozenset[str]:
    """The Discord ids a signed-in account has linked; none signed out."""
    if user_id is None:
        return frozenset()
    return (await account_contacts(session, user_id)).discord_ids


@dataclass(frozen=True)
class PublicBlock:
    """One block as the public page would show it: ``problem`` hides it.

    ``parts`` is the block with the opted-out lines left out, as plain text
    and the people it references; :attr:`masked` is it with every person as
    "a member", and the text every check ran on.
    """

    parts: tuple[str | MemberRef, ...] = ()
    problem: str | None = None
    # Lines left out because they name someone who opted out.
    lines_left_out: int = 0
    # The names that hid it, for the admin preview only; never on the page.
    names_found: tuple[str, ...] = ()
    raw: str = field(default="", repr=False)

    def render(self, viewer_ids: frozenset[str] = frozenset()) -> str:
        """The block with everyone masked but the people ``viewer_ids`` are."""
        out = []
        for part in self.parts:
            if isinstance(part, str):
                out.append(part)
            elif part.user_id in viewer_ids:
                out.append(part.username or VIEWER_PLACEHOLDER)
            else:
                out.append(MEMBER_PLACEHOLDER)
        return "".join(out).strip()

    @property
    def masked(self) -> str:
        return self.render()

    def shown(self, viewer_ids: frozenset[str] = frozenset()) -> str:
        """What the page shows a visitor: nothing if the block fails."""
        return "" if self.problem is not None else self.render(viewer_ids)


def public_block(
    text: str,
    names: frozenset[str],
    gate: OptOutGate,
    blocked_names: frozenset[str] = frozenset(),
) -> PublicBlock:
    """``text`` checked for the page: opted-out lines out, people masked."""
    text = (text or "").strip()
    lines = text.splitlines()
    kept = [
        line for line in lines if not gate.carries_blocked_member(line, blocked_names)
    ]
    parts = split_members("\n".join(kept))
    masked = PublicBlock(parts=parts).masked
    problem = public_text_problem(masked, names) if masked else None
    if problem is None and gate.carries_blocked_id(masked):
        problem = "it carries a blocked id"
    return PublicBlock(
        parts=parts,
        problem=problem,
        lines_left_out=len(lines) - len(kept),
        names_found=tuple(names_in(masked, names)) if masked else (),
        raw=text,
    )


async def check_public_blocks(
    session: AsyncSession, memory: ChatAgentGuildMemory
) -> dict[str, PublicBlock]:
    """Personality, behavior and memory, masked and each checked against every
    name :func:`page_member_names` knows, and the opt-out gate."""
    gate = await load_gate(session)
    texts = {
        "personality": memory.personality or "",
        "behavior": memory.behavior or "",
        "memory": memory.content or "",
    }
    names = await page_member_names(session, memory.guild_id, *texts.values())
    blocked = await blocked_member_names(
        session, gate, memory.guild_id, *texts.values()
    )
    return {
        name: public_block(texts[name], names, gate, blocked) for name in PUBLIC_BLOCKS
    }
