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

Each stored shape is redacted by a keep-list, never a redact-list: a chat
message keeps :data:`_CHAT_PRESERVED_KEYS`, a help context message keeps
:data:`_HELP_PRESERVED_KEYS`, a pydantic-ai part keeps its whole self only when
its kind is in :data:`_MODEL_AUTHORED_PART_KINDS` and otherwise keeps
:data:`_REDACTED_PART_PRESERVED_FIELDS`. A forum post goes further and is
built rather than filtered: :func:`redact_forum_post` returns the three
member-authored columns of the row and reads nothing else, so a key the sender
invents cannot reach it. Anything upstream adds later is therefore redacted
until somebody decides it is safe, which is the failure mode the intent policy
can live with. The handler trigger context is the one redact-list
(:data:`_HANDLER_CONTENT_KEYS` plus the ``_content`` suffix)
because we build every key in it — except a timer re-fire's ``payload``, whose
keys a handler script chose, which is why that whole value is emptied.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

MESSAGE_CONTENT_PLACEHOLDER: str = "[message content]"

CONTENT_RETENTION_WINDOW: timedelta = timedelta(hours=48)

_CHAT_PRESERVED_KEYS = frozenset(
    {
        "message_id",
        "author_id",
        "reply_to_message_id",
        "reply_to_author_id",
        "reply_to_is_self",
        "reactions",
        "sent_at",
        "mentions_bot",
    }
)

_HELP_PRESERVED_KEYS = frozenset({"author", "timestamp"})

_MODEL_AUTHORED_PART_KINDS = frozenset(
    {
        "system-prompt",
        "text",
        "thinking",
        "tool-call",
        "tool-search-call",
        "builtin-tool-call",
        "builtin-tool-search-call",
        "builtin-tool-return",
        "builtin-tool-search-return",
        "compaction",
        "file",
    }
)

_REDACTED_PART_PRESERVED_FIELDS = frozenset(
    {
        "part_kind",
        "tool_name",
        "tool_call_id",
        "tool_kind",
        "timestamp",
        "outcome",
    }
)

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
        "payload",
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


def _redact_mapping(mapping: dict, should_redact: Callable[[str], bool]) -> dict:
    return {
        key: _redact_value(value) if should_redact(key) else value
        for key, value in mapping.items()
    }


def _is_redacted_chat_key(key: str) -> bool:
    return key not in _CHAT_PRESERVED_KEYS


def _is_redacted_help_key(key: str) -> bool:
    return key not in _HELP_PRESERVED_KEYS


def _is_redacted_handler_key(key: str) -> bool:
    return key in _HANDLER_CONTENT_KEYS or key.endswith("_content")


def _is_redacted_part_field(key: str) -> bool:
    return key not in _REDACTED_PART_PRESERVED_FIELDS


def _redact_part(part: dict) -> dict:
    if part.get("part_kind") in _MODEL_AUTHORED_PART_KINDS:
        return dict(part)
    return _redact_mapping(part, _is_redacted_part_field)


def redact_text(text: str | None) -> str | None:
    """The placeholder, unless there was no text to hide."""
    return None if text is None else _redact_present_text(text)


def redact_chat_agent_messages(messages: list[dict] | None) -> list[dict]:
    """Redact serialised chat-agent messages, the shape of a turn's triggers.

    Keeps the ids, reply pointers, reactions and flags the conversation detail
    view renders around the message body and nothing else, so a field added
    upstream carries a placeholder rather than what somebody said.
    """
    return [
        _redact_mapping(message, _is_redacted_chat_key)
        for message in messages or []
    ]


def redact_model_message_parts(messages: list[dict] | None) -> list[dict] | None:
    """Redact the human text inside a serialised pydantic-ai message list.

    Parts the model or its provider authored pass through: the system prompt,
    reply text, reasoning, tool calls and provider-side tool results. Reasoning
    may restate what a member said, but it is derived text in the same class
    as ``agent_output`` and the retention sweep bounds it at 48h with the rest
    of the delta. Everything else we send the model — prompts, tool returns,
    retry prompts and any kind this module has never seen — is redacted down
    to its bookkeeping: kind, tool name and call id, tool kind, timestamp and
    outcome. Content, metadata and any field added later are emptied, and a
    field that was absent stays absent.
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
        _redact_mapping(message, _is_redacted_help_key)
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


def redact_forum_post(post: dict) -> dict:
    """The member-authored columns of a forum-agent response row.

    A forum post's title is as much a member's own words as its body, and its
    attachment filenames are member-supplied text, so all three are redacted
    and only what the agent decided about the post is stored verbatim. Text
    that is absent or null becomes the empty string those not-null columns
    expect rather than an invented placeholder, so a starter post carrying
    only an image still leaves an audit row.

    Returns exactly the columns it redacts, so the route it feeds cannot
    smuggle a sender-chosen key into the row.
    """
    return {
        "post_title": _redact_present_text(post.get("post_title") or ""),
        "post_content": _redact_present_text(post.get("post_content") or ""),
        "attachments": [],
    }


def redact_trigger_context(context: dict) -> dict:
    """Redact the message text a handler run's audit context carries.

    Ids, flags, counts, role lists and timestamps stay, so the run still shows
    which trigger fired, in which channel, for whom. A timer re-fire's payload
    is emptied whole: a script chose what to carry across the wait, and it may
    have carried the message it was reacting to.

    The result shares no mutable value with ``context``: it is a deep copy, so
    a caller that goes on to hand the original to something else can be sure
    the audit copy is fixed at the moment it was taken. The copy is defensive;
    it is not a claim that anything downstream mutates the original.
    """
    return _redact_mapping(copy.deepcopy(context), _is_redacted_handler_key)


def oldest_retained_stream_id(now: datetime) -> str:
    """The Redis stream id below which entries are past the retention window.

    Raises:
        ValueError: if ``now`` is naive, which would read as local time and
            move the cutoff by the machine's UTC offset.
    """
    if now.tzinfo is None:
        raise ValueError("oldest_retained_stream_id requires a timezone-aware datetime")
    return f"{int((now - CONTENT_RETENTION_WINDOW).timestamp() * 1000)}-0"
