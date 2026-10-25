"""The activity log skips edits and deletes of bot and webhook messages.

Disboard, the bump tracker's cleanup, the bot's own notices and webhook posts
were all logged on every edit and delete. Member messages log as before.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from smarter_dev.bot import audit_logger

GUILD_ID = 555
CHANNEL_ID = 777


def _author(is_bot=False):
    return SimpleNamespace(
        id=111, username="poster", mention="<@111>", avatar_url=None, is_bot=is_bot
    )


def _message(message_id, content, *, is_bot=False, webhook_id=None):
    return SimpleNamespace(
        id=message_id, content=content, author=_author(is_bot), webhook_id=webhook_id
    )


def _edit(old, new):
    return SimpleNamespace(
        guild_id=GUILD_ID, channel_id=CHANNEL_ID, message=new, old_message=old
    )


def _delete(message_id, old=None):
    return SimpleNamespace(
        guild_id=GUILD_ID, channel_id=CHANNEL_ID, message_id=message_id, old_message=old
    )


@pytest.fixture
def sent(monkeypatch):
    embeds = []

    async def should_log(*args, **kwargs):
        return True

    async def send(bot, guild_id, embed):
        embeds.append(embed)
        return True

    monkeypatch.setattr(audit_logger, "should_log_event", should_log)
    monkeypatch.setattr(audit_logger, "send_audit_log", send)
    monkeypatch.setattr(
        audit_logger,
        "_automated_message_ids",
        type(audit_logger._automated_message_ids)(),
    )
    return embeds


AUTOMATED = [
    pytest.param({"is_bot": True}, id="bot"),
    pytest.param({"webhook_id": 999}, id="webhook"),
]


@pytest.mark.parametrize("kind", AUTOMATED)
async def test_automated_edit_is_not_logged(sent, kind):
    await audit_logger.log_message_edit(
        None, _edit(_message(1, "old", **kind), _message(1, "new", **kind))
    )
    await audit_logger.log_message_edit(None, _edit(None, _message(2, "new", **kind)))

    assert sent == []


@pytest.mark.parametrize("kind", AUTOMATED)
async def test_automated_cached_delete_is_not_logged(sent, kind):
    await audit_logger.log_message_delete(None, _delete(1, _message(1, "bump", **kind)))

    assert sent == []


@pytest.mark.parametrize("kind", AUTOMATED)
async def test_automated_uncached_delete_seen_on_create_is_not_logged(sent, kind):
    audit_logger.remember_message(_message(3, "bump", **kind))

    await audit_logger.log_message_delete(None, _delete(3))

    assert sent == []


async def test_member_edit_and_delete_still_log(sent):
    audit_logger.remember_message(_message(4, "hello"))

    await audit_logger.log_message_edit(
        None, _edit(_message(4, "hello"), _message(4, "hi"))
    )
    await audit_logger.log_message_delete(None, _delete(4, _message(4, "hi")))
    await audit_logger.log_message_delete(None, _delete(5))

    assert [embed.title for embed in sent] == [
        "Message Edited",
        "Message Deleted",
        "Message Deleted",
    ]


def test_memory_is_bounded(sent, monkeypatch):
    monkeypatch.setattr(audit_logger, "AUTOMATED_MESSAGE_MEMORY", 3)

    for message_id in range(10):
        audit_logger.remember_message(_message(message_id, "x", is_bot=True))

    assert list(audit_logger._automated_message_ids) == [7, 8, 9]
