"""The chat agent forgets one person, per guild, on an admin's request.

A purge is the dream's counterpart for deletion requests. The same model, in
the same voice, rereads the parts of one guild's memory that mention one
Discord user with one instruction: remove everything said by, about, or
learned from them, and leave everything else as it was. Then it does the same
for every retained revision that mentions them, because those are copies of
the same memory, and for every note the guild holds, in batches.

The rules from the privacy plan shape everything here:

1. **Only the agent edits its memory.** Code never cuts lines out of a block,
   never string-replaces a name and never resets a guild. What code does is
   refuse: an output that still carries the user's ID, still names them
   without saying why, breaks a limit, or changes anything that does not
   mention them is sent back, and after the retries it fails.
2. **Bystanders are never rewritten.** A block (or a revision's block) that
   mentions neither the ID nor a listed name is not shown to the model as
   editable at all and is never written. In a block that does mention them,
   the model decides only the segments that mention them (keep, remove or
   rewrite); code rebuilds the block from the original bytes, so everything
   else comes back byte for byte and in order, and nothing can be added. A
   note that does not name them is never changed. An empty block stays
   empty; a placeholder such as ``(empty)`` is never written.
3. **A failure changes nothing.** Any refusal or exception leaves the blocks,
   notes and revisions exactly as they were. There is no fallback that blanks
   a block to make a purge "succeed".
4. **The target travels privately.** The ID and names are in the purge prompt
   and nowhere else; nothing here logs them.

The model calls run outside any database transaction. The guild's rows are
read into a snapshot first, the agent works on the snapshot, and only then a
short transaction takes the per-guild memory lock (the one the nightly dream
takes), checks the rows still match the snapshot, and writes. If anything
changed meanwhile (a dream, a new revision, a note gone) the guild is done
again from a fresh snapshot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable
from collections.abc import Callable
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.bot.agents.model_router import build_model_for
from smarter_dev.bot.agents.model_router import model_settings_for
from smarter_dev.shared.privacy_purge import PurgeTarget
from smarter_dev.shared.privacy_segment_edit import (
    PLACEHOLDERS,  # noqa: F401 — re-exported
)
from smarter_dev.shared.privacy_segment_edit import Segment
from smarter_dev.shared.privacy_segment_edit import SegmentEdit
from smarter_dev.shared.privacy_segment_edit import SegmentEditError
from smarter_dev.shared.privacy_segment_edit import apply_edits
from smarter_dev.shared.privacy_segment_edit import editable_segments
from smarter_dev.shared.privacy_segment_edit import rebuild  # noqa: F401 — re-exported
from smarter_dev.shared.privacy_segment_edit import segments  # noqa: F401 — re-exported
from smarter_dev.web.chat_memory_dream import DEFAULT_DREAM_MODEL
from smarter_dev.web.chat_memory_dream import DREAM_MODEL_ENV_VAR
from smarter_dev.web.chat_memory_dream import DREAM_REASONING_LEVEL
from smarter_dev.web.chat_memory_dream import MAX_IDENTITY_CHARS
from smarter_dev.web.chat_memory_dream import _dream_catalog_model
from smarter_dev.web.chat_memory_dream import identity_traits
from smarter_dev.web.crud import delete_notes_by_id
from smarter_dev.web.crud import get_guild_memory_blob
from smarter_dev.web.crud import list_memory_revisions
from smarter_dev.web.crud import lock_guild_memory
from smarter_dev.web.crud import prune_memory_revisions
from smarter_dev.web.crud import record_memory_revision
from smarter_dev.web.models import MAX_BEHAVIOR_CHARS
from smarter_dev.web.models import MAX_MEMORY_BLOB_CHARS
from smarter_dev.web.models import MAX_MEMORY_NOTE_CHARS
from smarter_dev.web.models import MAX_PERSONALITY_CHARS
from smarter_dev.web.models import ChatAgentMemoryNote

logger = logging.getLogger(__name__)

PURGE_OUTPUT_RETRIES = 2
# Every note the guild holds is reviewed, this many per model call.
PURGE_NOTES_BATCH = 100
# One agent run (its output retries included). Without it a call is bounded
# only by the provider client: 600 s per HTTP attempt, 3 attempts, 3 output
# tries, so 90 minutes.
PURGE_CALL_TIMEOUT_SECONDS = 900.0
# How many times a guild is done again from a fresh snapshot when its rows
# changed while the agent was working.
PURGE_SNAPSHOT_ATTEMPTS = 3

BLOCK_NAMES = ("memory", "behavior", "personality")
_BLOCK_LIMITS = {
    "memory": MAX_MEMORY_BLOB_CHARS,
    "behavior": MAX_BEHAVIOR_CHARS,
    "personality": MAX_PERSONALITY_CHARS,
}
_BLOCK_HEADINGS = {
    "personality": "# My personality",
    "behavior": "# My behavior",
    "memory": "# What I remember",
}

StillCurrent = Callable[[AsyncSession], Awaitable[bool]]


# -- segments --------------------------------------------------------------------
#
# The segmenter, the edit schema and the rebuild are shared with the bot and
# the worker: :mod:`smarter_dev.shared.privacy_segment_edit`.


class UnresolvedItem(BaseModel):
    """Something the agent could not settle: where it is, and why — never a quote."""

    location: str
    reason: str


class PurgeOutput(BaseModel):
    edits: list[SegmentEdit] = Field(default_factory=list)
    unresolved: list[UnresolvedItem] = Field(default_factory=list)


@dataclass(frozen=True)
class PurgeContext:
    """One purge pass: a set of blocks (only some editable) and a batch of notes."""

    target: PurgeTarget
    memory: str = ""
    behavior: str = ""
    personality: str = ""
    notes: tuple[tuple[str, str], ...] = ()

    @property
    def editable(self) -> tuple[str, ...]:
        """The blocks that mention the target: the only ones the agent sees."""
        return tuple(name for name in BLOCK_NAMES if self.target.mentions(getattr(self, name)))

    @property
    def mentioning_notes(self) -> tuple[str, ...]:
        return tuple(i for i, content in self.notes if self.target.mentions(content))

    def editable_by_location(self) -> dict[str, list[Segment]]:
        out = {name: editable_segments(name, getattr(self, name), self.target) for name in self.editable}
        for note_id, content in self.notes:
            if self.target.mentions(content):
                out[f"note:{note_id}"] = editable_segments(f"note:{note_id}", content, self.target)
        return out

    @property
    def needs_model(self) -> bool:
        return bool(self.editable or self.notes)


@dataclass(frozen=True)
class PurgedBlocks:
    memory: str
    behavior: str
    personality: str
    changed_blocks: frozenset[str] = frozenset()
    rewritten_notes: dict[str, str] = field(default_factory=dict)
    dropped_notes: tuple[str, ...] = ()
    unresolved: tuple[UnresolvedItem, ...] = ()


class PurgeRefused(Exception):
    """The agent's output could not be accepted; nothing was written."""


class PurgeStopped(Exception):
    """The run this purge belongs to was closed or superseded; nothing was written."""


class PurgeConflict(Exception):
    """The guild's memory kept changing under the purge; nothing was written."""


PURGE_SYSTEM_PROMPT = """\
You are the Smarter Dev Discord bot, going back over what you remember about one
server because someone has asked to be forgotten.

You are given one person's Discord user ID and the names they went by, and the
parts of your memory that mention them, cut into numbered segments (a line, a
sentence, or a part of a sentence). For every segment listed under
`# Segments to decide`, decide one of:

- `remove` — it is about this person, or came from them;
- `rewrite` — it mixes this person with someone else: give the new `text` for
  that segment only, about the other person, without the person's name or ID
  (do not repeat a list marker such as "- "; it is kept for you);
- `keep` — the name belongs to a different person who shares it. Say so in
  `unresolved` for that block or note.

Everything that is not a listed segment stays exactly as written; you cannot
change it, and you cannot add anything. Do not write that someone was removed,
forgotten or asked anything.

`# Notes, read only` are notes that do not name the person. You cannot change
them. If one is about this person without naming them, list it in `unresolved`
as `note:<id>` with a short reason, so the admin can look.

`unresolved` items are `location` (`memory`, `behavior`, `personality` or
`note:<id>`) and a short `reason`. Never quote the text and never write the
person's name or ID in a reason.

Return only the structured output: one entry in `edits` per listed segment id.
"""


def build_purge_user_message(context: PurgeContext) -> str:
    names = ", ".join(context.target.names) or "(no names known)"
    parts = [f"# The person\n\nDiscord user ID {context.target.user_id}; names: {names}"]
    for name in ("personality", "behavior", "memory"):
        if name in context.editable:
            parts.append(f"{_BLOCK_HEADINGS[name]}\n\n{getattr(context, name)}")
    mentioning = [(i, c) for i, c in context.notes if context.target.mentions(c)]
    if mentioning:
        parts.append(
            "# Notes that mention the person\n\n"
            + "\n".join(f"[note:{i}] {c}" for i, c in mentioning)
        )
    decide = [
        f"[{seg.id}] {seg.body}"
        for segs in context.editable_by_location().values()
        for seg in segs
    ]
    parts.append("# Segments to decide\n\n" + ("\n".join(decide) or "None this time."))
    read_only = [(i, c) for i, c in context.notes if not context.target.mentions(c)]
    if read_only:
        parts.append(
            "# Notes, read only\n\n" + "\n".join(f"[note:{i}] {c}" for i, c in read_only)
        )
    return "\n\n".join(parts)


# -- validation ----------------------------------------------------------------


def _identity_chars(memory: str) -> int:
    traits = identity_traits(memory)
    if not traits:
        return 0
    return len("## Identity & Voice\n" + "\n".join(f"- {t}" for t in traits))


def compose_purge(
    output: PurgeOutput, context: PurgeContext, *, retries_left: int
) -> PurgedBlocks:
    """Accept the agent's decisions and rebuild the text from them, or refuse.

    Every problem is a :class:`ModelRetry` while retries remain and a
    :class:`PurgeRefused` once they are gone. No problem message quotes
    stored text.
    """

    def refuse(problem: str) -> PurgeRefused:
        if retries_left > 0:
            raise ModelRetry(problem)
        return PurgeRefused(problem)

    target = context.target
    unresolved_locations = {item.location.strip() for item in output.unresolved}
    valid_locations = set(context.editable) | {f"note:{i}" for i, _ in context.notes}
    for location in unresolved_locations:
        if location not in valid_locations:
            raise refuse(
                f"Unresolved location {location!r} is not one of the blocks you were "
                "shown or a note:<id>."
            )
    reasons = " ".join(item.reason for item in output.unresolved)
    if target.mentions(reasons):
        raise refuse("Unresolved reasons must not name the person or carry their ID.")

    texts = {name: getattr(context, name) for name in BLOCK_NAMES}
    texts.update({f"note:{note_id}": content for note_id, content in context.notes})
    try:
        rebuilt = apply_edits(texts, output.edits, target, unresolved=unresolved_locations)
    except SegmentEditError as error:
        raise refuse(str(error)) from None

    final: dict[str, str] = {}
    changed: set[str] = set()
    for name in BLOCK_NAMES:
        previous = getattr(context, name)
        new = rebuilt[name]
        final[name] = new
        if new != previous:
            if len(new.strip()) > _BLOCK_LIMITS[name]:
                raise refuse(f"`{name}` would be over its {_BLOCK_LIMITS[name]}-character limit.")
            changed.add(name)
    if "memory" in changed and _identity_chars(final["memory"]) > MAX_IDENTITY_CHARS:
        raise refuse("Identity & Voice would be over 800 characters.")

    rewritten: dict[str, str] = {}
    dropped: list[str] = []
    for note_id, content in context.notes:
        new = rebuilt[f"note:{note_id}"]
        if new == content:
            continue
        if not new.strip():
            dropped.append(note_id)
        elif len(new.strip()) > MAX_MEMORY_NOTE_CHARS:
            raise refuse(f"Note {note_id!r} would be over {MAX_MEMORY_NOTE_CHARS} characters.")
        else:
            rewritten[note_id] = new

    return PurgedBlocks(
        memory=final["memory"],
        behavior=final["behavior"],
        personality=final["personality"],
        changed_blocks=frozenset(changed),
        rewritten_notes=rewritten,
        dropped_notes=tuple(dropped),
        unresolved=tuple(output.unresolved),
    )


def _untouched(context: PurgeContext) -> PurgedBlocks:
    return PurgedBlocks(
        memory=context.memory, behavior=context.behavior, personality=context.personality
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


async def _purge_once(agent: PurgeAgent, context: PurgeContext) -> tuple[PurgedBlocks, int]:
    """One pass; returns the result and how many model calls it took (0 or 1)."""
    if not context.needs_model:
        return _untouched(context), 0
    async with asyncio.timeout(PURGE_CALL_TIMEOUT_SECONDS):
        result = await agent.run(build_purge_user_message(context), deps=context)
    # Validate again at the persistence boundary, including injected agents.
    return compose_purge(result.output, context, retries_left=0), 1


# -- one guild -----------------------------------------------------------------


GuildPurgeOutcome = Literal["purged", "unchanged", "empty"]


@dataclass(frozen=True)
class GuildPurgeResult:
    guild_id: str
    outcome: GuildPurgeOutcome
    notes_rewritten: int = 0
    notes_dropped: int = 0
    notes_reviewed: int = 0
    revisions_rewritten: int = 0
    model_calls: int = 0
    unresolved: tuple[UnresolvedItem, ...] = ()

    def as_step(self) -> dict:
        return {
            "outcome": self.outcome,
            "notes_rewritten": self.notes_rewritten,
            "notes_dropped": self.notes_dropped,
            "notes_reviewed": self.notes_reviewed,
            "revisions_rewritten": self.revisions_rewritten,
            "model_calls": self.model_calls,
            "unresolved": [item.model_dump() for item in self.unresolved],
        }


SessionFactory = Callable[[], contextlib.AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True)
class _Snapshot:
    """What a purge pass read, as plain values: the compare-and-set baseline."""

    blob: tuple[str, str, str, int] | None = None
    revisions: tuple[tuple[str, int, str, str, str], ...] = ()
    notes: tuple[tuple[str, str], ...] = ()

    @property
    def empty(self) -> bool:
        return self.blob is None and not self.revisions and not self.notes


def _blob_key(memory) -> tuple[str, str, str, int] | None:
    if memory is None:
        return None
    return (memory.content or "", memory.behavior or "", memory.personality or "", memory.revision)


def _revision_key(revision) -> tuple[str, int, str, str, str]:
    return (
        str(revision.id),
        revision.revision,
        revision.content or "",
        revision.behavior or "",
        revision.personality or "",
    )


async def _guild_notes(session: AsyncSession, guild_id: str) -> list[ChatAgentMemoryNote]:
    """Every note the guild holds, oldest first. No limit: all are reviewed."""
    return list(
        (
            await session.scalars(
                select(ChatAgentMemoryNote)
                .where(ChatAgentMemoryNote.guild_id == guild_id)
                .order_by(ChatAgentMemoryNote.created_at, ChatAgentMemoryNote.id)
            )
        ).all()
    )


def _batches(notes: tuple[tuple[str, str], ...]) -> list[tuple[tuple[str, str], ...]]:
    return [notes[i : i + PURGE_NOTES_BATCH] for i in range(0, len(notes), PURGE_NOTES_BATCH)]


@dataclass
class _Plan:
    blocks: PurgedBlocks | None = None
    revisions: dict[str, PurgedBlocks] = field(default_factory=dict)
    rewritten_notes: dict[str, str] = field(default_factory=dict)
    dropped_notes: list[str] = field(default_factory=list)
    unresolved: list[UnresolvedItem] = field(default_factory=list)
    model_calls: int = 0


async def _plan_notes(
    agent: PurgeAgent,
    target: PurgeTarget,
    notes: tuple[tuple[str, str], ...],
    plan: _Plan,
    *,
    first_context: PurgeContext | None = None,
) -> None:
    """Review ``notes`` in batches; the first batch rides with ``first_context``'s blocks."""
    batches = _batches(notes) or [()]
    for index, batch in enumerate(batches):
        if index == 0 and first_context is not None:
            context = PurgeContext(
                target=target,
                memory=first_context.memory,
                behavior=first_context.behavior,
                personality=first_context.personality,
                notes=batch,
            )
        else:
            context = PurgeContext(target=target, notes=batch)
        result, calls = await _purge_once(agent, context)
        plan.model_calls += calls
        if index == 0 and first_context is not None:
            plan.blocks = result
        plan.rewritten_notes.update(result.rewritten_notes)
        plan.dropped_notes.extend(result.dropped_notes)
        plan.unresolved.extend(result.unresolved)


async def _check_current(session: AsyncSession, still_current: StillCurrent | None) -> None:
    if still_current is not None and not await still_current(session):
        raise PurgeStopped("the run was closed or superseded")


def _apply_notes(notes_by_id: dict, plan: _Plan, now: datetime) -> None:
    for note_id, text in plan.rewritten_notes.items():
        notes_by_id[note_id].content = text
        notes_by_id[note_id].updated_at = now


async def purge_guild_memory(
    session_factory: SessionFactory,
    *,
    guild_id: str,
    target: PurgeTarget,
    now: datetime,
    agent: PurgeAgent | None = None,
    still_current: StillCurrent | None = None,
) -> GuildPurgeResult:
    """Have the agent remove ``target`` from one guild's memory.

    Snapshot blocks, every note and every retained revision (no lock, no open
    transaction) → one pass over the blocks that mention the target together
    with the first batch of notes, one pass per further batch of notes, one
    pass per distinct retained revision that mentions the target → a short
    transaction: lock the guild, ``still_current``, compare with the snapshot,
    write, commit. A changed guild is done again from a fresh snapshot.
    Anything raised leaves every row as it was.
    """
    purge_agent = agent
    total_calls = 0
    for _attempt in range(PURGE_SNAPSHOT_ATTEMPTS):
        async with session_factory() as session:
            memory = await get_guild_memory_blob(session, guild_id)
            snapshot = _Snapshot(
                blob=_blob_key(memory),
                revisions=tuple(
                    _revision_key(r) for r in await list_memory_revisions(session, guild_id)
                ),
                notes=tuple((str(n.id), n.content) for n in await _guild_notes(session, guild_id)),
            )
            await session.rollback()
        if snapshot.empty:
            return GuildPurgeResult(guild_id=guild_id, outcome="empty")

        if purge_agent is None:
            purge_agent = get_purge_agent()
        plan = _Plan()
        live = PurgeContext(
            target=target,
            memory=snapshot.blob[0] if snapshot.blob else "",
            behavior=snapshot.blob[1] if snapshot.blob else "",
            personality=snapshot.blob[2] if snapshot.blob else "",
        )
        await _plan_notes(purge_agent, target, snapshot.notes, plan, first_context=live)
        # Revisions are copies of the same memory: each distinct one that
        # mentions the target gets its own pass, one identical to the live
        # blocks reuses the live result, and one that does not mention the
        # target is never sent and never written.
        by_blocks: dict[tuple[str, str, str], PurgedBlocks] = {
            (live.memory, live.behavior, live.personality): plan.blocks
        }
        for rev_id, rev_number, content, behavior, personality in snapshot.revisions:
            context = PurgeContext(
                target=target, memory=content, behavior=behavior, personality=personality
            )
            if not context.editable:
                continue
            key = (content, behavior, personality)
            if key not in by_blocks:
                result, calls = await _purge_once(purge_agent, context)
                plan.model_calls += calls
                by_blocks[key] = result
                plan.unresolved.extend(
                    UnresolvedItem(location=f"revision:{rev_number}/{item.location}", reason=item.reason)
                    for item in result.unresolved
                )
            if by_blocks[key].changed_blocks:
                plan.revisions[rev_id] = by_blocks[key]
        total_calls += plan.model_calls

        blocks_changed = bool(plan.blocks and plan.blocks.changed_blocks)
        anything = (
            blocks_changed or plan.revisions or plan.rewritten_notes or plan.dropped_notes
        )
        async with session_factory() as session:
            await lock_guild_memory(session, guild_id)
            await _check_current(session, still_current)
            memory = await get_guild_memory_blob(session, guild_id)
            revisions = await list_memory_revisions(session, guild_id)
            if _blob_key(memory) != snapshot.blob or tuple(
                _revision_key(r) for r in revisions
            ) != snapshot.revisions:
                await session.rollback()
                continue
            touched = [*plan.rewritten_notes, *plan.dropped_notes]
            notes_by_id = {}
            if touched:
                rows = (
                    await session.scalars(
                        select(ChatAgentMemoryNote).where(
                            ChatAgentMemoryNote.id.in_([UUID(i) for i in touched])
                        )
                    )
                ).all()
                notes_by_id = {str(n.id): n for n in rows}
                originals = dict(snapshot.notes)
                if any(
                    i not in notes_by_id or notes_by_id[i].content != originals[i]
                    for i in touched
                ):
                    await session.rollback()
                    continue
            if not anything:
                await session.rollback()
                return GuildPurgeResult(
                    guild_id=guild_id,
                    outcome="unchanged",
                    notes_reviewed=len(snapshot.notes),
                    model_calls=total_calls,
                    unresolved=tuple(plan.unresolved),
                )

            for revision in revisions:
                purged = plan.revisions.get(str(revision.id))
                if purged is not None:
                    revision.content = purged.memory
                    revision.behavior = purged.behavior
                    revision.personality = purged.personality
            if blocks_changed and memory is not None:
                model_name = os.getenv(DREAM_MODEL_ENV_VAR, DEFAULT_DREAM_MODEL)
                memory.content = plan.blocks.memory
                memory.behavior = plan.blocks.behavior
                memory.personality = plan.blocks.personality
                memory.revision = memory.revision + 1
                memory.model_name = model_name
                memory.updated_at = now
                await session.flush()
                await record_memory_revision(
                    session,
                    guild_id=guild_id,
                    content=plan.blocks.memory,
                    behavior=plan.blocks.behavior,
                    personality=plan.blocks.personality,
                    revision=memory.revision,
                    notes_consumed=0,
                    model_name=model_name,
                )
                await prune_memory_revisions(session, guild_id)
            _apply_notes(notes_by_id, plan, now)
            await delete_notes_by_id(session, [UUID(i) for i in plan.dropped_notes])
            await session.commit()
        return GuildPurgeResult(
            guild_id=guild_id,
            outcome="purged",
            notes_rewritten=len(plan.rewritten_notes),
            notes_dropped=len(plan.dropped_notes),
            notes_reviewed=len(snapshot.notes),
            revisions_rewritten=len(plan.revisions),
            model_calls=total_calls,
            unresolved=tuple(plan.unresolved),
        )
    raise PurgeConflict("guild memory kept changing during the purge")


async def purge_guild_notes(
    session_factory: SessionFactory,
    *,
    guild_id: str,
    target: PurgeTarget,
    now: datetime,
    since: datetime | None,
    agent: PurgeAgent | None = None,
    still_current: StillCurrent | None = None,
) -> GuildPurgeResult:
    """The final notes pass, after the runtimes purged their histories.

    The bot may have written a note about the person from a history it had
    not folded yet. Every note created at or after ``since`` (the memory
    step) and every note that mentions the target goes through the agent,
    in batches; blocks and revisions are not touched here.
    """
    purge_agent = agent
    total_calls = 0
    for _attempt in range(PURGE_SNAPSHOT_ATTEMPTS):
        async with session_factory() as session:
            rows = await _guild_notes(session, guild_id)
            notes = tuple(
                (str(n.id), n.content)
                for n in rows
                if target.mentions(n.content) or (since is not None and _at_or_after(n.created_at, since))
            )
            await session.rollback()
        if not notes:
            return GuildPurgeResult(guild_id=guild_id, outcome="empty", model_calls=total_calls)
        if purge_agent is None:
            purge_agent = get_purge_agent()
        plan = _Plan()
        await _plan_notes(purge_agent, target, notes, plan)
        total_calls += plan.model_calls
        touched = [*plan.rewritten_notes, *plan.dropped_notes]
        if not touched:
            return GuildPurgeResult(
                guild_id=guild_id,
                outcome="unchanged",
                notes_reviewed=len(notes),
                model_calls=total_calls,
                unresolved=tuple(plan.unresolved),
            )
        async with session_factory() as session:
            await lock_guild_memory(session, guild_id)
            await _check_current(session, still_current)
            current = (
                await session.scalars(
                    select(ChatAgentMemoryNote).where(
                        ChatAgentMemoryNote.id.in_([UUID(i) for i in touched])
                    )
                )
            ).all()
            notes_by_id = {str(n.id): n for n in current}
            originals = dict(notes)
            if any(
                i not in notes_by_id or notes_by_id[i].content != originals[i] for i in touched
            ):
                await session.rollback()
                continue
            _apply_notes(notes_by_id, plan, now)
            await delete_notes_by_id(session, [UUID(i) for i in plan.dropped_notes])
            await session.commit()
        return GuildPurgeResult(
            guild_id=guild_id,
            outcome="purged",
            notes_rewritten=len(plan.rewritten_notes),
            notes_dropped=len(plan.dropped_notes),
            notes_reviewed=len(notes),
            model_calls=total_calls,
            unresolved=tuple(plan.unresolved),
        )
    raise PurgeConflict("guild notes kept changing during the purge")


def _at_or_after(value: datetime, since: datetime) -> bool:
    # SQLite hands back naive datetimes for timezone-aware columns.
    if value.tzinfo is None and since.tzinfo is not None:
        since = since.replace(tzinfo=None)
    elif value.tzinfo is not None and since.tzinfo is None:
        value = value.replace(tzinfo=None)
    return value >= since
