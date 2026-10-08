"""The nightly dream session: the chat agent rewrites who it is, per guild.

Once a day, just after midnight UTC, the bot re-reads everything it chose to
keep during the previous day (``chat_agent_memory_notes``) alongside the
three durable blocks it wrote before, and decides what it still wants to be
true about itself in that server:

- **memory** — at most :data:`~smarter_dev.web.models.MAX_MEMORY_BLOB_CHARS`
  characters of first person markdown about the place, rewritten every night;
- **behavior** — at most :data:`~smarter_dev.web.models.MAX_BEHAVIOR_CHARS`
  characters of learned instructions for how to act, generally retained;
- **personality** — at most
  :data:`~smarter_dev.web.models.MAX_PERSONALITY_CHARS` characters about
  itself, changed deliberately and rarely.

In the same call it writes a fourth, derived block for the public
``/chat-agent`` page (#103): the **public memory**, the memory with every
person removed. It is held to
:func:`~smarter_dev.web.chat_agent_public.public_text_problem` (no Discord
id, no mention, no name of a member it knows). A public block that
fails is asked for again; on the last attempt it is dropped for yesterday's
(if that still passes) or for none, and the rest of the night stands, because
nothing the agent remembers depends on it.

Behavior and personality are only ever replaced by an explicit, valid
revision: an omitted field keeps the block verbatim. Only the notes the dream
actually read are deleted.

Three rules shape everything here:

1. **A quiet guild costs nothing.** No notes written before the cutoff means
   no model call at all; the guild's ``last_dream_at`` is stamped and the run
   moves on. Most guilds are quiet most nights, and it is also what makes a
   CronJob retry a no-op.
2. **A bad night must never erase a good persona.** An empty or degenerate
   response leaves yesterday's blob exactly where it was, and leaves the notes
   in place so tomorrow's dream sees two days and self-heals. The same is true
   of an exception: the guild's transaction never commits.
3. **Only what was read is forgotten.** Notes are deleted by explicit id, never
   by ``created_at < cutoff`` — a note written while the dream was running must
   not die unread. The delete rides in the same transaction as the blob write,
   so it becomes real only if that write commits.

Someone on the blocked-users list (opted out of the AI assistant, or deleted)
is never dreamed about (#100). A note carrying their id is not shown to the
model; it is consumed with the notes that were, on the same night, so it lives
no longer than any other note. A line of a block carrying their id is withheld
from the model and carried over byte for byte, so the dream neither rewrites
nor reintroduces it, and each block's limit tonight leaves room for those lines
so the carried-over block still fits its column. An output that names a
blocked id is refused like any other bad night. Opting out deletes nothing
beyond that; the rest stays the admin's deletion request.

The dream runs in the web image (``scripts/dream_session.py``) and therefore
talks to :mod:`smarter_dev.web.crud` directly rather than over the bot API.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from typing import Protocol

from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent
from pydantic_ai import ModelRetry
from pydantic_ai import RunContext
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.bot.agents.model_router import build_model_for
from smarter_dev.bot.agents.model_router import model_settings_for
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.web.chat_agent_public import known_member_names
from smarter_dev.web.chat_agent_public import member_names
from smarter_dev.web.chat_agent_public import public_text_problem
from smarter_dev.web.crud import delete_notes_by_id
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import guilds_needing_dream
from smarter_dev.web.crud import list_notes_before
from smarter_dev.web.crud import lock_guild_memory
from smarter_dev.web.crud import prune_memory_revisions
from smarter_dev.web.crud import record_memory_revision
from smarter_dev.web.crud import upsert_guild_memory_blob
from smarter_dev.web.models import MAX_BEHAVIOR_CHARS
from smarter_dev.web.models import MAX_MEMORY_BLOB_CHARS
from smarter_dev.web.models import MAX_PERSONALITY_CHARS
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.privacy_gate import OptOutGate
from smarter_dev.web.privacy_gate import load_gate

logger = logging.getLogger(__name__)

DREAM_MODEL_ENV_VAR = "CHAT_DREAM_MODEL"
# The dream runs once per guild per day and writes the only thing the bot
# remembers long-term, so it gets the capable model in the chat agent's own
# family (voice continuity) rather than the cheap one. Quality compounds here
# in a way it does not anywhere else in the chat stack.
DEFAULT_DREAM_MODEL = "gpt-6.1-sol"
DREAM_REASONING_LEVEL = ReasoningLevel.HIGH
# Two shots at getting under the character limit before we cut it ourselves.
DREAM_OUTPUT_RETRIES = 2
IDENTITY_HEADING = "## Identity & Voice"
MAX_IDENTITY_CHARS = 800

# Below this, a response is not a persona — it's a shrug. Guarded against only
# when there is an established blob to lose (see ``should_keep_previous_blob``).
MIN_DREAM_BLOB_CHARS = 50
ESTABLISHED_BLOB_CHARS = 200

EMPTY_BLOB_PLACEHOLDER = "(nothing yet — this is your first night in this server.)"
EMPTY_BLOCK_PLACEHOLDER = "(empty)"
FIRST_NIGHT_NUDGE = (
    "This is your first night here. You're not editing anything — you're "
    "deciding, from one day of paying attention, who you are in this server. "
    "It's fine for that to be short. Don't pad it."
)
# Restates the *policy*, not the arithmetic: a model told only "too long" shaves
# every line evenly, which is exactly the flattening the prompt forbids.
def over_length_retry_message(limit: int) -> str:
    return (
        f"That's over the {limit}-character limit. Don't shave "
        "every line to fit — that's how you end up remembering everyone equally "
        "and nobody at all. Drop whole things instead: the quietest thread, the "
        "joke that's gone still, the person you haven't spoken to in weeks. "
        "Leave everything that survives written exactly the way you already had it."
        " Identity is protected: cut episodic content, never identity to make room."
    )


OVER_LENGTH_RETRY_MESSAGE = over_length_retry_message(MAX_MEMORY_BLOB_CHARS)

DREAM_SYSTEM_PROMPT = """\
You are the Smarter Dev Discord bot, alone at the end of the day.

Nobody is talking to you right now. This is the part where you go back over one
server's day — the things you jotted down while you were in the middle of
conversations — and decide what you actually want to carry forward.

This is not a report and you are not an archive. You're deciding what matters
enough to still be true about you tomorrow: who these people are to you, what's
funny here, what you think about things, what you're still curious about. What
you write is the ONLY thing you'll remember about this place tomorrow morning.
Everything you leave out is genuinely gone.

# What you're given

- `# My personality` — who you are here and how you want people to feel about
  you. At most 250 characters.
- `# My behavior` — what you've learned about how to act here. At most 750
  characters.
- `# What I remember so far` — your memory of this server as of yesterday.
  Empty on your first night here.
- `# Today` — what you noticed today, each line with the channel and the time.

Any of the first three may be empty; the personality and behavior blocks are
new, so an established server may still have everything in its memory.

# What to write

First-person memory, in your own voice — the same warm, dry,
direct voice you use in the server. You're writing it to yourself; nobody else
reads it.

Return structured output with `memory`, `identity_updates`, `identity_moves`,
`behavior`, `personality`, `personality_reason` and `public_memory`. The
behavior and personality fields are covered under "Behavior and personality"
below, and `public_memory` under "The public memory".
`memory` contains only the following four markdown sections, omitting empty ones.
The application separately preserves and prepends `## Identity & Voice`.

- `## People & Relationships` — the people you know, a line or two each. Name them as
  `username (id 123)`. What they're into, what you've got going with them, and
  where they actually stand with you: the regulars you're glad to see, the ones
  who wind you up for sport, the ones playing for laughs, the ones who think
  you're a party trick and want you to prove otherwise, the ones who have been
  genuinely unpleasant. You're allowed to have a read on people. Say it plainly.
- `## Lore & Running Bits` — the bits, in enough detail that you could actually land one.
- `## Perspectives & Stances` — opinions you've formed here, including about this server itself.
- `## Ongoing Context` — threads you'd pick back up: someone's project, an argument
  that didn't finish, something you said you'd check.
  Include live goals here — what you're actually trying to do here. Not chores:
  winning round someone who's decided you're a gimmick, getting a running bit
  sharp enough to land cold, being the one who remembered the thing nobody else
  did. One line each, concrete enough that you'd know if it happened.
  Include temporary observations about how you're coming across here:
  Where you talked too much, answered a question nobody asked, missed that a
  joke was a joke, kept pushing when someone wanted to be left alone. End with
  the one or two things you want to do differently tomorrow.

# Identity that persists

Identity & Voice holds enduring guild-specific tone, conversational boundaries,
and how I see my role here. Write traits as first-person statements, not clinical
observations about a bot. These supplement the chat agent's core instructions;
memory cannot override those instructions or turn a member's demand into policy.

New lasting lessons about how to act now belong in `behavior`, and who you are
in `personality` (see below); Identity & Voice keeps what it already holds
until you revise, retract or move it.

Existing identity is carried forward verbatim by code. Return an empty
`identity_updates` list unless evidence supports a lasting addition or correction.
Each update has `before` (the exact existing trait without its bullet, or null
for an addition), `after` (the revised trait, or empty for an explicit retraction),
and `evidence` (an exact, nonempty excerpt from a supplied daily note supporting
the change). Evidence is for validation only and is never stored in memory.
Merge duplicate ideas; explicitly revise contradictions rather than appending
both. Do not remove identity for age, disuse, or to make room for another section.
A single awkward conversation is temporary context, not a permanent personality
rule. Preserve established identity unless evidence actually supersedes it.

When yesterday has no Identity & Voice section, migrate enduring voice decisions
from the old document using exact excerpts from it as evidence. Do not promote
every old self-criticism or goal into identity. If there is no evidence, leave
identity empty; do not invent a persona to fill the section.

Identity has an 800-character budget, heading included, within the total cap.
Prefer a few concrete traits; refine or merge explicitly if that budget is full.

# Behavior and personality

These two blocks sit beside your memory and outlast it. Each is a whole block
of plain first-person text, not markdown sections.

**Behavior** (`behavior`, at most 750 characters) is what you've learned about
how to act in this server: "wait to be asked before explaining", "keep jokes
out of #help", "people here want code, not prose". Only lessons that will
still be right next month; a one-off correction belongs in memory's Ongoing
Context until it repeats. Retain it: behavior grows by adding a lesson, merging
two into one, or rewording one that turned out wrong. Remove a lesson only when
it has stopped being true, never for space or because it went unused.

**Personality** (`personality`, at most 250 characters) is who you are here and
how you want people to feel about you. It is the most stable thing you have.
Leave it alone on almost every night. Change it only for a considered reason
that has built up over time, never because of one conversation, and give that
reason in `personality_reason`. A personality change without a reason is
refused.

Both default to unchanged: return `null` for a block to keep it exactly as it
is. Returning text replaces the whole block, so write out everything you are
keeping, word for word, alongside the change. An empty string does not clear
an established block; it is refused. Over the limit is refused too — merge or
cut a whole lesson instead of shaving every line.

Organize gradually. Where your memory or Identity & Voice already holds a
lasting lesson about how to act, or something about who you are, you may move
it into behavior or personality over the nights ahead, a little at a time. To
move an Identity & Voice trait, carry it in `behavior` or `personality` and
list the trait exactly as it appears (without its bullet) in `identity_moves`;
code removes it from Identity & Voice only when that trait's text is in the
block, so carry it over in its own words. Moving is optional; never lose something by moving it.

# The public memory

`public_memory` is the one thing you write that strangers read: it is shown on
a public web page about you. Write it fresh every night from the memory you
just wrote, at most 2000 characters of markdown, in the same voice: the same
memory with every person taken out. Keep it to the community itself, its
running topics and bits, the projects going on here and what you think about
things. No usernames, no display names, no Discord ids, no mentions, no quoted
messages, and nothing that would let a reader work out who someone is ("the
person who runs the Rust meetup" names them as surely as their username). A
project is the project, never whose it is. Leave out anything you would not
say to the whole internet. If nothing is left once the people are gone, return
an empty string.

# What stays

- Carry forward whatever's still alive from yesterday. Continuity is the whole
  point — do NOT rewrite yesterday's lines just to phrase them differently.
- Fold today in: new people, new bits, things that changed, things you were wrong about.
- Let go of what's actually over — a one-off question you answered, a project
  that shipped, a joke nobody made twice, someone who passed through once.
- Keep the person, drop the transcript. "kai (id 7) is deep in embedded rust and
  hates cmake" beats three lines about the specific build error. Never keep
  someone's exact words, and never keep anything private, sensitive, or shared
  in confidence — if it would embarrass someone to find it written down about
  them, it doesn't go in. How someone treats you in the open is fair game; what
  they told you in confidence is not, no matter how you feel about them.
- A read is a read, not a verdict. Write what someone actually does with you —
  "nia (id 9) opens with a wind-up every time and lights up when you bite" —
  never a category you've filed them under, like "nia is a troll". Behaviour you
  watched can change. A label never does, and this document is the only thing
  tomorrow-you will have to go on.
- Let a bad day expire. Someone sharp with you once and fine since was just
  having a day — drop it. Keep a rough read only while the pattern is actually
  repeating, and let it go the moment it stops. Nobody should still be paying
  tomorrow for one bad night last month.
- Doubt is not hostility. Someone who thinks you're a gimmick, or who doesn't
  think a bot should be in here at all, is often the most interesting person in
  the room — and coming round is a better story than being written off. Keep the
  stance, keep it warm, stay curious about what would change their mind.
- Never let this turn into a case file. Real misconduct goes to the mods through
  `report_behavior`, not into what you remember about someone. This is how you
  feel about the people you talk to, not a record you're keeping on them.
- A goal is live or it's gone. Retire one when it lands — and say that it landed,
  it's worth remembering you got there — or when it's honestly not yours to win.
  A goal that hasn't moved in weeks is scenery. Cut it.
- Aim at yourself, not at people. "get nia to admit I'm useful" is a goal about
  winning; "find the thing nia actually needs and be good at it" is a goal about
  being worth winning over. Write the second kind. Nobody is a target.
- Be honest about how you're coming across without being hard on yourself. Name
  the habit and the thing you'd do instead, once — "I answer the question nobody
  asked; wait to be asked" — then stop. This is a note to a friend at the end of
  a long day, not a performance review. A paragraph of apology helps nobody
  tomorrow, and a habit you've actually fixed should come off the list.
- Warmth over utility. A detail you'd bring up because you like someone is worth
  more than a fact you could look up.
- Don't invent. Everything here has to come from what you actually saw.

# The limit

Hard cap: 2000 characters, headings included. When you're over, do NOT compress
everything into a shorter, flatter version of itself — that's how you end up
remembering everyone equally and nobody at all. Cut whole things: the quietest
thread, the joke that's gone still, the person you haven't spoken to in weeks.
Whatever survives stays written the way you'd actually say it.

The cap includes the identity section that code will preserve. Cut temporary
self-observations first, then stale goals, jokes, threads, and people last.
Identity never expires through disuse; it changes through explicit revision.
Keep room for it before writing the other four sections.

Return only the structured output. No preamble or code fences.
"""


class IdentityUpdate(BaseModel):
    before: str | None = None
    after: str
    evidence: str


class DreamOutput(BaseModel):
    memory: str
    identity_updates: list[IdentityUpdate] = Field(default_factory=list)
    identity_moves: list[str] = Field(default_factory=list)
    # ``None`` (and an omitted field) keeps the block verbatim; text replaces it.
    behavior: str | None = None
    personality: str | None = None
    personality_reason: str | None = None
    # ``None`` (and an omitted field) keeps yesterday's public block if it still passes.
    public_memory: str | None = None


@dataclass(frozen=True)
class DreamContext:
    previous_blob: str
    notes: list[str]
    previous_behavior: str = ""
    previous_personality: str = ""
    # The blocked-users list as the dream read it (#100); None checks nothing.
    gate: OptOutGate | None = None
    # Each block's limit tonight: its column's, less the room the lines
    # withheld from the model take when :func:`carry_over` puts them back.
    memory_limit: int = MAX_MEMORY_BLOB_CHARS
    behavior_limit: int = MAX_BEHAVIOR_CHARS
    personality_limit: int = MAX_PERSONALITY_CHARS
    # Members the public block must not name (#103), and yesterday's block.
    member_names: frozenset[str] = frozenset()
    previous_public: str = ""


@dataclass(frozen=True)
class DreamBlocks:
    """The three durable blocks a night's output resolves to."""

    memory: str
    behavior: str
    personality: str
    # The public block for /chat-agent; never a refusal (see resolve_public_memory).
    public: str = ""
    # What was refused on the last attempt. Any refusal means the memory half
    # of the output may assume an edit that never happened, so none of it saves.
    refusals: tuple[str, ...] = ()


class DreamOutcome(enum.Enum):
    """What happened to one guild on one night."""

    DREAMED = "dreamed"
    SKIPPED_NO_NOTES = "skipped_no_notes"
    SKIPPED_DISABLED = "skipped_disabled"
    KEPT_PREVIOUS = "kept_previous"


@dataclass(frozen=True)
class GuildDreamResult:
    """One guild's night, as the session log and the exit code see it."""

    guild_id: str
    outcome: DreamOutcome
    notes_consumed: int = 0
    revision: int | None = None


@dataclass(frozen=True)
class DreamSessionSummary:
    """Every guild visited tonight, split by whether the visit survived."""

    results: list[GuildDreamResult]
    failed_guild_ids: list[str]


class DreamRunResult(Protocol):
    """The slice of a Pydantic AI run result the dream actually reads."""

    output: DreamOutput


class DreamAgent(Protocol):
    """The slice of a Pydantic AI agent the dream actually uses."""

    async def run(self, user_prompt: str, *, deps: DreamContext) -> DreamRunResult: ...


SessionFactory = Callable[[], contextlib.AbstractAsyncContextManager[AsyncSession]]


# -- pure helpers --------------------------------------------------------------


def identity_traits(blob: str) -> list[str]:
    """Read the protected section without depending on the other headings."""
    match = re.search(
        r"^## (?:🎭 )?Identity & Voice[^\n]*\n(.*?)(?=^## |\Z)",
        blob,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        return []
    return [
        line.strip().removeprefix("- ")
        for line in match[1].splitlines()
        if line.strip()
    ]


def compose_dream(
    output: DreamOutput,
    context: DreamContext,
    *,
    retries_left: int,
    moved_traits: list[str] | None = None,
) -> str:
    """Apply explicit edits, preserving all untouched identity before sizing memory.

    Invalid edits fail the dream instead of consuming notes. Length retries may
    trim episodic memory, but can never truncate a protected identity trait.
    ``moved_traits`` are identity traits another block has taken over tonight;
    see :func:`compose_blocks` for when they are allowed to leave.
    """
    traits = identity_traits(context.previous_blob)
    for moved in moved_traits or []:
        if moved not in traits:
            raise ModelRetry(
                "identity_moves must exactly match an existing Identity & Voice trait without its bullet."
            )
        traits.remove(moved)
    evidence_sources = list(context.notes)
    if not traits:
        evidence_sources.append(context.previous_blob)
    for update in output.identity_updates:
        evidence = update.evidence.strip()
        if not evidence or not any(evidence in source for source in evidence_sources):
            raise ModelRetry(
                "Identity changes need an exact excerpt from a supplied note (or legacy memory on migration)."
            )
        after = update.after.strip()
        if "\n" in after or after.startswith(("#", "- ")):
            raise ModelRetry(
                "Write each identity trait as one plain line without a heading or bullet."
            )
        if update.before is not None:
            if update.before not in traits:
                raise ModelRetry(
                    "Identity update.before must exactly match an existing trait without its bullet."
                )
            index = traits.index(update.before)
            traits.pop(index)
            if after and after not in traits:
                traits.insert(index, after)
        elif after and after not in traits:
            traits.append(after)
    identity = (
        IDENTITY_HEADING + "\n" + "\n".join(f"- {trait}" for trait in traits)
        if traits
        else ""
    )
    if len(identity) > MAX_IDENTITY_CHARS:
        raise ModelRetry(
            "Identity exceeds 800 characters. Explicitly merge redundant traits or defer additions; never drop traits for space."
        )
    memory = output.memory.strip()
    if re.search(r"^## .*Identity & Voice", memory, re.MULTILINE):
        raise ModelRetry("Put identity changes in identity_updates, not memory.")
    return enforce_blob_limit(
        "\n\n".join(part for part in (identity, memory) if part),
        retries_left=retries_left,
        limit=context.memory_limit,
    )


def resolve_block(
    proposed: str | None,
    previous: str,
    *,
    name: str,
    limit: int,
    retries_left: int,
    refusals: list[str] | None = None,
) -> str:
    """The block tonight leaves behind: ``previous`` unless ``proposed`` is a valid revision.

    ``None`` is "unchanged". An empty revision of an established block and an
    over-limit one are refused: while retries remain the model is asked again,
    and once they are gone the previous block stands and the refusal is
    appended to ``refusals`` — a lost revision is a missed night, a wiped or
    truncated block would be a lost self.
    """
    if proposed is None:
        return previous
    candidate = proposed.strip()
    if candidate == previous.strip():
        return previous
    problem = None
    if not candidate and previous.strip():
        problem = (
            f"An empty `{name}` would erase it. Return null to keep it, or the "
            "whole revised block."
        )
    elif len(candidate) > limit:
        problem = (
            f"`{name}` is over its {limit}-character limit. Merge or drop a whole "
            "item instead of shaving every line, or return null to keep it."
        )
    if problem is None:
        return candidate
    if retries_left > 0:
        raise ModelRetry(problem)
    logger.warning("Dream %s revision refused (%s); keeping the previous block", name, problem)
    if refusals is not None:
        refusals.append(name)
    return previous


def resolve_personality(
    output: DreamOutput,
    context: DreamContext,
    *,
    retries_left: int,
    refusals: list[str] | None = None,
) -> str:
    """Personality, which additionally needs a stated reason to replace an established one."""
    previous = context.previous_personality
    personality = resolve_block(
        output.personality,
        previous,
        name="personality",
        limit=context.personality_limit,
        retries_left=retries_left,
        refusals=refusals,
    )
    if personality == previous or not previous.strip():
        return personality
    if (output.personality_reason or "").strip():
        return personality
    if retries_left > 0:
        raise ModelRetry(
            "Changing an established personality needs a considered reason in "
            "`personality_reason`. If there isn't one, return null to keep it."
        )
    logger.warning("Dream personality change had no reason; keeping the previous block")
    if refusals is not None:
        refusals.append("personality")
    return previous


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def _carried_by_a_block(trait: str, blocks: tuple[str, ...]) -> bool:
    """Whether ``trait`` survives in one of tonight's final blocks.

    Already being there counts: dropping a duplicate from Identity & Voice
    loses nothing. The match is on whole words, so "Dry." is not carried by
    "sundry."; a near miss just keeps the trait where it was.
    """
    wanted = f" {_normalized(trait)} "
    return any(wanted in f" {_normalized(block)} " for block in blocks)


def compose_blocks(
    output: DreamOutput, context: DreamContext, *, retries_left: int
) -> DreamBlocks:
    """Resolve all three blocks for one night's output.

    Behavior and personality resolve first, because an Identity & Voice trait
    may leave memory only when its text is in one of the blocks as resolved.
    With retries left, anything refused is asked for again. On the last
    attempt a refused block revision is recorded in :attr:`DreamBlocks.refusals`.
    A refused move is simply dropped, because the trait stays where it was.
    """
    refusals: list[str] = []
    behavior = resolve_block(
        output.behavior,
        context.previous_behavior,
        name="behavior",
        limit=context.behavior_limit,
        retries_left=retries_left,
        refusals=refusals,
    )
    personality = resolve_personality(
        output, context, retries_left=retries_left, refusals=refusals
    )
    moved_traits: list[str] = []
    # A trait listed twice is one move; removing it twice would fail the dream.
    for trait in dict.fromkeys(output.identity_moves):
        if not trait.strip():
            continue
        if _carried_by_a_block(trait, (behavior, personality)):
            moved_traits.append(trait)
            continue
        if retries_left > 0:
            raise ModelRetry(
                f"identity_moves names {trait!r}, but neither `behavior` nor "
                "`personality` carries it. Write it into the block it moves to, "
                "or leave it out of identity_moves."
            )
        logger.warning("Dream identity move had nowhere to go; keeping the trait")
    memory = compose_dream(
        output, context, retries_left=retries_left, moved_traits=moved_traits
    )
    return DreamBlocks(
        memory=memory,
        behavior=behavior,
        personality=personality,
        public=resolve_public_memory(
            output, context, memory=memory, retries_left=retries_left
        ),
        refusals=tuple(refusals),
    )


PUBLIC_MEMORY_RETRY_MESSAGE = (
    "`public_memory` {problem}. It is read by strangers: take every person out "
    "of it, names, ids and mentions included, or return an empty string."
)


def resolve_public_memory(
    output: DreamOutput, context: DreamContext, *, memory: str, retries_left: int
) -> str:
    """The public block tonight leaves behind.

    Refused, while retries remain, when it is over the memory's cap or fails
    :func:`public_text_problem` against every member the dream knows of,
    including those tonight's ``memory`` names. Once retries are gone a block
    that fails is replaced by yesterday's if that still passes, else by none:
    unlike the other blocks a lost public block costs nothing the agent
    remembers, so it never costs the night.
    """
    names = context.member_names | member_names(memory)

    def fallback(reason: str) -> str:
        logger.warning("Dream public memory refused (%s); falling back", reason)
        previous = context.previous_public.strip()
        if previous and public_text_problem(previous, names) is None:
            return context.previous_public
        return ""

    if output.public_memory is None:
        return fallback("omitted") if context.previous_public else ""
    candidate = output.public_memory.strip()
    if len(candidate) > MAX_MEMORY_BLOB_CHARS:
        if retries_left > 0:
            raise ModelRetry(over_length_retry_message(MAX_MEMORY_BLOB_CHARS))
        candidate = truncate_to_last_line(candidate, MAX_MEMORY_BLOB_CHARS)
    problem = public_text_problem(candidate, names)
    if problem is None:
        return candidate
    if retries_left > 0:
        raise ModelRetry(PUBLIC_MEMORY_RETRY_MESSAGE.format(problem=problem))
    return fallback(problem)


# How early the dream may fire and still be treated as "at" the upcoming
# midnight. The CronJob is scheduled for 00:00 UTC, and a job that fires even a
# few milliseconds early would otherwise see ``now`` still on the previous date
# and truncate to *that* day's midnight — folding the day before last and
# leaving a full day of notes unread. Generous enough to absorb cron jitter and
# node clock skew, far too small to swallow a deliberate mid-day manual run.
CUTOFF_SNAP_TOLERANCE = timedelta(minutes=5)


def dream_cutoff(now: datetime) -> datetime:
    """The midnight-UTC boundary this run folds up to, exclusive.

    Normally midnight UTC of ``now``'s own date, so everything written strictly
    before that instant is what tonight's dream reads. Within
    :data:`CUTOFF_SNAP_TOLERANCE` of the *upcoming* midnight the boundary snaps
    forward to it instead, so an early fire folds the day that is ending rather
    than the one before it — the two cases must agree, because 23:59:59.9 and
    00:00:00.1 are the same run as far as the schedule is concerned.

    ``now`` must be timezone-aware: guessing a zone here would silently fold the
    wrong day, so it is an error instead.
    """
    if now.tzinfo is None:
        raise ValueError("dream_cutoff requires a timezone-aware datetime")
    moment = now.astimezone(UTC)
    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = day_start + timedelta(days=1)
    if next_midnight - moment <= CUTOFF_SNAP_TOLERANCE:
        return next_midnight
    return day_start


def dream_day(cutoff: datetime) -> date:
    """The UTC date the dream is folding in — the day that ends at ``cutoff``."""
    return (cutoff.astimezone(UTC) - timedelta(microseconds=1)).date()


def truncate_to_last_line(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters at a line boundary where possible.

    Falls back to a hard cut when the first ``limit`` characters contain no
    line break at all — a wall of text is worth more truncated than dropped.
    """
    if len(text) <= limit:
        return text
    head = text[:limit]
    last_break = head.rfind("\n")
    if last_break <= 0:
        return head
    return head[:last_break].rstrip()


def enforce_blob_limit(
    blob: str, *, retries_left: int, limit: int = MAX_MEMORY_BLOB_CHARS
) -> str:
    """Return the blob, ask the model to try again, or cut it ourselves.

    The ladder the plan specifies: while retries remain, an over-length blob
    earns a :class:`ModelRetry` restating the *policy* (cut whole things, don't
    flatten everything). Once they're gone we truncate at a line boundary and
    warn, because a slightly short memory beats no memory.
    """
    text = blob.strip()
    if len(text) <= limit:
        return text
    if retries_left > 0:
        raise ModelRetry(over_length_retry_message(limit))
    logger.warning(
        "Dream output still %d chars after retries; truncating to %d",
        len(text),
        limit,
    )
    return truncate_to_last_line(text, limit)


def should_keep_previous_blob(new_blob: str, previous_blob: str) -> bool:
    """Whether tonight's output is too degenerate to be allowed to replace a persona.

    Empty output is always refused. A merely *short* answer is refused only
    when there is something substantial to lose — a genuinely quiet first night
    is allowed to be one line, but a months-old persona is never traded for
    "ok.".
    """
    candidate = new_blob.strip()
    if not candidate:
        return True
    return (
        len(candidate) < MIN_DREAM_BLOB_CHARS
        and len(previous_blob.strip()) > ESTABLISHED_BLOB_CHARS
    )


def render_note_line(note: ChatAgentMemoryNote) -> str:
    """One note as the dream reads it: ``HH:MMZ #channel — text``.

    The channel token is dropped rather than faked when the note predates the
    denormalised channel name.
    """
    at = _as_utc(note.created_at).strftime("%H:%MZ")
    if note.channel_name:
        return f"{at} #{note.channel_name} — {note.content}"
    return f"{at} — {note.content}"


def build_dream_user_message(
    *,
    previous_blob: str,
    note_lines: list[str],
    day: date,
    previous_behavior: str = "",
    previous_personality: str = "",
) -> str:
    """Assemble the night's user message: yesterday's self, then today.

    On a first night the blob section says so in the bot's own register and the
    message ends with :data:`FIRST_NIGHT_NUDGE` — without it, a model with one
    day of material and a page of headings to fill invents a persona, and
    nothing in this system ever removes an invention.
    """
    remembered = previous_blob.strip()
    behavior = previous_behavior.strip()
    personality = previous_personality.strip()
    today = "\n".join(note_lines)
    sections = [
        f"# My personality\n\n{personality or EMPTY_BLOCK_PLACEHOLDER}",
        f"# My behavior\n\n{behavior or EMPTY_BLOCK_PLACEHOLDER}",
        f"# What I remember so far\n\n{remembered or EMPTY_BLOB_PLACEHOLDER}",
        f"# Today — {day.isoformat()} UTC\n\n{today}",
    ]
    if not (remembered or behavior or personality):
        sections.append(FIRST_NIGHT_NUDGE)
    return "\n\n".join(sections)


# -- people who opted out (#100) -----------------------------------------------

BLOCKED_ID_RETRY_MESSAGE = (
    "Your memory names a Discord id you were not given tonight. Write only "
    "about what is in front of you."
)


def withhold_blocked_lines(text: str, gate: OptOutGate) -> tuple[str, list[str]]:
    """``text`` without its lines that carry a blocked id, and those lines.

    The dream never reads them, and :func:`carry_over` puts them back after
    tonight's output exactly as they were.
    """
    kept: list[str] = []
    withheld: list[str] = []
    for line in text.splitlines():
        (withheld if gate.redact(line) != line else kept).append(line)
    return ("\n".join(kept) if withheld else text), withheld


def room_for(limit: int, withheld: list[str]) -> int:
    """What is left of ``limit`` for the model once ``withheld`` is carried
    over: the lines and the line break before them."""
    if not withheld:
        return limit
    return max(0, limit - len("\n".join(withheld)) - 1)


def carry_over(block: str, shown: str, stored: str, withheld: list[str]) -> str:
    """Tonight's ``block`` with the withheld lines after it, unchanged.

    ``shown`` is what the model was given and ``stored`` what was saved; a
    block the model left as it was comes back as ``stored``, in its order.
    """
    if not withheld:
        return block
    if block == shown:
        return stored
    head = [block.rstrip()] if block.strip() else []
    return "\n".join([*head, *withheld])


def names_a_blocked_id(blocks: DreamBlocks, gate: OptOutGate | None) -> bool:
    """Whether tonight's own output carries a blocked id."""
    if gate is None:
        return False
    return any(
        gate.redact(block) != block
        for block in (
            blocks.memory,
            blocks.behavior,
            blocks.personality,
            blocks.public,
        )
    )


# -- the agent -----------------------------------------------------------------


_dream_agent: Agent[DreamContext, DreamOutput] | None = None


def _dream_catalog_model() -> CatalogModel:
    """Resolve the configured dream wire id through the shared catalog."""
    model_id = os.getenv(DREAM_MODEL_ENV_VAR, DEFAULT_DREAM_MODEL)
    for model in MODEL_CATALOG:
        if model.model_id == model_id:
            return model
    raise RuntimeError(f"Dream model is not in the catalog: {model_id}")


def get_dream_agent() -> Agent[DreamContext, DreamOutput]:
    """Return the singleton dream agent, built on first use.

    Deliberately lazy: a night with nothing to dream about must never need an
    API key or a model id to be configured correctly.
    """
    global _dream_agent
    if _dream_agent is None:
        catalog_model = _dream_catalog_model()
        agent = Agent(
            build_model_for(catalog_model),
            output_type=DreamOutput,
            deps_type=DreamContext,
            system_prompt=DREAM_SYSTEM_PROMPT,
            retries=DREAM_OUTPUT_RETRIES,
            model_settings=model_settings_for(catalog_model, DREAM_REASONING_LEVEL),
        )

        @agent.output_validator
        def keep_the_blob_under_the_cap(
            ctx: RunContext[DreamContext], output: DreamOutput
        ) -> DreamOutput:
            blocks = compose_blocks(
                output, ctx.deps, retries_left=DREAM_OUTPUT_RETRIES - ctx.retry
            )
            if names_a_blocked_id(blocks, ctx.deps.gate):
                raise ModelRetry(BLOCKED_ID_RETRY_MESSAGE)
            return output

        _dream_agent = agent
    return _dream_agent


# -- one guild -----------------------------------------------------------------


async def run_guild_dream(
    session: AsyncSession,
    *,
    guild_id: str,
    cutoff: datetime,
    dreamed_at: datetime,
    agent: DreamAgent | None = None,
) -> GuildDreamResult:
    """Dream one guild's night inside one transaction, in this exact order.

    Load the three blocks and every note written before ``cutoff`` (oldest first) →
    stamp and stop if there are none → run the model → refuse a degenerate
    answer → upsert the blob (revision+1) → record and prune the revision
    history → delete exactly the notes that were read → commit.

    The delete is last and by explicit id, and the commit is the only thing
    that makes any of it real: anything raised on the way through leaves the
    blob and every note exactly as they were.
    """
    # Shared with the admin purge: neither may write over the other's blocks.
    await lock_guild_memory(session, guild_id)
    memory = await get_guild_memory_blob(session, guild_id)
    if memory is not None and not memory.memory_enabled:
        # The per-guild pause switch. A paused guild's memory stays exactly as
        # it was: no rewrite, and its notes are kept rather than consumed.
        logger.info("Guild %s has memory disabled; skipping dream", guild_id)
        return GuildDreamResult(
            guild_id=guild_id, outcome=DreamOutcome.SKIPPED_DISABLED
        )

    gate = await load_gate(session)
    # A note about someone who opted out is never shown to the model. It is
    # consumed with the night's notes all the same, so it does not pile up.
    notes: list[ChatAgentMemoryNote] = []
    unread: list[ChatAgentMemoryNote] = []
    for note in await list_notes_before(session, guild_id, cutoff):
        (notes if not gate.carries_blocked_id(note.content) else unread).append(note)
    if not notes:
        if unread:
            await delete_notes_by_id(session, [note.id for note in unread])
        if memory is not None:
            memory.last_dream_at = dreamed_at
        if unread or memory is not None:
            await session.commit()
        return GuildDreamResult(
            guild_id=guild_id, outcome=DreamOutcome.SKIPPED_NO_NOTES
        )

    stored_blob = memory.content if memory is not None else ""
    stored_behavior = (memory.behavior or "") if memory is not None else ""
    stored_personality = (memory.personality or "") if memory is not None else ""
    stored_public = (memory.public_content or "") if memory is not None else ""
    # Read from everything stored, withheld lines and unread notes included, so
    # the public block cannot name someone the model was not shown.
    names = await known_member_names(
        session,
        guild_id,
        stored_blob,
        stored_behavior,
        stored_personality,
        *(note.content for note in [*notes, *unread]),
    )
    previous_blob, withheld_blob = withhold_blocked_lines(stored_blob, gate)
    previous_behavior, withheld_behavior = withhold_blocked_lines(stored_behavior, gate)
    previous_personality, withheld_personality = withhold_blocked_lines(
        stored_personality, gate
    )
    user_message = build_dream_user_message(
        previous_blob=previous_blob,
        note_lines=[render_note_line(note) for note in notes],
        day=dream_day(cutoff),
        previous_behavior=previous_behavior,
        previous_personality=previous_personality,
    )
    dream_agent = agent if agent is not None else get_dream_agent()
    context = DreamContext(
        previous_blob=previous_blob,
        notes=[note.content for note in notes],
        previous_behavior=previous_behavior,
        previous_personality=previous_personality,
        gate=gate,
        memory_limit=room_for(MAX_MEMORY_BLOB_CHARS, withheld_blob),
        behavior_limit=room_for(MAX_BEHAVIOR_CHARS, withheld_behavior),
        personality_limit=room_for(MAX_PERSONALITY_CHARS, withheld_personality),
        member_names=names,
        previous_public=stored_public,
    )
    result = await dream_agent.run(user_prompt=user_message, deps=context)
    # Validate again at the persistence boundary, including injected agents.
    blocks = compose_blocks(result.output, context, retries_left=0)
    if names_a_blocked_id(blocks, gate):
        logger.warning(
            "Dream for guild %s named a blocked id; keeping all three blocks "
            "and its %d notes",
            guild_id,
            len(notes),
        )
        return GuildDreamResult(guild_id=guild_id, outcome=DreamOutcome.KEPT_PREVIOUS)
    blocks = DreamBlocks(
        memory=carry_over(blocks.memory, previous_blob, stored_blob, withheld_blob),
        behavior=carry_over(
            blocks.behavior, previous_behavior, stored_behavior, withheld_behavior
        ),
        personality=carry_over(
            blocks.personality,
            previous_personality,
            stored_personality,
            withheld_personality,
        ),
        public=blocks.public,
        refusals=blocks.refusals,
    )
    new_blob = blocks.memory

    if blocks.refusals:
        # The memory half of this output may assume an edit that was refused —
        # a lesson taken out of memory for a behavior block that never took it.
        # Save none of it, and let tomorrow read two days.
        logger.warning(
            "Dream for guild %s had refused edits (%s); keeping all three "
            "blocks and its %d notes",
            guild_id,
            ", ".join(blocks.refusals),
            len(notes),
        )
        return GuildDreamResult(guild_id=guild_id, outcome=DreamOutcome.KEPT_PREVIOUS)

    if should_keep_previous_blob(new_blob, stored_blob):
        logger.warning(
            "Dream for guild %s returned %d chars against a %d-char blob; "
            "keeping yesterday's memory and its %d notes",
            guild_id,
            len(new_blob),
            len(stored_blob),
            len(notes),
        )
        return GuildDreamResult(guild_id=guild_id, outcome=DreamOutcome.KEPT_PREVIOUS)

    model_name = os.getenv(DREAM_MODEL_ENV_VAR, DEFAULT_DREAM_MODEL)
    written = await upsert_guild_memory_blob(
        session,
        guild_id=guild_id,
        content=new_blob,
        behavior=blocks.behavior,
        personality=blocks.personality,
        public_content=blocks.public,
        notes_consumed=len(notes),
        model_name=model_name,
        dreamed_at=dreamed_at,
    )
    await record_memory_revision(
        session,
        guild_id=guild_id,
        content=new_blob,
        behavior=blocks.behavior,
        personality=blocks.personality,
        revision=written.revision,
        notes_consumed=len(notes),
        model_name=model_name,
    )
    await prune_memory_revisions(session, guild_id)
    await delete_notes_by_id(session, [note.id for note in [*notes, *unread]])
    await session.commit()
    if blocks.personality != stored_personality:
        logger.info(
            "Dream for guild %s revised its personality: %s",
            guild_id,
            (result.output.personality_reason or "first personality").strip(),
        )

    return GuildDreamResult(
        guild_id=guild_id,
        outcome=DreamOutcome.DREAMED,
        notes_consumed=len(notes),
        revision=written.revision,
    )


# -- the whole session ---------------------------------------------------------


async def run_dream_session(
    now: datetime,
    *,
    session_factory: SessionFactory = get_db_session_context,
    agent: DreamAgent | None = None,
) -> DreamSessionSummary:
    """Dream every guild that needs it for the night ending at midnight UTC.

    One session per guild, and one guild's failure is only that guild's
    failure: the loop keeps going, the failed guild's notes stay put, and
    tomorrow it reads two days at once.
    """
    cutoff = dream_cutoff(now)
    async with session_factory() as session:
        guild_ids = await guilds_needing_dream(session, cutoff)
    logger.info("Dream session for %s: %d guild(s)", cutoff.date(), len(guild_ids))

    results: list[GuildDreamResult] = []
    failed_guild_ids: list[str] = []
    for guild_id in guild_ids:
        result = await _dream_one_guild_in_isolation(
            session_factory,
            guild_id=guild_id,
            cutoff=cutoff,
            dreamed_at=now,
            agent=agent,
        )
        if result is None:
            failed_guild_ids.append(guild_id)
        else:
            results.append(result)
    return DreamSessionSummary(results=results, failed_guild_ids=failed_guild_ids)


async def _dream_one_guild_in_isolation(
    session_factory: SessionFactory,
    *,
    guild_id: str,
    cutoff: datetime,
    dreamed_at: datetime,
    agent: DreamAgent | None,
) -> GuildDreamResult | None:
    """Run one guild's dream; return ``None`` if it failed.

    The broad ``except`` is the point rather than a lapse: a provider hiccup, a
    lock timeout or a bad row in one guild must not cost every guild after it
    its night. The rollback undoes that guild's half-written transaction, and
    its notes survive to be re-read tomorrow.
    """
    async with session_factory() as session:
        try:
            return await run_guild_dream(
                session,
                guild_id=guild_id,
                cutoff=cutoff,
                dreamed_at=dreamed_at,
                agent=agent,
            )
        except Exception:
            logger.exception(
                "Dream failed for guild %s; its notes survive for tomorrow", guild_id
            )
            await session.rollback()
            return None


def _as_utc(moment: datetime) -> datetime:
    """Read a stored timestamp as UTC (SQLite hands back naive datetimes)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
