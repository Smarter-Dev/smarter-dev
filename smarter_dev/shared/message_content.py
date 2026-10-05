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
:data:`_HELP_PRESERVED_KEYS`, a pydantic-ai part keeps
:data:`_REDACTED_PART_PRESERVED_FIELDS` whoever wrote it, and a chat turn's
decision keeps :data:`_TURN_DECISION_PRESERVED_KEYS`. A forum post goes further
and is built rather than filtered: :func:`redact_forum_post` returns the three
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
import traceback
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from typing import Any

from smarter_dev.shared.retention_policy import IN_FLIGHT_MAX

MESSAGE_CONTENT_PLACEHOLDER: str = "[message content]"

# How old message text may get before the bot's trims and the hourly retention
# sweep (smarter_dev/web/retention.py) remove it. One hour inside the in-flight
# backstop, because the sweep runs at the top of each hour: a row due just after
# a run is cleared by the next, so nothing outlives IN_FLIGHT_MAX.
CONTENT_RETENTION_WINDOW: timedelta = IN_FLIGHT_MAX - timedelta(hours=1)
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

_TURN_DECISION_PRESERVED_KEYS = frozenset(
    {"rankings", "response_language", "response", "continue_watching"}
)

_TURN_RESPONSE_PRESERVED_KEYS = frozenset(
    {"target_message_id", "reply_directly", "not_cs_topic_brief_answer"}
)

_TURN_RANKING_PRESERVED_KEYS = frozenset({"message_id", "score"})

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
    """Redact all the text inside a serialised pydantic-ai message list.

    Every part is redacted down to its bookkeeping: kind, tool name and call
    id, tool kind, timestamp and outcome. That covers what members said — the
    prompts, tool returns and retry prompts — and what the model wrote too:
    its reply text, reasoning and tool-call arguments can all quote a member,
    and a web search's query is often lifted straight from a message. Content,
    arguments, metadata and any field added later are emptied, and a field
    that was absent stays absent. Message-level fields (kind, usage, model
    name, timestamps) carry no text and stay.
    """
    if messages is None:
        return None
    return [
        message | {"parts": [_redact_part(part) for part in message["parts"]]}
        if "parts" in message
        else dict(message)
        for message in messages
    ]


def redact_turn_decision(output: dict) -> dict:
    """Redact the text out of a chat turn's structured decision.

    Everything the model wrote goes: the reply, its voice summary and voice
    instruction, the running topic and notes, and each ranking's reasoning.
    What stays is what the decision *was* — which messages it scored and how
    high, whether it replied and to which message, in which language, and
    whether it kept watching. A key the decision gains later is redacted.
    """
    redacted = _redact_mapping(
        output, lambda key: key not in _TURN_DECISION_PRESERVED_KEYS
    )
    if isinstance(output.get("rankings"), list):
        redacted["rankings"] = [
            _redact_mapping(
                ranking, lambda key: key not in _TURN_RANKING_PRESERVED_KEYS
            )
            for ranking in output["rankings"]
            if isinstance(ranking, dict)
        ]
    if isinstance(output.get("response"), dict):
        redacted["response"] = _redact_mapping(
            output["response"], lambda key: key not in _TURN_RESPONSE_PRESERVED_KEYS
        )
    return redacted


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
_CAUSE_SEPARATOR = "\nThe above exception was the direct cause of the following exception:\n"
_CONTEXT_SEPARATOR = "\nDuring handling of the above exception, another exception occurred:\n"


def exception_type_name(error: BaseException) -> str:
    """``module.Qualname`` of an exception, bare for builtins."""
    cls = type(error)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def stored_error_type(value: str | None) -> str | None:
    """``value`` if it is an exception type name, otherwise the placeholder.

    For a column that should hold :func:`exception_type_name` but is filled by
    another process: a sender still on the old ``Type: message`` format, or
    any other text, stores the placeholder rather than what it sent.
    """
    if value is None:
        return None
    if value and all(part.isidentifier() for part in value.split(".")):
        return value
    return _redact_present_text(value)


def exception_trace(error: BaseException) -> str:
    """A traceback of ``error`` and every exception chained to it, without text.

    Any exception message can carry a member's words: a provider body that
    echoes the prompt, a validation error quoting its input, a Discord API
    error, a script that trips over the message it reacts to. So the stored
    trace is built from the exception objects, never from their text: each
    exception's type and stack frames (file, line, function), the chaining
    between them, and the placeholder where each message was. No source lines,
    no notes and no message is ever read.
    """
    chain: list[tuple[BaseException, str]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    separator = ""
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append((current, separator))
        if current.__cause__ is not None:
            current, separator = current.__cause__, _CAUSE_SEPARATOR
        elif current.__context__ is not None and not current.__suppress_context__:
            current, separator = current.__context__, _CONTEXT_SEPARATOR
        else:
            current = None
    # chain[i]'s separator says how chain[i] led to chain[i - 1]; printed
    # oldest first, as Python does, it sits between them.
    separator_before = [chain[i + 1][1] for i in range(len(chain) - 1)]
    lines: list[str] = []
    for index in range(len(chain) - 1, -1, -1):
        exception, _ = chain[index]
        if exception.__traceback__ is not None:
            lines.append(_TRACEBACK_HEADER)
            lines.extend(
                f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}'
                for frame in traceback.extract_tb(exception.__traceback__)
            )
        lines.append(f"{exception_type_name(exception)}: {MESSAGE_CONTENT_PLACEHOLDER}")
        if index > 0:
            lines.append(separator_before[index - 1])
    return "\n".join(lines) + "\n"
