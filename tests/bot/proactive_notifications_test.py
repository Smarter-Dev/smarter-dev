"""Tests for the proactive bot's notification model (pure parts)."""

from __future__ import annotations

import asyncio
from datetime import UTC
from datetime import datetime

from smarter_dev.bot.proactive import notifications
from smarter_dev.bot.proactive.types import ChannelMessage

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _message(message_id: str = "555", *, mention: bool = False,
             reply_to: str | None = None) -> ChannelMessage:
    return ChannelMessage(
        id=message_id,
        timestamp=NOW,
        author_id="901",
        author_name="alice",
        author_display="ally",
        is_bot=False,
        content="hey bot, what's a coroutine?",
        reply_to_id=reply_to,
        mention_user_ids=("999",) if mention else (),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=19 if reply_to else 0,
        roles=("Regular", "Helper"),
    )


def test_mention_notification_carries_verbatim_id_and_user_metadata():
    notification = notifications.mention_notification(
        _message(mention=True), channel_id="123", channel_name="python-help"
    )
    assert notification.kind == "mention"
    assert notification.channel_id == "123"
    assert notification.channel_name == "python-help"
    assert notification.wakes is True
    assert notification.message_ids == ("555",)
    for expected in ("555", "ally", "alice", "901", "what's a coroutine?",
                     "Regular, Helper"):
        assert expected in notification.body


def test_reply_notification_names_the_replied_to_bot_message():
    replied = ChannelMessage(
        id="500", timestamp=NOW, author_id="999", author_name="smarter-bot",
        author_display="smarter-bot", is_bot=True, content="earlier answer",
        reply_to_id=None, mention_user_ids=(), mention_everyone=False,
        attachment_count=0, sticker_count=0, message_type=0,
    )
    notification = notifications.reply_notification(
        _message(reply_to="500"), replied_to=replied,
        channel_id="123", channel_name="python-help",
    )
    assert notification.kind == "reply_to_bot"
    assert notification.channel_id == "123"
    assert notification.channel_name == "python-help"
    assert notification.wakes is True
    assert "earlier answer" in notification.body
    assert "500" in notification.body


def test_watcher_summary_notification_wakes_only_on_wake_decision():
    waking = notifications.watcher_summary_notification(
        summary="alice asked the room about docker caching",
        message_ids=["1", "2"], wake=True, created_at=NOW,
        channel_id="123", channel_name="python-help",
    )
    quiet = notifications.watcher_summary_notification(
        summary="two people chatting about keyboards",
        message_ids=["3"], wake=False, created_at=NOW,
        channel_id="123", channel_name="python-help",
    )
    assert waking.wakes is True and quiet.wakes is False
    assert waking.channel_id == "123"
    assert waking.channel_name == "python-help"
    assert quiet.kind == "watcher_summary"
    assert "keyboards" in quiet.body
    assert quiet.message_ids == ("3",)


def test_new_messages_notification_groups_ids_and_summary():
    notification = notifications.new_messages_notification(
        summary="alice and bob are comparing parser benchmarks",
        message_ids=["601", "602", "603"],
        created_at=NOW,
        channel_id="123",
        channel_name="python-help",
    )
    assert notification.kind == "new_messages"
    assert notification.channel_id == "123"
    assert notification.channel_name == "python-help"
    assert notification.wakes is False
    assert notification.message_ids == ("601", "602", "603")
    assert "3 new messages" in notification.body
    assert "parser benchmarks" in notification.body


def test_mode_change_and_expiry_notifications_never_wake():
    mode = notifications.mode_change_notification(
        mode="active", cause="alice mentioned the bot",
        until=NOW, created_at=NOW,
        channel_id="123", channel_name="python-help",
    )
    expiry = notifications.instruction_expired_notification(
        instruction_id="w1", text="watch for tech news", created_at=NOW,
        channel_id="123", channel_name="python-help",
    )
    assert mode.wakes is False and expiry.wakes is False
    assert mode.channel_id == expiry.channel_id == "123"
    assert mode.channel_name == expiry.channel_name == "python-help"
    assert "active" in mode.body
    assert "w1" in expiry.body and "tech news" in expiry.body


def test_recovery_notification_carries_channel_provenance():
    notification = notifications.recovery_notification(
        missed_count=4,
        created_at=NOW,
        channel_id="123",
        channel_name="python-help",
    )

    assert notification.channel_id == "123"
    assert notification.channel_name == "python-help"


def test_queue_caps_and_reports_drops():
    queue = notifications.NotificationQueue(limit=3)
    for n in range(5):
        queue.push(notifications.mode_change_notification(
            mode="passive", cause=f"cause {n}", until=None, created_at=NOW,
            channel_id="123", channel_name="python-help",
        ))
    items, dropped = queue.drain()
    assert len(items) == 3
    assert dropped == 2
    assert "cause 4" in items[-1].body  # newest kept, oldest dropped
    # Drained means empty.
    assert queue.drain() == ([], 0)


async def test_waking_push_sets_queue_wake_event():
    queue = notifications.NotificationQueue()

    queue.push(notifications.mention_notification(
        _message(mention=True), channel_id="123", channel_name="python-help"
    ))

    assert queue._wake_event.is_set()
    await asyncio.wait_for(queue.wait_for_wake(), timeout=0.1)


def test_non_waking_push_does_not_set_queue_wake_event():
    queue = notifications.NotificationQueue()

    queue.push(notifications.mode_change_notification(
        mode="passive", cause="quiet update", until=None, created_at=NOW,
        channel_id="123", channel_name="python-help",
    ))

    assert not queue._wake_event.is_set()


def test_queue_drain_clears_wake_event():
    queue = notifications.NotificationQueue()
    queue.push(notifications.mention_notification(
        _message(mention=True), channel_id="123", channel_name="python-help"
    ))

    queue.drain()

    assert not queue._wake_event.is_set()


async def test_waking_push_during_in_flight_consumer_signals_next_loop():
    queue = notifications.NotificationQueue()
    queue.push(notifications.mention_notification(
        _message(mention=True), channel_id="123", channel_name="python-help"
    ))
    await queue.wait_for_wake()
    queue.drain()

    queue.push(notifications.mention_notification(
        _message(message_id="556", mention=True),
        channel_id="123",
        channel_name="python-help",
    ))

    await asyncio.wait_for(queue.wait_for_wake(), timeout=0.1)
    assert queue._wake_event.is_set()


def test_render_notifications_is_ordered_and_notes_drops():
    items = [
        notifications.mode_change_notification(
            mode="active", cause="mention", until=None, created_at=NOW,
            channel_id="123", channel_name="python-help",
        ),
        notifications.mention_notification(
            _message(mention=True),
            channel_id="123",
            channel_name="python-help",
        ),
    ]
    rendered = notifications.render_notifications(items, dropped=2)
    assert rendered.index("mode_change") < rendered.index("mention")
    assert "2 older notifications were dropped" in rendered
    assert "NOTIFICATIONS" in rendered


def test_render_notifications_prefixes_channel_name():
    item = notifications.mode_change_notification(
        mode="active", cause="mention", until=None, created_at=NOW,
        channel_id="123", channel_name="python-help",
    )

    rendered = notifications.render_notifications([item])

    assert "[#python-help] [2026-08-19T12:00:00Z, mode_change]" in rendered


def test_render_notifications_falls_back_to_channel_id():
    item = notifications.mode_change_notification(
        mode="active", cause="mention", until=None, created_at=NOW,
        channel_id="123",
        channel_name="",
    )

    rendered = notifications.render_notifications([item])

    assert "[#123] [2026-08-19T12:00:00Z, mode_change]" in rendered


def test_render_notifications_without_provenance_omits_channel_prefix():
    item = notifications.mode_change_notification(
        mode="active", cause="mention", until=None, created_at=NOW,
        channel_id="", channel_name="",
    )

    rendered = notifications.render_notifications([item])

    assert "[#]" not in rendered
    assert "[2026-08-19T12:00:00Z, mode_change]" in rendered
