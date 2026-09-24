"""Attachments stay visible through the proactive producer (#20).

Each test drives a hikari-shaped message through the real conversion and
asserts on the text the models actually receive: transcript lines (watcher,
Jev and agent tools) and the serialized queue envelope the external agent
reads.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

from smarter_dev.bot.plugins.proactive import channel_message_from_hikari
from smarter_dev.bot.proactive import notifications
from smarter_dev.bot.proactive.contracts import NotificationEnvelope
from smarter_dev.bot.proactive.environment import ChannelEnvironment
from smarter_dev.bot.proactive.types import ChannelMessage
from smarter_dev.bot.proactive.watcher import build_jev_watcher_material

NOW = datetime(2026, 9, 24, 19, 30, tzinfo=UTC)
SIGNED_URL = (
    "https://cdn.discordapp.com/attachments/111/222/trace.png"
    "?ex=66f3a1b2&is=66f25032&hm=abc123def456&"
)


def _hikari_message(*, content: str = "", attachments=(), referenced=None):
    author = SimpleNamespace(
        id=901, username="alice", global_name="Ally", is_bot=False
    )
    return SimpleNamespace(
        id=555,
        author=author,
        member=None,
        created_at=NOW,
        content=content,
        referenced_message=referenced,
        user_mentions_ids=[999],
        mentions_everyone=False,
        attachments=list(attachments),
        stickers=[],
        type=0,
    )


def _attachment(url=SIGNED_URL, filename="trace.png", media_type="image/png",
                size=48_213):
    return SimpleNamespace(
        url=url, filename=filename, media_type=media_type, size=size
    )


def test_attachment_only_message_renders_url_type_and_size_in_transcript():
    message = channel_message_from_hikari(_hikari_message(attachments=[_attachment()]))
    env = ChannelEnvironment(visible=[message], bot_user_id="999")

    line = env.render([message])

    assert line.endswith(
        f": [attachment: trace.png, image/png, 47 KB, url={SIGNED_URL}]"
    )


def test_watcher_transcript_names_the_file_without_the_signed_url():
    message = channel_message_from_hikari(_hikari_message(attachments=[_attachment()]))
    env = ChannelEnvironment(visible=[message], bot_user_id="999")

    line = env.render([message], attachment_urls=False)
    material = build_jev_watcher_material(context_transcript="", new_transcript=line)

    assert line.endswith(": [attachment: trace.png, image/png, 47 KB]")
    assert "cdn.discordapp.com" not in material


def test_every_attachment_on_a_message_is_listed_after_its_text():
    message = channel_message_from_hikari(
        _hikari_message(
            content="logs attached",
            attachments=[
                _attachment(),
                _attachment(
                    url="https://cdn.discordapp.com/attachments/111/223/app.log?ex=1&is=2&hm=3&",
                    filename="app.log",
                    media_type="text/plain; charset=utf-8",
                    size=900,
                ),
            ],
        )
    )
    line = ChannelEnvironment(visible=[message], bot_user_id="999").render([message])

    assert ": logs attached [attachment: trace.png," in line
    assert "[attachment: app.log, text/plain; charset=utf-8, 900 B, url=" in line
    assert message.attachment_count == 2


def test_record_round_trip_keeps_attachments_and_old_records_still_load():
    message = channel_message_from_hikari(_hikari_message(attachments=[_attachment()]))
    assert ChannelMessage.from_record(json.loads(json.dumps(message.to_record()))) == message

    legacy = message.to_record()
    del legacy["attachments"]
    legacy["attachment_count"] = 2
    restored = ChannelMessage.from_record(legacy)
    assert restored.attachments == ()
    line = ChannelEnvironment(visible=[restored], bot_user_id="999").render([restored])
    assert line.endswith(": [2 attachments, no details]")


def test_mention_envelope_body_carries_the_signed_attachment_url():
    message = channel_message_from_hikari(_hikari_message(attachments=[_attachment()]))
    notification = notifications.mention_notification(
        message, channel_id="123", channel_name="help"
    )

    wire = json.loads(
        NotificationEnvelope.from_notification(notification, guild_id="42").model_dump_json()
    )

    assert wire["body"].endswith(
        f"\n> [attachment: trace.png, image/png, 47 KB, url={SIGNED_URL}]"
    )


def test_reply_envelope_body_carries_the_attachment():
    bot_message = channel_message_from_hikari(
        SimpleNamespace(
            **{
                **vars(_hikari_message(content="try this")),
                "id": 444,
                "author": SimpleNamespace(
                    id=999, username="bot", global_name=None, is_bot=True
                ),
            }
        )
    )
    reply = channel_message_from_hikari(
        _hikari_message(content="still broken", attachments=[_attachment()])
    )
    notification = notifications.reply_notification(
        reply, bot_message, channel_id="123", channel_name="help"
    )

    wire = json.loads(
        NotificationEnvelope.from_notification(notification, guild_id="42").model_dump_json()
    )

    assert wire["body"].endswith(
        f"> still broken [attachment: trace.png, image/png, 47 KB, url={SIGNED_URL}]"
    )


def test_message_without_attachments_renders_unchanged():
    message = channel_message_from_hikari(_hikari_message(content="hello"))
    line = ChannelEnvironment(visible=[message], bot_user_id="999").render([message])
    assert line.endswith("·Ally: hello")
