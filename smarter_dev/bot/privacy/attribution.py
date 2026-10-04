"""Is every member-written part of a stored history attributed to its author?

A privacy purge may skip a store only when folding it cannot remove anything:
no hit on the person's id or names anywhere, AND every part that could carry
a member's words says whose words they are. A part that does not (a line
rendered before ``uid=`` existed, a summary written from such lines) could be
the person under a nickname the purge was never told, so it forces a fold.

Summaries are the hard case: a summary of unattributed lines is itself
unattributed, forever. So summaries written from attributed input (an ordinary
compaction of an attributed history, or a privacy fold) carry
``ATTRIBUTION_MARK``; an unmarked summary counts as unattributed.

Host-written parts (system prompts, the agent's replies, tool calls and
returns) are not judged here; the purge searches them for the id and names.
"""

from __future__ import annotations

import re

from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart

ATTRIBUTION_MARK = "[attribution:v1]"
# Must match chat_compaction.COMPACTED_PREFIX (a test checks it).
CHAT_SUMMARY_PREFIX = "[compacted history]"
# How proactive memory notes (agent.memory_note_pair) begin.
MEMORY_NOTE_PREFIX = "[COMPACTION MEMORY NOTE"
# A member transcript line as render_transcript_line writes it.
_TRANSCRIPT_LINE = re.compile(r"^\[[^\]]+\] \[id=[^\]]*\] ")


def _text(content: object) -> str:
    return content if isinstance(content, str) else str(content)


def _user_texts(messages: list[ModelMessage]) -> list[str]:
    return [
        _text(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


def chat_summary_marked(text: str) -> bool:
    return text.startswith(f"{CHAT_SUMMARY_PREFIX} {ATTRIBUTION_MARK}")


def chat_history_attributed(messages: list[ModelMessage]) -> bool:
    """Every ``<message>`` tag names its author (``user-id=``, or ``self``
    for the bot) and every earlier summary is marked."""
    for text in _user_texts(messages):
        if text.startswith(CHAT_SUMMARY_PREFIX):
            if not chat_summary_marked(text):
                return False
            continue
        for chunk in text.split("<message")[1:]:
            tag = chunk.split(">", 1)[0]
            if 'user-id="' not in tag and 'self="true"' not in tag:
                return False
    return True


def _member_lines_attributed(text: str) -> tuple[bool, bool]:
    """(all transcript lines carry uid=, at least one does)."""
    seen = False
    for line in text.splitlines():
        if not _TRANSCRIPT_LINE.match(line):
            continue
        if "(uid=" not in line:
            return False, seen
        seen = True
    return True, seen


def proactive_history_attributed(messages: list[ModelMessage]) -> bool:
    """Every memory note is marked, every member transcript line (in briefs
    and tool returns) carries ``uid=``, and the history shows positive
    evidence of the attributed era: a marked note or an attributed line.
    Without that evidence the agent's replies, watcher summaries and skim
    returns may date from before attribution, so the history counts as
    unattributed."""
    evidence = False
    texts: list[str] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                texts.append(_text(part.content))
            elif isinstance(part, ToolReturnPart):
                texts.append(_text(part.content))
    for text in texts:
        if text.startswith(MEMORY_NOTE_PREFIX):
            first_line = text.split("\n", 1)[0]
            if ATTRIBUTION_MARK not in first_line:
                return False
            evidence = True
            continue
        attributed, seen = _member_lines_attributed(text)
        if not attributed:
            return False
        evidence = evidence or seen
    return evidence
