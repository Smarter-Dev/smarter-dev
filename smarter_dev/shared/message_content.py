"""What may be written where verbatim Discord message text would otherwise go.

Discord grants the message-content intent on the promise that we do not keep
what people say. So every durable row that would carry message text carries
:data:`MESSAGE_CONTENT_PLACEHOLDER` instead, written that way at row
construction rather than scrubbed later. Empty text stays empty and absent
text stays absent: a placeholder is never invented where nobody said anything.

Verbatim text does survive outside this module — in the agents' own working
history and in the Redis hand-offs that feed the proactive agent and the
handler workers, none of which this module touches. ``docs/data-retention.md`` is the one list of those
places and of what bounds each; this docstring states no number so it cannot
drift from that list. Moderators auditing what was actually said use the
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
import re
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from typing import Any

MESSAGE_CONTENT_PLACEHOLDER: str = "[message content]"

CONTENT_RETENTION_WINDOW: timedelta = timedelta(hours=48)
CONTENT_RETENTION_MILLISECONDS: int = int(CONTENT_RETENTION_WINDOW.total_seconds() * 1000)

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
        "tool-call",
        "tool-search-call",
        "builtin-tool-call",
        "builtin-tool-search-call",
        "builtin-tool-return",
        "builtin-tool-search-return",
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

    Keeps the ids, reply pointers, reactions, flags and timestamp the
    conversation detail view renders around the message body and nothing else,
    so a field added upstream carries a placeholder rather than what somebody
    said.
    """
    return [
        _redact_mapping(message, _is_redacted_chat_key)
        for message in messages or []
    ]


def redact_model_message_parts(messages: list[dict] | None) -> list[dict] | None:
    """Redact the human text inside a serialised pydantic-ai message list.

    Parts the model or its provider authored pass through: the system prompt,
    reply text, tool calls and provider-side tool results. Reasoning and a
    provider's compaction part are model-authored too, but they retell what
    members said, so they are redacted with everything else we send the model
    — prompts, tool returns, retry prompts and any kind this module has never
    seen. Each is redacted down
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
    return f"{int(now.timestamp() * 1000) - CONTENT_RETENTION_MILLISECONDS}-0"


_TRACEBACK_HEADER = "Traceback (most recent call last):"
_TRACEBACK_FRAME_PREFIX = '  File "'
_TRACEBACK_CHAIN_SEPARATORS = frozenset(
    {
        "During handling of the above exception, another exception occurred:",
        "The above exception was the direct cause of the following exception:",
    }
)


def redact_provider_error(
    *, error_message: str, traceback: str, provider_body: str | None
) -> dict[str, str | None]:
    """The text columns of a chat error row, with any provider body removed.

    A provider error body can echo the request prompt, and so a member's
    message, back at us, and nothing can tell which bodies do. The exception
    message and the traceback repeat the body, so when there is one all three
    are redacted: the traceback keeps its header, its ``File`` lines, the
    chaining separators and each exception's type, and drops source lines and
    every exception message. An error with no provider body is kept as sent.
    """
    if provider_body is None:
        return {
            "error_message": error_message,
            "traceback": traceback,
            "provider_body": None,
        }
    kept: list[str] = []
    in_message = False
    for line in traceback.splitlines():
        if line == _TRACEBACK_HEADER or line in _TRACEBACK_CHAIN_SEPARATORS:
            kept.append(line)
            in_message = False
        elif line.startswith(_TRACEBACK_FRAME_PREFIX):
            kept.append(line)
        elif line and not line[0].isspace() and not in_message:
            exception_type = line.split(":", 1)[0]
            kept.append(f"{exception_type}: {MESSAGE_CONTENT_PLACEHOLDER}")
            in_message = True
    return {
        "error_message": _redact_present_text(error_message),
        "traceback": "\n".join(kept) + ("\n" if kept else ""),
        "provider_body": _redact_present_text(provider_body),
    }


_HANDLER_ERROR_LABEL = re.compile(r"[A-Za-z_][\w.]*")


def redact_handler_error(error: str | None) -> str | None:
    """A handler script's error with its message replaced by the placeholder.

    A script that trips over the message it is reacting to puts that text in
    its exception message, and nothing can tell which messages do. The
    leading labels the runtime writes (``compile``, ``runtime`` and the
    exception type) are kept, so the run still says what kind of failure it
    was.
    """
    if error is None or error == "":
        return error
    labels: list[str] = []
    rest = error
    while len(labels) < 2 and ": " in rest:
        label, remainder = rest.split(": ", 1)
        if not _HANDLER_ERROR_LABEL.fullmatch(label):
            break
        labels.append(label)
        rest = remainder
    return ": ".join([*labels, MESSAGE_CONTENT_PLACEHOLDER])
