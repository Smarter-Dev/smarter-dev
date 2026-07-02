"""Tests for the bot's batched member-activity reporter."""

from __future__ import annotations

from datetime import datetime, timezone

from smarter_dev.bot.plugins.handler_events import ActivityBatcher


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeAPI:
    def __init__(self, status_code=200, raise_error=False):
        self.posted = []
        self.status_code = status_code
        self.raise_error = raise_error

    async def post(self, path, json_data=None):
        if self.raise_error:
            raise ConnectionError("api down")
        self.posted.append((path, json_data))
        return _Resp(self.status_code)


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


async def test_flush_posts_pending_events_once():
    batcher = ActivityBatcher()
    batcher.record("G1", "U1", _t("2026-07-02T10:00:00+00:00"))
    batcher.record("G1", "U2", _t("2026-07-02T10:00:05+00:00"))
    api = _FakeAPI()
    await batcher.flush(api)
    path, payload = api.posted[0]
    assert path == "/activity/batch"
    assert len(payload["events"]) == 2
    # Flushed events are gone; the next flush posts nothing.
    await batcher.flush(api)
    assert len(api.posted) == 1


async def test_record_keeps_latest_timestamp_per_user():
    batcher = ActivityBatcher()
    batcher.record("G1", "U1", _t("2026-07-02T10:00:00+00:00"))
    batcher.record("G1", "U1", _t("2026-07-02T10:05:00+00:00"))
    batcher.record("G1", "U1", _t("2026-07-02T09:00:00+00:00"))  # stale — ignored
    api = _FakeAPI()
    await batcher.flush(api)
    events = api.posted[0][1]["events"]
    assert len(events) == 1
    assert events[0]["message_at"] == "2026-07-02T10:05:00+00:00"


async def test_failed_flush_requeues_events():
    batcher = ActivityBatcher()
    batcher.record("G1", "U1", _t("2026-07-02T10:00:00+00:00"))
    down = _FakeAPI(raise_error=True)
    await batcher.flush(down)  # swallowed, events kept
    up = _FakeAPI()
    await batcher.flush(up)
    assert len(up.posted[0][1]["events"]) == 1


async def test_requeue_does_not_clobber_newer_events():
    batcher = ActivityBatcher()
    batcher.record("G1", "U1", _t("2026-07-02T10:00:00+00:00"))
    down = _FakeAPI(raise_error=True)

    real_flush_started = batcher._pending  # noqa: SLF001 — precondition sanity
    assert real_flush_started

    await batcher.flush(down)
    # A newer event arrives after the failed flush; requeue must not regress it.
    batcher.record("G1", "U1", _t("2026-07-02T10:09:00+00:00"))
    up = _FakeAPI()
    await batcher.flush(up)
    events = up.posted[0][1]["events"]
    assert events[0]["message_at"] == "2026-07-02T10:09:00+00:00"
