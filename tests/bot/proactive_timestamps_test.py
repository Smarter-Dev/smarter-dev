"""Agent-visible timestamps preserve dates, UTC offsets, and precision."""

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import pytest

from smarter_dev.bot.proactive import agent as agent_module
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


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["text", "structured", "error"])
async def test_tool_results_are_timestamped_after_completion(monkeypatch, outcome):
    finished = datetime(2026, 9, 4, 18, 42, 3, 123456, tzinfo=UTC)
    payload = [{"message_id": "123", "content": "hello"}]

    async def tool(ctx):
        # Advance the clock inside the tool to catch timestamps taken too early.
        monkeypatch.setattr(agent_module, "datetime", SimpleNamespace(now=lambda tz: finished))
        if outcome == "error":
            raise RuntimeError("unavailable")
        return payload if outcome == "structured" else "hello"

    result = await agent_module.tool_errors_returned(tool)(None)
    stamp = "2026-09-04T18:42:03.123456Z"
    if outcome == "structured":
        assert result == {"tool_completed_at": stamp, "result": payload}
    else:
        assert result.startswith(f"[tool_completed_at={stamp}]\n")
        assert ("RuntimeError: unavailable" if outcome == "error" else "hello") in result
