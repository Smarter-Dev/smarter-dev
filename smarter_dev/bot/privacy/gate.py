"""The one opt-out gate for the bot's agents and their memory (#100).

An agent here is an LLM with tools and memory (Zech, #100): the chat engine
in every mode and the proactive agent. Someone on the blocked-users list
(opted out from ``/privacy``, or deleted) must not have their Discord
messages read by an agent, and nothing about or from them may be written to
an agent's memory. Memory writers call :func:`refuses` before they persist
anything about or from a person and :func:`redact` on free text; stored
histories go through :func:`blank_model_messages` (chat) and
:func:`blank_proactive_messages` (proactive) when written and when read back.
Plain LLM calls without tools and memory (``/tldr``, ``/help``, ``/rules``,
the forum tagger, moderation triage) are not gated.

All of it reads the process-wide :class:`BlockedUsersCache`, so it fails
closed before the list has loaded and sees an opt-out from this process's
button at once. The web side has the same gate over the database in
:mod:`smarter_dev.web.privacy_gate`.

Opting out deletes nothing already held; that is the admin's deletion.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import SystemPromptPart
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart

from smarter_dev.bot.privacy.attribution import attributed_line
from smarter_dev.bot.privacy.attribution import is_transcript_line
from smarter_dev.bot.privacy.blocked_users import BLOCKED_PLACEHOLDER
from smarter_dev.bot.privacy.blocked_users import BlockedUsersCache
from smarter_dev.bot.privacy.blocked_users import get_blocked_users
from smarter_dev.bot.privacy.blocked_users import redact_blocked_mentions

__all__ = [
    "BLOCKED_PLACEHOLDER",
    "blank_chat_text",
    "blank_model_messages",
    "blank_proactive_messages",
    "blank_transcript_text",
    "redact",
    "refuses",
]


def refuses(*user_ids: Any) -> bool:
    """True when a memory write about or from any of ``user_ids`` must not
    happen. ``None`` ids (no subject, no known author) are ignored."""
    blocked = get_blocked_users()
    return any(u is not None and blocked.is_blocked(u) for u in user_ids)


def redact(text: str) -> str:
    """``text`` with every blocked person's id or mention replaced."""
    return redact_blocked_mentions(text, get_blocked_users())


# One rendered chat message (``chat_input_format._render_message``): the open
# tag on a line of its own, an XML-escaped body (it cannot hold a literal
# "\n</message>"), the close tag on a line of its own. " user-id" with its
# space, so "reply-to-user-id" on the bot's own message is not its author.
_CHAT_MESSAGE = re.compile(
    r'^<message\b[^>\n]*? user-id="(?P<uid>[0-9]{1,22})"[^>\n]*>\n.*?\n</message>$',
    re.MULTILINE | re.DOTALL,
)
_REPLY_TO_USER = re.compile(r'\breply-to-user-id="(?P<uid>[0-9]{1,22})"')
_REPLY_TO_NAME = re.compile(r' reply-to-username="[^"\n]*"')


def blank_chat_text(text: str, blocked: BlockedUsersCache | None = None) -> str:
    """Rendered chat input with every blocked author's ``<message>`` turned
    into the placeholder, and every other mention of a blocked id redacted.

    For text about to be stored (chat history, compaction summaries) or read
    back from a store written before the person opted out.
    """
    blocked = blocked or get_blocked_users()

    def message(match: re.Match[str]) -> str:
        if blocked.is_blocked(match["uid"]):
            return BLOCKED_PLACEHOLDER
        reply = _REPLY_TO_USER.search(match[0].partition("\n")[0])
        if reply is not None and blocked.is_blocked(reply["uid"]):
            # A reply to them keeps its words but not their name.
            return _REPLY_TO_NAME.sub("", match[0], count=1)
        return match[0]

    if "<message" in text:
        text = _CHAT_MESSAGE.sub(message, text)
    return redact_blocked_mentions(text, blocked)


# A proactive transcript line (``proactive.transcript.render_transcript_line``)
# starts with its time and message id; ``attribution`` reads its author.
_TRANSCRIPT_MESSAGE_ID = re.compile(r"^\[[^\]\n]+\] \[id=([0-9]*)\] ")


def blank_transcript_text(text: str, blocked: BlockedUsersCache | None = None) -> str:
    """Proactive transcript text with every blocked author's line (and the
    lines its message continues on) turned into the placeholder, and every
    other blocked id redacted."""
    blocked = blocked or get_blocked_users()
    out: list[str] = []
    in_blocked_message = False
    for line in text.split("\n"):
        if is_transcript_line(line):
            match = attributed_line(line)
            message_id = _TRANSCRIPT_MESSAGE_ID.match(line)
            in_blocked_message = match is not None and blocked.is_blocked(
                match["uid"], message_id[1] if message_id and message_id[1] else None
            )
            out.append(BLOCKED_PLACEHOLDER if in_blocked_message else line)
        elif not in_blocked_message:
            out.append(line)
    return redact_blocked_mentions("\n".join(out), blocked)


def _blank_content(content: Any, blank: Callable[[str], str]) -> Any:
    """Every string in a part's content, however nested, through ``blank``."""
    if isinstance(content, str):
        return blank(content)
    if isinstance(content, list):
        return [_blank_content(item, blank) for item in content]
    if isinstance(content, dict):
        return {key: _blank_content(value, blank) for key, value in content.items()}
    return content


def _blank_messages(
    messages: list[ModelMessage],
    member_text: Callable[[str], str],
    other_text: Callable[[str], str],
) -> list[ModelMessage]:
    out: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelRequest | ModelResponse):
            out.append(message)
            continue
        parts = []
        for part in message.parts:
            if isinstance(part, UserPromptPart | ToolReturnPart):
                part = replace(part, content=_blank_content(part.content, member_text))
            elif isinstance(part, SystemPromptPart | TextPart):
                part = replace(part, content=_blank_content(part.content, other_text))
            parts.append(part)
        out.append(replace(message, parts=parts))
    return out


def blank_model_messages(
    messages: list[ModelMessage], blocked: BlockedUsersCache | None = None
) -> list[ModelMessage]:
    """A copy of a stored chat history with blocked authors' messages blanked.

    Member input (user prompts, tool returns) goes through
    :func:`blank_chat_text`; the system prompt and the agent's replies have
    blocked ids redacted. Used before a history is written and when one
    written before someone opted out is read back for the model.
    """
    blocked = blocked or get_blocked_users()
    return _blank_messages(
        messages,
        lambda text: blank_chat_text(text, blocked),
        lambda text: redact_blocked_mentions(text, blocked),
    )


def blank_proactive_messages(
    messages: list[ModelMessage], blocked: BlockedUsersCache | None = None
) -> list[ModelMessage]:
    """:func:`blank_model_messages` for the proactive agent's history, whose
    member input is transcript lines rather than ``<message>`` tags."""
    blocked = blocked or get_blocked_users()
    return _blank_messages(
        messages,
        lambda text: blank_transcript_text(text, blocked),
        lambda text: redact_blocked_mentions(text, blocked),
    )


def without_blocked_lines(text: str | None) -> str | None:
    """``text`` without the lines that carry a blocked id, for a prompt.

    For the guild memory the dream wrote (which names people as
    ``<N:username>``, or ``username (id N)`` before #104): a line about someone
    who opted out stays stored but
    is not shown to any agent (#100). ``None`` stays ``None``.
    """
    if not text:
        return text
    blocked = get_blocked_users()
    lines = text.splitlines()
    kept = [line for line in lines if redact_blocked_mentions(line, blocked) == line]
    return text if len(kept) == len(lines) else "\n".join(kept)
