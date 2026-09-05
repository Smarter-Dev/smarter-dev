"""Agent-visible timestamps preserve dates, UTC offsets, and precision."""

from datetime import datetime

import pytest

from smarter_dev.bot.proactive.notifications import Notification
from smarter_dev.bot.proactive.notifications import render_notifications
from smarter_dev.bot.proactive.timestamps import utc_timestamp
from smarter_dev.bot.proactive.transcript import render_transcript_line


@pytest.mark.parametrize("source", [
    "2026-09-02T01:02:03.456789+05:30",
    "2026-09-01T19:32:03.456789",
    "2026-09-01T19:32:03.456789Z",
])
def test_timestamp_normalizes_to_utc(source):
    assert utc_timestamp(datetime.fromisoformat(source)) == "2026-09-01T19:32:03.456789Z"


def test_transcript_includes_message_sent_time():
    record = {
        "id": "123", "timestamp": "2026-09-02T01:02:03+05:30",
        "author_id": "456", "author_display": "Alice", "is_bot": False,
        "reply_to_id": None, "content": "hello",
    }
    assert render_transcript_line(record, {"456": "A"}) == (
        "[2026-09-01T19:32:03Z] [id=123] A·Alice: hello"
    )


def test_notifications_use_latest_event_even_when_delivered_out_of_order():
    items = [Notification(kind="reaction", created_at=datetime.fromisoformat(stamp), body="hi")
             for stamp in ["2026-09-02T01:00:00+05:30", "2026-09-01T19:00:00Z"]]
    rendered = render_notifications(items)
    assert "Most recent notification: 2026-09-01T19:30:00Z" in rendered
    assert "[2026-09-01T19:30:00Z, reaction]" in rendered
    assert "[2026-09-01T19:00:00Z, reaction]" in rendered
