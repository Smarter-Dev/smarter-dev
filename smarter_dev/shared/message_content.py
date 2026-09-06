"""What may be written where verbatim Discord message text would otherwise go.

Discord grants the message-content intent on the promise that we do not keep
what people say. So every durable row that would carry message text carries
:data:`MESSAGE_CONTENT_PLACEHOLDER` instead, written that way at row
construction rather than scrubbed later. Empty text stays empty and absent
text stays absent: a placeholder is never invented where nobody said anything.

Verbatim text survives in exactly two places, both of which the policy allows
and neither of which this module touches: the chat agent's Redis working
history (2h TTL, verbatim tail after compaction) and the proactive agent's
history (compaction tail). Moderators auditing what was actually said use the
activity-channel audit log.

Both tiers import this: the web tier redacts the rows it stores, the bot tier
bounds the age of the Redis streams that carry message text in flight. Every
function here is pure — value in, new value out, argument untouched — because
a handler still runs against the verbatim trigger context whose audit copy is
redacted.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

MESSAGE_CONTENT_PLACEHOLDER: str = "[message content]"

CONTENT_RETENTION_WINDOW: timedelta = timedelta(hours=48)

_CHAT_MESSAGE_CONTENT_KEYS = frozenset({"body", "attachments"})

_HELP_PRESERVED_KEYS = frozenset({"author", "timestamp"})

_HUMAN_TEXT_PART_KINDS = frozenset({"user-prompt", "tool-return"})

_EXPLICIT_SUBMISSION_INTERACTION_TYPES = frozenset({"slash_command"})

_HANDLER_CONTENT_KEYS = frozenset(
    {
        "content",
        "message_content",
        "old_content",
        "starter_message_content",
        "attachments",
        "attachment_urls",
        "embeds",
        "thread_name",
    }
)


def _redact_present_text(text: str) -> str:
    return text if text == "" else MESSAGE_CONTENT_PLACEHOLDER


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_present_text(value)
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    return value


def _redact_mapping(mapping: dict, carries_message_text: Callable[[str], bool]) -> dict:
    return {
        key: _redact_value(value) if carries_message_text(key) else value
        for key, value in mapping.items()
    }


def _carries_chat_message_text(key: str) -> bool:
    return key in _CHAT_MESSAGE_CONTENT_KEYS


def _carries_help_message_text(key: str) -> bool:
    return key not in _HELP_PRESERVED_KEYS


def _carries_handler_message_text(key: str) -> bool:
    return key in _HANDLER_CONTENT_KEYS or key.endswith("_content")


def _redact_part(part: dict) -> dict:
    if part.get("part_kind") not in _HUMAN_TEXT_PART_KINDS:
        return dict(part)
    return part | {"content": _redact_value(part.get("content"))}


def redact_text(text: str | None) -> str | None:
    """The placeholder, unless there was no text to hide."""
    return None if text is None else _redact_present_text(text)


def redact_chat_agent_messages(messages: list[dict] | None) -> list[dict]:
    """Redact serialised chat-agent messages, the shape of a turn's triggers.

    Keeps the ids, reply pointers, reactions and flags the conversation detail
    view renders around the message body.
    """
    return [
        _redact_mapping(message, _carries_chat_message_text)
        for message in messages or []
    ]


def redact_model_message_parts(messages: list[dict] | None) -> list[dict] | None:
    """Redact the human text inside a serialised pydantic-ai message list.

    Model output, tool calls and system prompts pass through, so the transcript
    still shows which tools ran with which arguments and what the agent said.
    """
    if messages is None:
        return None
    return [
        message | {"parts": [_redact_part(part) for part in message["parts"]]}
        if "parts" in message
        else dict(message)
        for message in messages
    ]


def redact_help_context_messages(messages: list[dict] | None) -> list[dict]:
    """Redact the channel scrape a help conversation was answered against.

    Keeps each message's author and timestamp and nothing else, so a key added
    upstream carries a placeholder rather than what somebody said.
    """
    return [
        _redact_mapping(message, _carries_help_message_text)
        for message in messages or []
    ]


def redact_help_question(question: str, interaction_type: str) -> str:
    """Keep a question the member submitted to us; redact one we overheard.

    A slash-command argument was typed at the bot on purpose. A mention or a
    streak reply is the member's own Discord message and gets the placeholder.
    """
    if interaction_type in _EXPLICIT_SUBMISSION_INTERACTION_TYPES:
        return question
    return _redact_present_text(question)


def redact_trigger_context(context: dict) -> dict:
    """Redact the message text a handler run's audit context carries.

    Ids, flags, counts, role lists and timestamps stay, so the run still shows
    which trigger fired, in which channel, for whom.
    """
    return _redact_mapping(context, _carries_handler_message_text)


def oldest_retained_stream_id(now: datetime) -> str:
    """The Redis stream id below which entries are past the retention window.

    Raises:
        ValueError: if ``now`` is naive, which would read as local time and
            move the cutoff by the machine's UTC offset.
    """
    if now.tzinfo is None:
        raise ValueError("oldest_retained_stream_id requires a timezone-aware datetime")
    return f"{int((now - CONTENT_RETENTION_WINDOW).timestamp() * 1000)}-0"
