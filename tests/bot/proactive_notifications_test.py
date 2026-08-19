"""Tests for the proactive bot's notification model (pure parts)."""

from __future__ import annotations

from datetime import UTC, datetime

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
    )


def test_mention_notification_carries_verbatim_id_and_user_metadata():
    notification = notifications.mention_notification(_message(mention=True))
    assert notification.kind == "mention"
    assert notification.wakes is True
    assert notification.message_ids == ("555",)
    for expected in ("555", "ally", "alice", "901", "what's a coroutine?"):
        assert expected in notification.body


def test_reply_notification_names_the_replied_to_bot_message():
    replied = ChannelMessage(
        id="500", timestamp=NOW, author_id="999", author_name="smarter-bot",
        author_display="smarter-bot", is_bot=True, content="earlier answer",
        reply_to_id=None, mention_user_ids=(), mention_everyone=False,
        attachment_count=0, sticker_count=0, message_type=0,
    )
    notification = notifications.reply_notification(
        _message(reply_to="500"), replied_to=replied
    )
    assert notification.kind == "reply_to_bot"
    assert notification.wakes is True
    assert "earlier answer" in notification.body
    assert "500" in notification.body


def test_watcher_summary_notification_wakes_only_on_wake_decision():
    waking = notifications.watcher_summary_notification(
        summary="alice asked the room about docker caching",
        message_ids=["1", "2"], wake=True, created_at=NOW,
    )
    quiet = notifications.watcher_summary_notification(
        summary="two people chatting about keyboards",
        message_ids=["3"], wake=False, created_at=NOW,
    )
    assert waking.wakes is True and quiet.wakes is False
    assert quiet.kind == "watcher_summary"
    assert "keyboards" in quiet.body
    assert quiet.message_ids == ("3",)


def test_mode_change_and_expiry_notifications_never_wake():
    mode = notifications.mode_change_notification(
        mode="active", cause="alice mentioned the bot",
        until=NOW, created_at=NOW,
    )
    expiry = notifications.instruction_expired_notification(
        instruction_id="w1", text="watch for tech news", created_at=NOW,
    )
    assert mode.wakes is False and expiry.wakes is False
    assert "active" in mode.body
    assert "w1" in expiry.body and "tech news" in expiry.body


def test_queue_caps_and_reports_drops():
    queue = notifications.NotificationQueue(limit=3)
    for n in range(5):
        queue.push(notifications.mode_change_notification(
            mode="passive", cause=f"cause {n}", until=None, created_at=NOW,
        ))
    items, dropped = queue.drain()
    assert len(items) == 3
    assert dropped == 2
    assert "cause 4" in items[-1].body  # newest kept, oldest dropped
    # Drained means empty.
    assert queue.drain() == ([], 0)


def test_render_notifications_is_ordered_and_notes_drops():
    items = [
        notifications.mode_change_notification(
            mode="active", cause="mention", until=None, created_at=NOW,
        ),
        notifications.mention_notification(_message(mention=True)),
    ]
    rendered = notifications.render_notifications(items, dropped=2)
    assert rendered.index("mode_change") < rendered.index("mention")
    assert "2 older notifications were dropped" in rendered
    assert "NOTIFICATIONS" in rendered
