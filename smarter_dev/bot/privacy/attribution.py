"""Is every member-written part of a stored history attributed to its author?

A privacy purge may skip a store only when folding it cannot remove anything:
no hit on the person's id or names anywhere, AND every part that could carry
a member's words says, on its own, whose words they are. A part that does not
(a line rendered before ``uid=`` existed, a watcher summary or skim without
uids, a summary written from such lines) could be the person under a nickname
the purge was never told, so it forces a fold. Parts are judged one by one:
one old-era part anywhere means fold.

Summaries are the hard case: a summary of unattributed lines is itself
unattributed, forever. So summaries written from attributed input (an ordinary
compaction of an attributed history, or a privacy fold) carry
``ATTRIBUTION_MARK``; an unmarked summary counts as unattributed. The mark is
honoured only where the host writes it (the memory note that opens a
proactive history, the chat summary that opens a chat history), never in a
tool return or member text.

Host-written parts (system prompts, the agent's replies, tool calls, returns
of tools that do not read members) are not judged here; the purge searches
them for the id and names.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart

ATTRIBUTION_MARK = "[attribution:v1]"
# Must match chat_compaction.COMPACTED_PREFIX (a test checks it).
CHAT_SUMMARY_PREFIX = "[compacted history]"
# How proactive memory notes (agent.memory_note_pair) begin and end their
# first line; the mark sits right after the header.
MEMORY_NOTE_PREFIX = "[COMPACTION MEMORY NOTE"
MEMORY_NOTE_ACK = "Understood — that is my own memory note"
# Proactive tools whose returns carry members' messages or summaries of them.
MEMBER_READING_TOOLS = frozenset(
    {"lookup_message", "channel_history", "skim_messages", "read_notifications"}
)

# A transcript line as render_transcript_line writes it:
#   [time] [id=…] [BOT] TAG·display (uid=N) (reply to id=M): content
# The uid is only honoured at that position; "(uid=" inside the content (or
# a pre-uid display name followed by ": ") does not count.
_LINE_START = re.compile(r"^\[[^\]\n]+\] \[id=[^\]\n]*\] ")
_ATTRIBUTED_LINE = re.compile(
    r"^\[[^\]\n]+\] \[id=[^\]\n]*\] (?:\[BOT\] )?[A-Z]+·"
    r"(?P<display>(?:(?!: )[^\n])*?) \(uid=(?P<uid>[0-9]+)\)"
    r"(?: \(reply to id=[^)\n]*\))?: "
)
# Chat transcript attribution: <message ... user-id="N" username="name">.
_CHAT_AUTHOR = re.compile(
    r'<message\b[^>]*?\buser-id="(?P<uid>[0-9]+)"(?P<rest>[^>]*)>'
)
_CHAT_USERNAME = re.compile(r'\busername="(?P<name>[^"]*)"')


def _text(content: object) -> str:
    return content if isinstance(content, str) else str(content)


def attributed_line(line: str) -> re.Match[str] | None:
    """The match of an attributed transcript line, or None."""
    return _ATTRIBUTED_LINE.match(line)


def is_transcript_line(line: str) -> bool:
    return bool(_LINE_START.match(line))


def attributed_authors(text: str) -> Iterator[tuple[str, str]]:
    """(uid, display name) for every attributed author in ``text``: proactive
    transcript lines and chat ``<message>`` tags."""
    for line in text.splitlines():
        match = attributed_line(line)
        if match:
            yield match.group("uid"), match.group("display")
    for match in _CHAT_AUTHOR.finditer(text):
        name = _CHAT_USERNAME.search(match.group("rest"))
        yield match.group("uid"), name.group("name") if name else ""


def _lines_qualify(text: str) -> tuple[bool, bool]:
    """(no unattributed transcript line, at least one attributed line)."""
    seen = False
    for line in text.splitlines():
        if not is_transcript_line(line):
            continue
        if not attributed_line(line):
            return False, seen
        seen = True
    return True, seen


# -- chat ---------------------------------------------------------------------


def chat_summary_marked(text: str) -> bool:
    return text.startswith(f"{CHAT_SUMMARY_PREFIX} {ATTRIBUTION_MARK}")


def chat_history_attributed(messages: list[ModelMessage]) -> bool:
    """Every ``<message>`` tag names its author (``user-id=``, or ``self``
    for the bot); a summary is honoured only as the history's opening
    request, and only when marked."""
    for index, message in enumerate(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart):
                continue
            text = _text(part.content)
            if text.startswith(CHAT_SUMMARY_PREFIX):
                if index != 0 or not chat_summary_marked(text):
                    return False
                continue
            for chunk in text.split("<message")[1:]:
                tag = chunk.split(">", 1)[0]
                if 'user-id="' not in tag and 'self="true"' not in tag:
                    return False
    return True


# -- proactive ----------------------------------------------------------------


def _opening_note_marked(messages: list[ModelMessage]) -> bool:
    """The history opens with a host-written, marked memory note: a request
    holding only the note, answered by the agent's acknowledgement."""
    if len(messages) < 2:
        return False
    first, second = messages[0], messages[1]
    if not isinstance(first, ModelRequest) or len(first.parts) != 1:
        return False
    part = first.parts[0]
    if not isinstance(part, UserPromptPart):
        return False
    first_line = _text(part.content).split("\n", 1)[0]
    if not (
        first_line.startswith(MEMORY_NOTE_PREFIX)
        and first_line.endswith(ATTRIBUTION_MARK)
    ):
        return False
    return isinstance(second, ModelResponse) and any(
        _text(getattr(p, "content", "")).startswith(MEMORY_NOTE_ACK)
        for p in second.parts
    )


def proactive_history_attributed(messages: list[ModelMessage]) -> bool:
    """Every member-bearing part qualifies on its own.

    - The opening memory note qualifies only when marked (and in place).
    - Any other user part (a wake brief) and any return of a member-reading
      tool qualifies only with no unattributed transcript line and at least
      one ``uid=``-attributed line: a brief of watcher summaries without uids,
      or a skim, is an unattributed part.
    - The agent's replies and other tools are host-written: searched only.
    """
    marked_note = _opening_note_marked(messages)
    evidence = marked_note
    for index, message in enumerate(messages):
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                text = _text(part.content)
                if index == 0 and text.startswith(MEMORY_NOTE_PREFIX):
                    if not marked_note:
                        return False
                    continue
            elif (
                isinstance(part, ToolReturnPart)
                and part.tool_name in MEMBER_READING_TOOLS
            ):
                text = _text(part.content)
            else:
                continue
            no_bad_lines, has_attributed = _lines_qualify(text)
            if not (no_bad_lines and has_attributed):
                return False
            evidence = True
    return evidence
