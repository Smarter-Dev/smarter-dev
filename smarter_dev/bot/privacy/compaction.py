"""Privacy compaction: the agent's own model rewrites memory without one person.

A purge never edits memory with string replacement and never blanks it. Each
store is folded whole (no verbatim tail) by a model that is told, privately,
whose traces to leave out. The output is validated before anything is written:

- it must not contain the person's Discord user id: the model is asked again,
  up to ``ID_RETRIES`` more times, and if it still fails the step raises
  ``PrivacyCompactionFailed`` and the caller writes nothing;
- a whole-word, case-insensitive hit on one of their names gets one re-ask,
  after which the output is accepted and the hits are counted for the ack.

A model error or an output that does not fit its shape is an invalid attempt
on the same budget. Matching is the shared ``PurgeTarget`` (whole-number id, whole-word names).
Nothing here logs the id, the names or any memory text; never log a
``PurgeTarget`` (its repr carries both).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Generic
from typing import Literal
from typing import TypeVar

from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models import Model

from smarter_dev.bot.agents.chat_compaction import COMPACTED_PREFIX
from smarter_dev.bot.agents.chat_compaction import MAX_SUMMARY_CHARS
from smarter_dev.bot.agents.chat_compaction import _collect_system_parts
from smarter_dev.bot.agents.chat_compaction import _render_transcript
from smarter_dev.bot.privacy.attribution import ATTRIBUTION_MARK
from smarter_dev.bot.proactive.agent import memory_note_pair
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.environment import WatchInstruction
from smarter_dev.shared.privacy_purge import PurgeTarget

logger = logging.getLogger(__name__)

ID_RETRIES = 2
# One model call of a purge. Purges run while holding the chat run lock, the
# proactive wake lock and the guild privacy lock, so a hung provider must not
# hold them: a timeout fails the step at once and nothing is written.
MODEL_TIMEOUT_SECONDS = 120
T = TypeVar("T")

ID_FEEDBACK = (
    "CORRECTION: your previous output still contained the removed person's "
    "Discord user id. Write it again with no trace of that id."
)
NAME_FEEDBACK = (
    "CORRECTION: your previous output still mentioned the removed person by "
    "name. Write it again without them; keep a name only where it clearly "
    "means something or someone else."
)
INVALID_FEEDBACK = (
    "CORRECTION: your previous output could not be used. Follow the required "
    "output format exactly."
)


def private_brief(target: PurgeTarget) -> str:
    """The person to remove, for the model's prompt only."""
    names = "; ".join(target.names) if target.names else "(no names given)"
    return (
        "PERSON TO REMOVE (private; never repeat these values):\n"
        f"- Discord user id: {target.user_id}\n"
        f"- names they have used: {names}"
    )


class PrivacyCompactionFailed(Exception):
    """The model gave no valid output; the store must stay as it was."""


@dataclass(frozen=True)
class Validated(Generic[T]):
    output: T
    name_hits: int
    attempts: int


async def generate_validated(
    produce: Callable[[str | None], Awaitable[T]],
    texts: Callable[[T], Iterable[str]],
    target: PurgeTarget,
    *,
    id_retries: int = ID_RETRIES,
    emptied: Callable[[T], str | None] | None = None,
) -> Validated[T]:
    """Ask the model until its output is free of the target's id.

    The rewrite checks (privacy:v1), and nothing else:
    1. ``emptied(output)`` names a field that came back empty although its
       input was not: the step fails at once (never a reset);
    2. the user id in the output: re-asked up to ``id_retries`` times, then
       the step fails;
    3. a listed name still present after one re-ask is accepted and counted;
    4. a model timeout or error: the step fails (an error after the first
       attempt is retried within the same budget as 2).

    ``produce(feedback)`` runs the model once; ``feedback`` is None on the
    first attempt and a correction afterwards. ``texts`` lists every string of
    the output that would be written.
    """
    feedback: str | None = None
    retries_left = id_retries
    name_reask_used = False
    attempts = 0
    while True:
        attempts += 1
        try:
            output = await asyncio.wait_for(
                produce(feedback), timeout=MODEL_TIMEOUT_SECONDS
            )
            written = "\n".join(texts(output))
        except TimeoutError:
            logger.warning(
                "privacy compaction attempt %d timed out; step failed", attempts
            )
            raise PrivacyCompactionFailed("model call timed out") from None
        except Exception as error:  # noqa: BLE001 — an invalid attempt
            logger.warning(
                "privacy compaction attempt %d produced no usable output (%s)",
                attempts,
                type(error).__name__,
            )
            if retries_left <= 0:
                raise PrivacyCompactionFailed("no valid output") from None
            retries_left -= 1
            feedback = INVALID_FEEDBACK
            continue
        if target.id_hits(written):
            logger.warning(
                "privacy compaction attempt %d still carried the user id", attempts
            )
            if retries_left <= 0:
                raise PrivacyCompactionFailed("output kept the user id")
            retries_left -= 1
            feedback = ID_FEEDBACK
            continue
        field = emptied(output) if emptied is not None else None
        if field is not None:
            logger.warning(
                "privacy compaction attempt %d emptied %s; step failed",
                attempts,
                field,
            )
            raise PrivacyCompactionFailed(f"{field} came back empty")
        hits = target.name_hits(written)
        if hits and not name_reask_used:
            name_reask_used = True
            feedback = NAME_FEEDBACK
            continue
        return Validated(output=output, name_hits=hits, attempts=attempts)


def _with_feedback(prompt: str, feedback: str | None) -> str:
    return prompt if feedback is None else f"{prompt}\n\n{feedback}"


# -- chat agent: history + topic + notes --------------------------------------

CHAT_PURGE_PROMPT = """\
You rewrite a Discord chat agent's working memory for one channel because a
person asked to be removed from it. You get the agent's whole conversation
history as a transcript (XML `<message>` blocks with `user-id` and `username`
attributes, the agent's replies, tool calls and returns; it may open with an
earlier `[compacted history]` summary), plus the channel's current topic line
and notes.

Remove every trace of the person named privately below: their messages, what
they asked, said, shared or decided, replies addressed to them, facts about
them, and the fact that they took part. Do not mention that anyone was
removed. Keep everything else, attributed exactly as before.

Output three fields:
- `summary`: the running summary the agent will read in place of the whole
  history. Same rules as always: attribute every kept question, claim,
  request or decision as `username (id <user-id>)`; structure it as a
  `Participants:` line, topic bullets, and an `Agent state:` section. At most
  {max_chars} characters. If there is no transcript, return an empty string.
  If nothing remains once the person is removed, say briefly that the
  conversation has no open threads.
- `topic`: the channel's topic line (1-2 sentences) rewritten the same way;
  an empty string if no topic was given.
- `notes`: the channel's notes (1-5 sentences) rewritten the same way; an
  empty string if no notes were given.
Always return all three fields.

No preamble, no quoting, no apologies."""


class ChatPurgeOutput(BaseModel):
    # No defaults that could blank a store: a missing field stays None, and
    # a field whose input was non-empty must come back non-empty.
    summary: str | None = None
    topic: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ChatPurgeResult:
    history: list[ModelMessage]
    topic: str | None
    notes: str | None
    name_hits: int


def _chat_purge_prompt(
    transcript: str, topic: str | None, notes: str | None, target: PurgeTarget
) -> str:
    sections = [private_brief(target)]
    sections.append(
        f"<transcript>\n{transcript}\n</transcript>"
        if transcript
        else "<transcript/> (no transcript)"
    )
    sections.append(f"<topic>\n{topic}\n</topic>" if topic else "<topic/> (none)")
    sections.append(f"<notes>\n{notes}\n</notes>" if notes else "<notes/> (none)")
    return "\n\n".join(sections)


async def purge_chat_memory(
    history: list[ModelMessage],
    topic: str | None,
    notes: str | None,
    target: PurgeTarget,
    *,
    model: Model | str,
) -> ChatPurgeResult:
    """Fold one channel's whole chat memory into a summary without ``target``.

    Raises ``PrivacyCompactionFailed`` when the model cannot produce valid
    output; the caller then writes nothing.
    """
    agent = Agent(
        model,
        output_type=ChatPurgeOutput,
        system_prompt=CHAT_PURGE_PROMPT.format(max_chars=MAX_SUMMARY_CHARS),
    )
    transcript = _render_transcript(history) if history else ""
    prompt = _chat_purge_prompt(transcript, topic, notes, target)

    async def produce(feedback: str | None) -> ChatPurgeOutput:
        result = await agent.run(_with_feedback(prompt, feedback))
        output = result.output

        def clean(value: str | None) -> str | None:
            return None if value is None else value.strip()

        summary = clean(output.summary)
        return ChatPurgeOutput(
            summary=None if summary is None else summary[:MAX_SUMMARY_CHARS],
            topic=clean(output.topic),
            notes=clean(output.notes),
        )

    def emptied(output: ChatPurgeOutput) -> str | None:
        for name, had_input, value in (
            ("summary", bool(history), output.summary),
            ("topic", bool(topic), output.topic),
            ("notes", bool(notes), output.notes),
        ):
            if had_input and not value:
                return name
        return None

    validated = await generate_validated(
        produce,
        lambda output: (output.summary or "", output.topic or "", output.notes or ""),
        target,
        emptied=emptied,
    )
    output = validated.output
    new_history: list[ModelMessage] = []
    if history:
        # The system prompt rides in the first request; it must survive.
        new_history = [
            ModelRequest(
                parts=[
                    *_collect_system_parts(history),
                    UserPromptPart(
                        content=(
                            f"{COMPACTED_PREFIX} {ATTRIBUTION_MARK} "
                            f"{output.summary or ''}"
                        )
                    ),
                ]
            )
        ]
    return ChatPurgeResult(
        history=new_history,
        topic=(output.topic or "") if topic else None,
        notes=(output.notes or "") if notes else None,
        name_hits=validated.name_hits,
    )


# -- proactive agent: guild / channel history ---------------------------------

PROACTIVE_PURGE_PROMPT = """\
Your rolling context is being compacted because a person asked to be removed
from your memory: everything above this message will be replaced by the note
you write now. Write the memory your future self needs to continue
seamlessly, exactly as you would for an ordinary compaction — conversations
still in motion and who is in them, commitments or follow-ups you made, what
you have learned about the people and channels you watch — attributing every
statement to WHO said or did it and WHERE (channel name and id).

Leave out every trace of the person below: their messages, what they asked,
said, shared or decided, your replies to them, facts about them, and the fact
that they took part. Do not mention that anyone was removed. Never write
their user id or any of their names.

{target}"""


async def purge_proactive_history(
    history: list[ModelMessage],
    target: PurgeTarget,
    *,
    model: Model | str,
) -> tuple[list[ModelMessage], int]:
    """Fold a proactive agent history into one memory note without ``target``.

    Returns the new history (the standard 2-message memory-note pair) and the
    remaining name-hit count. Raises ``PrivacyCompactionFailed``.
    """
    agent = Agent(model, output_type=str)
    prompt = PROACTIVE_PURGE_PROMPT.format(target=private_brief(target))

    async def produce(feedback: str | None) -> str:
        result = await agent.run(
            _with_feedback(prompt, feedback), message_history=history
        )
        return (result.output or "").strip()

    def emptied(note: str) -> str | None:
        return "note" if history and not note else None

    validated = await generate_validated(
        produce, lambda note: (note,), target, emptied=emptied
    )
    return (
        memory_note_pair(validated.output, attributed=True),
        validated.name_hits,
    )


# -- proactive watch instructions ---------------------------------------------

WATCH_PURGE_PROMPT = """\
You maintain the watch instructions a Discord bot set for its watcher (wake
criteria with an id each). A person asked to be removed from the bot's
memory. For EVERY instruction below decide one action:
- `keep` — it has nothing to do with that person; leave it unchanged.
- `rewrite` — it is still useful without them; give the new text in `text`,
  with no trace of the person.
- `drop` — it only makes sense because of them.
Return one decision per instruction id, no more, no fewer.

{target}

INSTRUCTIONS:
{instructions}"""


class InstructionDecision(BaseModel):
    instruction_id: str
    action: Literal["keep", "rewrite", "drop"]
    text: str = ""


class InstructionDecisions(BaseModel):
    decisions: list[InstructionDecision] = Field(default_factory=list)


@dataclass(frozen=True)
class WatchPurgeResult:
    entries: list[WatchInstruction]
    kept: int
    rewritten: int
    dropped: int
    name_hits: int

    @property
    def changed(self) -> bool:
        return bool(self.rewritten or self.dropped)


async def purge_watch_instructions(
    store: InstructionStore,
    target: PurgeTarget,
    *,
    model: Model | str,
) -> WatchPurgeResult:
    """Have the model keep, rewrite or drop each watch instruction.

    Raises ``PrivacyCompactionFailed``; the caller then persists nothing.
    """
    entries = list(store.entries)
    by_id = {entry.instruction_id: entry for entry in entries}
    agent = Agent(model, output_type=InstructionDecisions)
    prompt = WATCH_PURGE_PROMPT.format(
        target=private_brief(target),
        instructions="\n".join(
            f"- {entry.instruction_id}: {entry.text}" for entry in entries
        ),
    )

    async def produce(feedback: str | None) -> list[WatchInstruction]:
        result = await agent.run(_with_feedback(prompt, feedback))
        decisions = {d.instruction_id: d for d in result.output.decisions}
        if set(decisions) != set(by_id) or len(result.output.decisions) != len(by_id):
            raise ValueError("decisions do not cover every instruction once")
        new_entries: list[WatchInstruction] = []
        for entry in entries:
            decision = decisions[entry.instruction_id]
            if decision.action == "drop":
                continue
            if decision.action == "keep":
                new_entries.append(entry)
                continue
            text = decision.text.strip()
            if not text:
                raise ValueError("rewrite without text")
            new_entries.append(
                WatchInstruction(
                    instruction_id=entry.instruction_id,
                    text=text,
                    expires_at=entry.expires_at,
                )
            )
        return new_entries

    validated = await generate_validated(
        produce, lambda kept: [entry.text for entry in kept], target
    )
    new_entries = validated.output
    kept = sum(
        1 for entry in new_entries if by_id[entry.instruction_id].text == entry.text
    )
    rewritten = len(new_entries) - kept
    return WatchPurgeResult(
        entries=new_entries,
        kept=kept,
        rewritten=rewritten,
        dropped=len(entries) - len(new_entries),
        name_hits=validated.name_hits,
    )
