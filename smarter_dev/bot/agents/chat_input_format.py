"""Render the chat-agent input as an XML transcript.

Replaces ``model_dump_json()`` for the agent's user prompt. The transcript
format makes message boundaries and author attribution visually obvious,
which helps the model avoid grouping multiple users' messages as if one
person said them.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Iterable
from xml.sax.saxutils import escape as xml_escape

from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from smarter_dev.bot.agents.chat_models import (
    Author,
    ChannelInfo,
    FollowupAgentInput,
    GuildEventView,
    InitialAgentInput,
    Me,
    MemoryNote,
    Message,
)
from smarter_dev.bot.agents.url_registry import register_escaped_url
from smarter_dev.shared.model_catalog import MODEL_CATALOG

# Wire model id -> human label, for the per-turn ``<your-model>`` metadata tag.
_MODEL_LABEL_BY_ID: dict[str, str] = {
    catalog_model.model_id: catalog_model.label for catalog_model in MODEL_CATALOG
}

# ``<what-i-did window="...">``: the whole rolling hour at activation, only the
# delta afterwards. The label is part of the prompt — it tells the agent whether
# an action missing from the block means "didn't happen" or "already narrated".
EVENT_WINDOW_FULL = "last-60-min"
EVENT_WINDOW_DELTA = "since-your-last-turn"

# Newest N actions only. A raid can put hundreds of actions in the hour, and a
# wall of them would crowd out the conversation the agent is actually in.
MAX_EVENT_LINES = 12
TRUNCATED_EVENTS_LINE = "(…older actions this hour not shown)"

# Message-shaped events carry a purpose, but a handler message's purpose IS its
# content — clamp it so one long announcement can't eat the block.
MAX_EVENT_SUMMARY_CHARS = 120

# First person, past tense: how each moderation action reads back to the bot.
# Two forms because some actions (a purge, a lone message delete) may not name
# a person at all.
_MOD_ACTION_PHRASES: dict[str, tuple[str, str]] = {
    "timeout": ("timed out {target}", "timed someone out"),
    "untimeout": ("lifted the timeout on {target}", "lifted a timeout"),
    "warn": ("warned {target}", "warned someone"),
    "ban": ("banned {target}", "banned someone"),
    "unban": ("unbanned {target}", "unbanned someone"),
    "kick": ("kicked {target}", "kicked someone"),
    "mute": ("muted {target}", "muted someone"),
    "purge": ("purged {target}'s messages", "purged messages"),
    "delete": ("deleted {target}'s message", "deleted a message"),
}


def _attr(value: str | bool | None) -> str | None:
    """Render an XML attribute value, escaping ``&``, ``<``, ``>``, and quotes.

    Returns None when the value is empty/None so the caller can drop the
    attribute entirely rather than emit ``foo=""``.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "true" if value else None
    s = str(value)
    if not s:
        return None
    return xml_escape(s, {'"': "&quot;"})


def _format_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    # Render every timestamp in UTC with a trailing ``Z`` so the agent sees a
    # uniform format regardless of where the source datetime came from.
    if dt.tzinfo is not None:
        from datetime import timezone

        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _open_tag(name: str, attrs: dict[str, str | bool | None], *, self_close: bool = False) -> str:
    parts: list[str] = [name]
    for k, raw in attrs.items():
        v = _attr(raw)
        if v is None:
            continue
        parts.append(f'{k}="{v}"')
    inside = " ".join(parts)
    return f"<{inside}/>" if self_close else f"<{inside}>"


def _text_tag(name: str, attrs: dict[str, str | bool | None], body: str) -> str:
    """Tag whose body is user text — escape it."""
    return f"{_open_tag(name, attrs)}\n{xml_escape(body)}\n</{name}>"


def _xml_tag(name: str, attrs: dict[str, str | bool | None], inner_xml: str) -> str:
    """Tag whose body is already-rendered XML — do not escape."""
    return f"{_open_tag(name, attrs)}\n{inner_xml}\n</{name}>"


def _empty_tag(name: str, attrs: dict[str, str | bool | None]) -> str:
    return _open_tag(name, attrs, self_close=True)


def _as_utc(moment: datetime) -> datetime:
    """``moment`` in UTC, reading a naive value as already-UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _format_clock(moment: datetime) -> str:
    """Wall-clock UTC, e.g. ``14:02Z`` — the memory blocks' time format."""
    return _as_utc(moment).strftime("%H:%MZ")


def _minutes_ago(moment: datetime, now: datetime) -> int:
    """Whole minutes between ``moment`` and ``now``, never negative."""
    elapsed_seconds = (_as_utc(now) - _as_utc(moment)).total_seconds()
    return max(0, int(elapsed_seconds // 60))


def _compact_duration(seconds: int) -> str:
    """A span the way a person says it: ``10m``, ``1h30m``, ``7d``."""
    remaining = max(0, seconds)
    parts: list[str] = []
    for suffix, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{suffix}")
    # Two units is as much precision as any of this is worth reading aloud.
    return "".join(parts[:2]) or "0s"


def _clamped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _named_person(username: str | None, user_id: str | None) -> str:
    """``mallory (id 8)`` when the id is known, ``mallory`` when it isn't."""
    if not username:
        return "someone"
    return f"{username} (id {user_id})" if user_id else username


def _mod_action_phrase(event: GuildEventView) -> str:
    """First-person past tense for one moderation action."""
    action = (event.action or "").strip().lower()
    with_target, without_target = _MOD_ACTION_PHRASES.get(
        action,
        (f"used {action} on {{target}}", f"used {action}"),
    )
    phrase = (
        with_target.format(target=_named_person(event.target_username, event.target_user_id))
        if event.target_username
        else without_target
    )
    if event.duration_seconds:
        phrase = f"{phrase} for {_compact_duration(event.duration_seconds)}"
    return phrase


def _render_event_line(event: GuildEventView, *, now: datetime) -> str:
    """One ``<what-i-did>`` line: when, what the bot did, and why after the colon.

    The colon is load-bearing grammar — everything after it is the reason the
    action was taken or the purpose of the message that was sent, never a
    message body.
    """
    if event.kind == "bot_dm":
        body = f"I DM'd {_named_person(event.target_username, event.target_user_id)}"
        tail = _clamped(event.summary.strip(), MAX_EVENT_SUMMARY_CHARS)
    elif event.action:
        body = f"I {_mod_action_phrase(event)}"
        if event.channel_name:
            body = f"{body} in #{event.channel_name}"
        if event.source == "manual" and event.moderator_username:
            # A slash command: the bot's account did it, but on someone's behalf,
            # and claiming it as its own idea would be a lie by omission.
            body = f"{body} for @{event.moderator_username}"
        tail = (event.reason or "").strip()
    else:
        body = "I posted"
        if event.channel_name:
            body = f"{body} in #{event.channel_name}"
        tail = _clamped(event.summary.strip(), MAX_EVENT_SUMMARY_CHARS)

    line = f"{_format_clock(event.at)} ({_minutes_ago(event.at, now)}m ago) — {body}"
    if tail:
        line = f"{line}: {tail}"
    return line if line.endswith((".", "!", "?", "…")) else f"{line}."


def _render_event_lines(events: list[GuildEventView], *, now: datetime) -> str:
    """The newest :data:`MAX_EVENT_LINES` events, oldest-first, flagged if cut."""
    ordered = sorted(events, key=lambda event: _as_utc(event.at))
    shown = ordered[-MAX_EVENT_LINES:]
    lines = [_render_event_line(event, now=now) for event in shown]
    if len(ordered) > len(shown):
        return "\n".join([TRUNCATED_EVENTS_LINE, *lines])
    return "\n".join(lines)


def _render_note_line(note: MemoryNote) -> str:
    """One ``<from-today>`` line: when, where, and what the agent kept."""
    where = f" #{note.channel_name}" if note.channel_name else ""
    return f"{_format_clock(note.created_at)}{where} — {note.text.strip()}"


def _render_memory_chunks(
    *,
    now_utc: datetime,
    long_term_memory: str | None,
    long_term_memory_updated_at: datetime | None,
    memory_notes: list[MemoryNote],
    guild_events: list[GuildEventView],
    guild_events_window: str,
) -> list[str]:
    """The three memory blocks, far-to-near, omitting any that is empty.

    An empty memory tag is worse than no tag: it hands the model a rendered
    absence to remark on, and "I don't remember anything about you" is exactly
    the line this whole system exists to avoid.
    """
    chunks: list[str] = []
    blob = (long_term_memory or "").strip()
    if blob:
        chunks.append(
            _text_tag(
                "what-i-remember",
                {
                    "updated": (
                        None
                        if long_term_memory_updated_at is None
                        else _as_utc(long_term_memory_updated_at).strftime("%Y-%m-%d")
                    )
                },
                blob,
            )
        )
    if memory_notes:
        ordered_notes = sorted(memory_notes, key=lambda note: _as_utc(note.created_at))
        chunks.append(
            _text_tag(
                "from-today",
                {},
                "\n".join(_render_note_line(note) for note in ordered_notes),
            )
        )
    if guild_events:
        chunks.append(
            _text_tag(
                "what-i-did",
                {"window": guild_events_window},
                _render_event_lines(guild_events, now=now_utc),
            )
        )
    return chunks


def _render_message(msg: Message, *, me: Me, authors_by_id: dict[str, Author]) -> str:
    is_self = msg.author_id == me.user_id
    attrs: dict[str, str | bool | None] = {
        "id": msg.message_id,
        "sent-utc": _format_utc(msg.sent_at),
    }
    if is_self:
        attrs["self"] = True
    else:
        attrs["user-id"] = msg.author_id
        author = authors_by_id.get(msg.author_id)
        if author is not None:
            attrs["username"] = author.username
            if author.nickname and author.nickname != author.username:
                attrs["nickname"] = author.nickname
            if author.role_names:
                attrs["roles"] = ",".join(author.role_names)
    if msg.reply_to_message_id:
        attrs["reply-to"] = msg.reply_to_message_id
    if msg.reply_to_is_self:
        attrs["reply-to-self"] = True
    elif msg.reply_to_author_id and msg.reply_to_author_id != me.user_id:
        attrs["reply-to-user-id"] = msg.reply_to_author_id
        target = authors_by_id.get(msg.reply_to_author_id)
        if target is not None:
            attrs["reply-to-username"] = target.username
    if msg.reactions:
        attrs["reactions"] = ",".join(msg.reactions)
    if msg.mentions_bot:
        attrs["mentions-bot"] = True

    if not msg.attachments:
        return _text_tag("message", attrs, msg.body)

    # Surface attachment URLs as child tags so the agent can read them with the
    # web_read tool (images, audio, PDFs, etc.). Record each so web_read can
    # recover the exact original when the model echoes back the escaped form.
    for att in msg.attachments:
        register_escaped_url(att.url)
    attachment_tags = "\n".join(
        _empty_tag("attachment", {"kind": att.kind, "url": att.url})
        for att in msg.attachments
    )
    inner = f"{xml_escape(msg.body)}\n{attachment_tags}"
    return _xml_tag("message", attrs, inner)


def _render_messages(messages: Iterable[Message], *, me: Me, authors_by_id: dict[str, Author]) -> str:
    """Render each message separated by a blank line for visual heft."""
    rendered = [_render_message(m, me=me, authors_by_id=authors_by_id) for m in messages]
    return "\n\n".join(rendered)


def render_message_xml(
    msg: Message,
    *,
    me: Me,
    authors: list[Author],
) -> str:
    """Render a single `<message>` tag for use as a ModelRequest payload."""
    authors_by_id = {a.user_id: a for a in authors}
    return _render_message(msg, me=me, authors_by_id=authors_by_id)


def render_metadata_xml(
    *,
    me: Me,
    channel: ChannelInfo,
    now_utc: datetime,
    topic: str | None,
    notes: str | None,
    image_quota: dict | None = None,
    model_name: str | None = None,
    reasoning_level: str | None = None,
    long_term_memory: str | None = None,
    long_term_memory_updated_at: datetime | None = None,
    memory_notes: list[MemoryNote] | None = None,
    guild_events: list[GuildEventView] | None = None,
    guild_events_window: str = EVENT_WINDOW_FULL,
) -> str:
    """Render the per-turn metadata block (me / channel / now / memory / topic)."""
    return _render_metadata(
        me=me,
        channel=channel,
        now_utc=now_utc,
        topic=topic,
        notes=notes,
        image_quota=image_quota,
        model_name=model_name,
        reasoning_level=reasoning_level,
        long_term_memory=long_term_memory,
        long_term_memory_updated_at=long_term_memory_updated_at,
        memory_notes=memory_notes,
        guild_events=guild_events,
        guild_events_window=guild_events_window,
    )


def _memory_arguments(
    agent_input: InitialAgentInput | FollowupAgentInput,
) -> dict[str, Any]:
    """The turn's memory kwargs for ``render_metadata_xml``, per the send policy.

    The blob and today's notes ride the initial turn only — they stay in the
    message history and prompt-cache from there, and re-sending them every turn
    would pay for them again while inviting the model to notice them again. The
    exception is a blob explicitly set on a follow-up, which is the engine
    re-emitting it after compaction drained the history that held it.
    """
    if isinstance(agent_input, InitialAgentInput):
        return {
            "long_term_memory": agent_input.long_term_memory,
            "long_term_memory_updated_at": agent_input.long_term_memory_updated_at,
            "memory_notes": agent_input.memory_notes,
            "guild_events": agent_input.guild_events,
            "guild_events_window": EVENT_WINDOW_FULL,
        }
    return {
        "long_term_memory": agent_input.long_term_memory,
        "long_term_memory_updated_at": agent_input.long_term_memory_updated_at,
        "memory_notes": None,
        "guild_events": agent_input.new_guild_events,
        "guild_events_window": EVENT_WINDOW_DELTA,
    }


def build_agent_call(
    agent_input: InitialAgentInput | FollowupAgentInput,
    prior_history: list[ModelMessage],
    image_quota: dict | None = None,
    model_name: str | None = None,
    reasoning_level: str | None = None,
) -> tuple[str, list[ModelMessage]]:
    """Convert a turn input into (user_prompt, message_history) for ``agent.run``.

    Each Discord message except the latest becomes its own
    ``ModelRequest(UserPromptPart(...))`` appended to ``prior_history``. The
    latest message becomes the ``user_prompt`` for this run, prefixed by a
    metadata block so per-turn context (now, topic, notes) refreshes.

    Why split: when several users post in quick succession, the model now sees
    each message as a distinct conversational input rather than one
    concatenated block, which keeps speaker attribution clean.
    """
    authors = list(agent_input.authors)
    me = agent_input.me
    memory_arguments = _memory_arguments(agent_input)

    if isinstance(agent_input, InitialAgentInput):
        messages = list(agent_input.channel_history) + [
            agent_input.activation_message
        ]
    else:
        messages = list(agent_input.new_messages)

    history = list(prior_history)
    if not messages:
        # Nothing to react to; metadata-only prompt. Shouldn't happen in
        # practice (the engine fires only when there's at least one new
        # message), but degrade safely.
        return (
            render_metadata_xml(
                me=me,
                channel=agent_input.channel,
                now_utc=agent_input.now_utc,
                topic=agent_input.topic,
                notes=agent_input.notes,
                image_quota=image_quota,
                model_name=model_name,
                reasoning_level=reasoning_level,
                **memory_arguments,
            ),
            history,
        )

    for earlier in messages[:-1]:
        history.append(
            ModelRequest(
                parts=[
                    UserPromptPart(
                        content=render_message_xml(earlier, me=me, authors=authors)
                    )
                ]
            )
        )

    latest = messages[-1]
    metadata = render_metadata_xml(
        me=me,
        channel=agent_input.channel,
        now_utc=agent_input.now_utc,
        topic=agent_input.topic,
        notes=agent_input.notes,
        image_quota=image_quota,
        model_name=model_name,
        reasoning_level=reasoning_level,
        **memory_arguments,
    )
    latest_xml = render_message_xml(latest, me=me, authors=authors)
    user_prompt = f"{metadata}\n\n{latest_xml}"
    return user_prompt, history


def _render_metadata(
    *,
    me: Me,
    channel: ChannelInfo,
    now_utc: datetime,
    topic: str | None,
    notes: str | None,
    image_quota: dict | None = None,
    model_name: str | None = None,
    reasoning_level: str | None = None,
    long_term_memory: str | None = None,
    long_term_memory_updated_at: datetime | None = None,
    memory_notes: list[MemoryNote] | None = None,
    guild_events: list[GuildEventView] | None = None,
    guild_events_window: str = EVENT_WINDOW_FULL,
) -> str:
    chunks: list[str] = []
    chunks.append(_empty_tag("me", {"user-id": me.user_id, "username": me.username}))
    chunks.append(
        _empty_tag(
            "channel",
            {
                "id": channel.channel_id,
                "name": channel.name,
                "description": channel.description,
            },
        )
    )
    chunks.append(_empty_tag("now", {"utc": _format_utc(now_utc)}))
    if model_name:
        # Which model (and effective reasoning level) is answering this turn.
        # Rendered per turn — not in the system prompt — because pydantic-ai
        # only applies the system prompt when history is empty, and the model
        # can change mid-engagement (override edits, the temporary default).
        chunks.append(
            _empty_tag(
                "your-model",
                {
                    "id": model_name,
                    "name": _MODEL_LABEL_BY_ID.get(model_name),
                    "reasoning-level": reasoning_level,
                },
            )
        )
    if image_quota is not None:
        # How many technical images can still be generated this hour, and when
        # the window resets — so the agent knows up front whether it can draw.
        remaining = image_quota.get("remaining")
        limit = image_quota.get("limit")
        chunks.append(
            _empty_tag(
                "image-quota",
                {
                    "remaining": None if remaining is None else str(remaining),
                    "limit": None if limit is None else str(limit),
                    "resets-utc": image_quota.get("resets_at"),
                },
            )
        )
    # Memory sits here on purpose: the zoom runs far to near — everything the
    # bot carries about this guild, then today, then the last hour, and only
    # then the per-channel scratchpad that ``<topic>``/``<notes>`` hold.
    chunks.extend(
        _render_memory_chunks(
            now_utc=now_utc,
            long_term_memory=long_term_memory,
            long_term_memory_updated_at=long_term_memory_updated_at,
            memory_notes=list(memory_notes or []),
            guild_events=list(guild_events or []),
            guild_events_window=guild_events_window,
        )
    )
    if topic:
        chunks.append(_text_tag("topic", {}, topic))
    if notes:
        chunks.append(_text_tag("notes", {}, notes))
    return "\n".join(chunks)


def render_input_xml(agent_input: InitialAgentInput | FollowupAgentInput) -> str:
    """Serialise an agent turn input as a flat XML transcript.

    No outer ``<turn>`` wrapper — the LLM's own message history tells it
    whether this is the first turn or a continuation. The body is the
    channel context (``<me>``, ``<channel>``, ``<now>``, optional
    ``<topic>`` and ``<notes>``) followed by a flat sequence of
    ``<message>`` tags in chronological order. The agent infers what's
    newly arrived since the last turn by comparing ``sent-utc`` against
    timestamps it already saw in history.
    """
    authors_by_id = {a.user_id: a for a in agent_input.authors}
    metadata = _render_metadata(
        me=agent_input.me,
        channel=agent_input.channel,
        now_utc=agent_input.now_utc,
        topic=agent_input.topic,
        notes=agent_input.notes,
    )

    if isinstance(agent_input, InitialAgentInput):
        messages = list(agent_input.channel_history) + [agent_input.activation_message]
    else:
        messages = list(agent_input.new_messages)

    messages_block = _render_messages(
        messages, me=agent_input.me, authors_by_id=authors_by_id
    )
    if messages_block:
        return f"{metadata}\n\n{messages_block}"
    return metadata
