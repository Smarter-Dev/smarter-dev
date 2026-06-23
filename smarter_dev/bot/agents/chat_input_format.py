"""Render the chat-agent input as an XML transcript.

Replaces ``model_dump_json()`` for the agent's user prompt. The transcript
format makes message boundaries and author attribution visually obvious,
which helps the model avoid grouping multiple users' messages as if one
person said them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable
from xml.sax.saxutils import escape as xml_escape

from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from smarter_dev.bot.agents.chat_models import (
    Author,
    ChannelInfo,
    FollowupAgentInput,
    InitialAgentInput,
    Me,
    Message,
)
from smarter_dev.bot.agents.url_registry import register_escaped_url


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
) -> str:
    """Render the per-turn metadata block (me / channel / now / topic / notes)."""
    return _render_metadata(
        me=me,
        channel=channel,
        now_utc=now_utc,
        topic=topic,
        notes=notes,
    )


def build_agent_call(
    agent_input: InitialAgentInput | FollowupAgentInput,
    prior_history: list[ModelMessage],
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
