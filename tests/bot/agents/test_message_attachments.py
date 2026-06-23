"""Tests for surfacing message attachment URLs to the chat agent.

Covers ``_build_attachments`` (extraction + the bot's-own-audio block) and
that ``render_message_xml`` emits ``<attachment>`` tags the agent can read.
"""

from __future__ import annotations

from types import SimpleNamespace

from smarter_dev.bot.agents.chat_context import _build_attachments
from smarter_dev.bot.agents.chat_input_format import render_message_xml
from smarter_dev.bot.agents.chat_models import Author, Me, Message, MessageAttachment


def _att(url: str, media_type=None, filename: str = "") -> SimpleNamespace:
    return SimpleNamespace(url=url, media_type=media_type, filename=filename)


def _msg(attachments: list) -> SimpleNamespace:
    return SimpleNamespace(attachments=attachments)


def test_build_attachments_extracts_url_and_type():
    msg = _msg([_att("https://x/pic.png", "image/png", "pic.png")])
    got = _build_attachments(msg, is_self=False)
    assert len(got) == 1
    assert got[0].url == "https://x/pic.png"
    assert got[0].kind == "image"


def test_build_attachments_hides_all_bot_own_attachments():
    # Every attachment on the bot's own messages is hidden — voice notes,
    # images, anything — so it never tries to read files it posted.
    msg = _msg(
        [
            _att("https://x/voice.ogg", "audio/ogg", "voice-message.ogg"),
            _att("https://x/pic.png", "image/png", "pic.png"),
        ]
    )
    assert _build_attachments(msg, is_self=True) == []
    # The same attachments from another user are surfaced normally.
    other = _build_attachments(msg, is_self=False)
    assert [a.kind for a in other] == ["audio", "image"]


def test_render_message_emits_attachment_tags_with_escaped_url():
    me = Me(user_id="999", username="bot")
    msg = Message(
        message_id="1",
        author_id="2",
        body="look at this",
        attachments=[
            MessageAttachment(url="https://x/pic.png?a=1&b=2", media_type="image/png")
        ],
    )
    xml = render_message_xml(msg, me=me, authors=[Author(user_id="2", username="alice")])
    assert '<attachment kind="image"' in xml
    assert "pic.png?a=1&amp;b=2" in xml  # & escaped inside the attribute
    assert "look at this" in xml
