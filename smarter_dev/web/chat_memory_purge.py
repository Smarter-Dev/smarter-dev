"""The chat agent forgets one person, per guild, on an admin's request.

A purge is the dream's counterpart for deletion requests. The same model, in
the same voice, rereads one guild's three durable blocks and its pending notes
with one instruction: remove everything said by, about, or learned from this
Discord user, and leave everything else as it was. Then it does the same for
every retained revision, because those are copies of the same memory.

The rules from the privacy plan shape everything here:

1. **Only the agent edits its memory.** Code never cuts lines out of a block,
   never string-replaces a name and never resets a guild. What code does is
   refuse: an output that still carries the user's ID, still names them
   without saying why, breaks a limit, or empties a block that had nothing to
   do with them is sent back, and after the retries it fails.
2. **A failure changes nothing.** One guild is one transaction. Any refusal or
   exception leaves the blocks, notes and revisions exactly as they were, and
   the step stays pending for the admin. There is no fallback that blanks a
   block to make a purge "succeed".
3. **The target travels privately.** The ID and names are in the purge prompt
   and nowhere else; nothing here logs them.

The purge takes the same per-guild lock as the dream, so a purge and a nightly
dream can never interleave and write over each other.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from typing import Literal
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent
from pydantic_ai import ModelRetry
from pydantic_ai import RunContext
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.bot.agents.model_router import build_model_for
from smarter_dev.bot.agents.model_router import model_settings_for
from smarter_dev.web.chat_memory_dream import DEFAULT_DREAM_MODEL
from smarter_dev.web.chat_memory_dream import DREAM_MODEL_ENV_VAR
from smarter_dev.web.chat_memory_dream import DREAM_REASONING_LEVEL
from smarter_dev.web.chat_memory_dream import MAX_IDENTITY_CHARS
from smarter_dev.web.chat_memory_dream import _dream_catalog_model
from smarter_dev.web.chat_memory_dream import identity_traits
from smarter_dev.web.crud import delete_notes_by_id
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import list_guild_notes
from smarter_dev.web.crud import list_memory_revisions
from smarter_dev.web.crud import lock_guild_memory
from smarter_dev.web.crud import prune_memory_revisions
from smarter_dev.web.crud import record_memory_revision
from smarter_dev.web.models import MAX_BEHAVIOR_CHARS
from smarter_dev.web.models import MAX_MEMORY_BLOB_CHARS
from smarter_dev.web.models import MAX_MEMORY_NOTE_CHARS
from smarter_dev.web.models import MAX_PERSONALITY_CHARS
from smarter_dev.web.models import MAX_NOTES_PER_GUILD_PER_DAY

logger = logging.getLogger(__name__)

PURGE_OUTPUT_RETRIES = 2
# Names shorter than this match too much ordinary text to be checked
# deterministically; the agent still sees them in the prompt.
MIN_CHECKED_NAME_CHARS = 2
# Every note the guild holds is reviewed, and a day is capped at this many.
PURGE_NOTES_LIMIT = MAX_NOTES_PER_GUILD_PER_DAY * 3


# -- the target ----------------------------------------------------------------


@dataclass(frozen=True)
class PurgeTarget:
    """The person being removed: their Discord ID and the names they went by.

    Matching is deliberately dumb and deterministic — it is the check that does
    not take the agent's word for it. The ID matches as a whole number, a name
    as a whole word in any case. A name can match a different person, which is
    why name hits are something the agent must explain, not an automatic fail.
    """

    user_id: str
    names: tuple[str, ...] = ()

    @classmethod
    def build(cls, user_id: str, names: list[str] | tuple[str, ...]) -> PurgeTarget:
        seen: dict[str, str] = {}
        for name in names:
            cleaned = " ".join(name.split())
            if cleaned and cleaned.casefold() not in seen:
                seen[cleaned.casefold()] = cleaned
        return cls(user_id=user_id.strip(), names=tuple(seen.values()))

    @property
    def checked_names(self) -> tuple[str, ...]:
        return tuple(n for n in self.names if len(n) >= MIN_CHECKED_NAME_CHARS)

    def id_hits(self, text: str) -> int:
        if not text or not self.user_id:
            return 0
        return len(re.findall(rf"(?<!\d){re.escape(self.user_id)}(?!\d)", text))

    def name_hits(self, text: str) -> int:
        if not text:
            return 0
        return sum(
            len(re.findall(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE))
            for name in self.checked_names
        )

    def mentions(self, text: str) -> bool:
        return bool(self.id_hits(text) or self.name_hits(text))


# -- model output --------------------------------------------------------------


class NoteEdit(BaseModel):
    id: str
    action: Literal["keep", "rewrite", "drop"]
    text: str | None = None


class UnresolvedItem(BaseModel):
    """Something the agent could not settle: where it is, and why — never a quote."""

    location: str
    reason: str


class PurgeOutput(BaseModel):
    memory: str
    behavior: str
    personality: str
    notes: list[NoteEdit] = Field(default_factory=list)
    unresolved: list[UnresolvedItem] = Field(default_factory=list)


@dataclass(frozen=True)
class PurgeContext:
    target: PurgeTarget
    memory: str
    behavior: str
    personality: str
    notes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PurgedBlocks:
    memory: str
    behavior: str
    personality: str
    rewritten_notes: dict[str, str] = field(default_factory=dict)
    dropped_notes: tuple[str, ...] = ()
    unresolved: tuple[UnresolvedItem, ...] = ()


class PurgeRefused(Exception):
    """The agent's output could not be accepted; nothing was written."""


PURGE_SYSTEM_PROMPT = """\
You are the Smarter Dev Discord bot, going back over what you remember about one
server because someone has asked to be forgotten.

You are given one person's Discord user ID and the names they went by. Remove
everything in your memory that came from them or is about them: lines about
who they are, running bits and threads that are theirs, opinions you formed
because of them, things they told you, lessons about how to act that you
learned from them in a way that would identify them, and every mention of their
name or ID. Afterwards nothing you keep should let anyone tell they were here.

Everything else stays exactly as you wrote it. This is not a rewrite and not a
chance to tidy up: other people's lines, your identity, your lore, your
opinions and your lessons stay word for word unless they are about this person.
Where one line mixes this person with someone else, rewrite just enough of it
that it is only about the other person. Do not write that someone was removed,
forgotten or asked anything; leave no trace of the request either.

# What you're given

- `# The person` — their user ID and names. Private; never write them down.
- `# My personality`, `# My behavior`, `# What I remember` — your three blocks.
  `# What I remember` includes your `## Identity & Voice` section; return it as
  part of `memory`, with its heading, edited only where it involves this person.
- `# Pending notes` — notes you kept today, each with an id.

# What to return

- `memory`, `behavior`, `personality`: each whole block as it should now read.
  Unchanged blocks come back exactly as given. A block may become empty only if
  everything in it was about this person.
- `notes`: one entry per pending note id: `keep`, `drop`, or `rewrite` with the
  new `text` (a note mixing this person with someone else keeps only the other).
- `unresolved`: anything you cannot settle, as `location` (`memory`, `behavior`,
  `personality`, or `note:<id>`) and a short `reason`. Use it when a name might
  belong to a different person who shares it, or when something might be about
  this person but you cannot tell. Never quote the text and never write the
  person's name or ID in a reason.

Limits still hold: memory at most 2000 characters (Identity & Voice at most 800
of them), behavior at most 750, personality at most 250, a note at most 500.

Return only the structured output.
"""


def build_purge_user_message(context: PurgeContext) -> str:
    names = ", ".join(context.target.names) or "(no names known)"
    notes = (
        "\n".join(f"[{note_id}] {content}" for note_id, content in context.notes)
        or "(none)"
    )
    return "\n\n".join(
        [
            f"# The person\n\nDiscord user ID {context.target.user_id}; names: {names}",
            f"# My personality\n\n{context.personality.strip() or '(empty)'}",
            f"# My behavior\n\n{context.behavior.strip() or '(empty)'}",
            f"# What I remember\n\n{context.memory.strip() or '(empty)'}",
            f"# Pending notes\n\n{notes}",
        ]
    )


# -- validation ----------------------------------------------------------------


def _identity_chars(memory: str) -> int:
    traits = identity_traits(memory)
    if not traits:
        return 0
    return len("## Identity & Voice\n" + "\n".join(f"- {t}" for t in traits))


def _segments(text: str) -> list[str]:
    """Lines, and sentences within a line: the units a purge keeps or drops."""
    return [
        piece.strip()
        for piece in re.split(r"\n|(?<=[.!?])\s+", text)
        if piece.strip() and not piece.strip().startswith("#")
    ]


def _unrelated_segments_kept(previous: str, new: str, target: PurgeTarget) -> tuple[int, int]:
    """How many of ``previous``'s segments not naming the target survive verbatim.

    A purge keeps everything that is not about the person word for word, so
    most of these must come through. Some may legitimately go — something
    about the person that never named them — which is why this is a share
    and not all of them.
    """
    unrelated = [seg for seg in _segments(previous) if not target.mentions(seg)]
    kept = sum(1 for seg in unrelated if seg in new)
    return kept, len(unrelated)


def compose_purge(
    output: PurgeOutput, context: PurgeContext, *, retries_left: int
) -> PurgedBlocks:
    """Accept the agent's purge, ask again, or refuse it.

    Every problem is a :class:`ModelRetry` while retries remain and a
    :class:`PurgeRefused` once they are gone. Nothing here edits the text.
    """

    def refuse(problem: str) -> PurgeRefused:
        if retries_left > 0:
            raise ModelRetry(problem)
        return PurgeRefused(problem)

    target = context.target
    blocks = {
        "memory": (output.memory.strip(), context.memory.strip(), MAX_MEMORY_BLOB_CHARS),
        "behavior": (output.behavior.strip(), context.behavior.strip(), MAX_BEHAVIOR_CHARS),
        "personality": (
            output.personality.strip(),
            context.personality.strip(),
            MAX_PERSONALITY_CHARS,
        ),
    }
    unresolved_locations = {item.location.strip() for item in output.unresolved}

    for name, (new, previous, limit) in blocks.items():
        if len(new) > limit:
            raise refuse(
                f"`{name}` is over its {limit}-character limit. Remove only what "
                "involves this person; everything else stays as it was."
            )
        if not new and previous and not target.mentions(previous):
            raise refuse(
                f"`{name}` came back empty, but nothing in it was about this "
                "person. Return it exactly as given."
            )
    if _identity_chars(blocks["memory"][0]) > MAX_IDENTITY_CHARS:
        raise refuse("Identity & Voice is over 800 characters; return it as given.")
    for name, (new, previous, _limit) in blocks.items():
        kept, unrelated = _unrelated_segments_kept(previous, new, target)
        if kept * 2 < unrelated:
            raise refuse(
                f"`{name}` lost things that were not about this person. Return "
                "everything that is not about them word for word."
            )
    memory = blocks["memory"][0]

    note_ids = [note_id for note_id, _ in context.notes]
    edits: dict[str, NoteEdit] = {}
    for edit in output.notes:
        if edit.id not in note_ids:
            raise refuse(f"There is no pending note with id {edit.id!r}.")
        if edit.id in edits:
            raise refuse(f"Note {edit.id!r} has two entries; give it one.")
        edits[edit.id] = edit
    missing = [note_id for note_id in note_ids if note_id not in edits]
    if missing:
        raise refuse(
            "Every pending note needs one entry (keep, drop or rewrite). "
            f"Missing: {', '.join(missing)}."
        )
    rewritten: dict[str, str] = {}
    dropped: list[str] = []
    originals = dict(context.notes)
    for note_id, edit in edits.items():
        if edit.action == "drop":
            dropped.append(note_id)
        elif edit.action == "rewrite":
            text = (edit.text or "").strip()
            if not text or len(text) > MAX_MEMORY_NOTE_CHARS:
                raise refuse(
                    f"Rewritten note {note_id!r} must be 1–{MAX_MEMORY_NOTE_CHARS} "
                    "characters; drop it if nothing is left."
                )
            if text != originals[note_id].strip():
                rewritten[note_id] = text

    kept_text = {name: values[0] for name, values in blocks.items()}
    for note_id in note_ids:
        if note_id not in dropped:
            kept_text[f"note:{note_id}"] = rewritten.get(note_id, originals[note_id])
    reasons = " ".join(item.reason for item in output.unresolved)
    if target.id_hits(reasons) or target.name_hits(reasons):
        raise refuse("Unresolved reasons must not name the person or carry their ID.")
    for location, text in kept_text.items():
        if target.id_hits(text):
            raise refuse(
                f"`{location}` still carries this person's ID. Remove what is "
                "about them."
            )
        if target.name_hits(text) and location not in unresolved_locations:
            raise refuse(
                f"`{location}` still uses one of this person's names. Remove it, "
                "or if it is a different person who shares the name, list "
                f"`{location}` in unresolved and say why."
            )

    return PurgedBlocks(
        memory=memory,
        behavior=blocks["behavior"][0],
        personality=blocks["personality"][0],
        rewritten_notes=rewritten,
        dropped_notes=tuple(dropped),
        unresolved=tuple(output.unresolved),
    )


# -- the agent -----------------------------------------------------------------


class PurgeRunResult(Protocol):
    output: PurgeOutput


class PurgeAgent(Protocol):
    async def run(self, user_prompt: str, *, deps: PurgeContext) -> PurgeRunResult: ...


_purge_agent: Agent[PurgeContext, PurgeOutput] | None = None


def build_purge_agent(model, *, model_settings=None) -> Agent[PurgeContext, PurgeOutput]:
    """A purge agent on ``model``, with the validator that refuses unclean output."""
    agent = Agent(
        model,
        output_type=PurgeOutput,
        deps_type=PurgeContext,
        system_prompt=PURGE_SYSTEM_PROMPT,
        retries=PURGE_OUTPUT_RETRIES,
        model_settings=model_settings,
    )

    @agent.output_validator
    def accept_only_a_clean_purge(
        ctx: RunContext[PurgeContext], output: PurgeOutput
    ) -> PurgeOutput:
        compose_purge(output, ctx.deps, retries_left=PURGE_OUTPUT_RETRIES - ctx.retry)
        return output

    return agent


def get_purge_agent() -> Agent[PurgeContext, PurgeOutput]:
    """The purge agent: the dream's model and reasoning, the purge's prompt."""
    global _purge_agent
    if _purge_agent is None:
        catalog_model = _dream_catalog_model()
        _purge_agent = build_purge_agent(
            build_model_for(catalog_model),
            model_settings=model_settings_for(catalog_model, DREAM_REASONING_LEVEL),
        )
    return _purge_agent


async def _purge_once(agent: PurgeAgent, context: PurgeContext) -> PurgedBlocks:
    result = await agent.run(build_purge_user_message(context), deps=context)
    # Validate again at the persistence boundary, including injected agents.
    return compose_purge(result.output, context, retries_left=0)


# -- one guild -----------------------------------------------------------------


GuildPurgeOutcome = Literal["purged", "unchanged", "empty"]


@dataclass(frozen=True)
class GuildPurgeResult:
    guild_id: str
    outcome: GuildPurgeOutcome
    notes_rewritten: int = 0
    notes_dropped: int = 0
    revisions_rewritten: int = 0
    unresolved: tuple[UnresolvedItem, ...] = ()

    def as_step(self) -> dict:
        return {
            "outcome": self.outcome,
            "notes_rewritten": self.notes_rewritten,
            "notes_dropped": self.notes_dropped,
            "revisions_rewritten": self.revisions_rewritten,
            "unresolved": [item.model_dump() for item in self.unresolved],
        }


async def purge_guild_memory(
    session: AsyncSession,
    *,
    guild_id: str,
    target: PurgeTarget,
    now: datetime,
    agent: PurgeAgent | None = None,
) -> GuildPurgeResult:
    """Have the agent remove ``target`` from one guild's memory, in one transaction.

    Lock the guild against the dream → load blocks, every note and every
    retained revision → one purge pass over the current blocks and notes → one
    pass over each distinct retained revision → write all of it → the caller
    commits. Anything raised on the way leaves every row as it was.
    """
    await lock_guild_memory(session, guild_id)
    memory = await get_guild_memory_blob(session, guild_id)
    notes = list(reversed(await list_guild_notes(session, guild_id, limit=PURGE_NOTES_LIMIT)))
    revisions = await list_memory_revisions(session, guild_id)
    if memory is None and not notes and not revisions:
        return GuildPurgeResult(guild_id=guild_id, outcome="empty")

    purge_agent = agent if agent is not None else get_purge_agent()
    current = PurgeContext(
        target=target,
        memory=memory.content if memory is not None else "",
        behavior=(memory.behavior or "") if memory is not None else "",
        personality=(memory.personality or "") if memory is not None else "",
        notes=tuple((str(note.id), note.content) for note in notes),
    )
    purged = await _purge_once(purge_agent, current)

    # Revisions are copies of the same memory: each distinct one gets its own
    # pass. One identical to the live blocks reuses the live result.
    by_blocks: dict[tuple[str, str, str], PurgedBlocks] = {
        (current.memory.strip(), current.behavior.strip(), current.personality.strip()): purged
    }
    revision_results: list[tuple[object, PurgedBlocks]] = []
    for revision in revisions:
        key = (
            revision.content.strip(),
            (revision.behavior or "").strip(),
            (revision.personality or "").strip(),
        )
        if key not in by_blocks:
            by_blocks[key] = await _purge_once(
                purge_agent,
                PurgeContext(
                    target=target,
                    memory=revision.content,
                    behavior=revision.behavior or "",
                    personality=revision.personality or "",
                ),
            )
        revision_results.append((revision, by_blocks[key]))

    blocks_changed = (purged.memory, purged.behavior, purged.personality) != (
        current.memory.strip(),
        current.behavior.strip(),
        current.personality.strip(),
    )
    revisions_rewritten = 0
    for revision, result in revision_results:
        if (
            result.memory != revision.content.strip()
            or result.behavior != (revision.behavior or "").strip()
            or result.personality != (revision.personality or "").strip()
        ):
            revision.content = result.memory
            revision.behavior = result.behavior
            revision.personality = result.personality
            revisions_rewritten += 1

    if blocks_changed and memory is not None:
        model_name = os.getenv(DREAM_MODEL_ENV_VAR, DEFAULT_DREAM_MODEL)
        memory.content = purged.memory
        memory.behavior = purged.behavior
        memory.personality = purged.personality
        memory.revision = memory.revision + 1
        memory.model_name = model_name
        memory.updated_at = now
        await session.flush()
        await record_memory_revision(
            session,
            guild_id=guild_id,
            content=purged.memory,
            behavior=purged.behavior,
            personality=purged.personality,
            revision=memory.revision,
            notes_consumed=0,
            model_name=model_name,
        )
        await prune_memory_revisions(session, guild_id)

    notes_by_id = {str(note.id): note for note in notes}
    for note_id, text in purged.rewritten_notes.items():
        notes_by_id[note_id].content = text
        notes_by_id[note_id].updated_at = now
    await delete_notes_by_id(session, [UUID(note_id) for note_id in purged.dropped_notes])
    await session.flush()

    changed = (
        (blocks_changed and memory is not None)
        or purged.rewritten_notes
        or purged.dropped_notes
        or revisions_rewritten
    )
    return GuildPurgeResult(
        guild_id=guild_id,
        outcome="purged" if changed else "unchanged",
        notes_rewritten=len(purged.rewritten_notes),
        notes_dropped=len(purged.dropped_notes),
        revisions_rewritten=revisions_rewritten,
        unresolved=purged.unresolved,
    )

