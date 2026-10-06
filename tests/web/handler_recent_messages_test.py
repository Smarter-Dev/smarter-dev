"""The per-author note of recent messages that burst detection reads.

A scam burst lands in several channels within seconds, faster than one review
finishes. These tests pin what dispatch keeps about each message (ids, shape
and a hash of what it carried, never the text), how long it is kept, and what a
script reads back.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import fakeredis.aioredis as fakeredis_aioredis
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import smarter_dev.web.admin_handlers_jobs as admin_handlers_jobs
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.handler_recent_messages import MAX_RECENT_MESSAGES
from smarter_dev.web.handler_recent_messages import RECENT_MESSAGES_TTL_SECONDS
from smarter_dev.web.handler_recent_messages import content_hash
from smarter_dev.web.handler_recent_messages import message_posted_at
from smarter_dev.web.handler_recent_messages import read_recent_messages
from smarter_dev.web.handler_recent_messages import recent_message_entry
from smarter_dev.web.handler_recent_messages import recent_messages_key
from smarter_dev.web.handler_recent_messages import record_recent_message
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import HandlerRun

_DISCORD_EPOCH_MS = 1420070400000
NOW = datetime(2026, 10, 5, 16, 9, 40, tzinfo=UTC)


def _snowflake(posted_at: datetime) -> str:
    return str((int(posted_at.timestamp() * 1000) - _DISCORD_EPOCH_MS) << 22)


def _context(posted_at: datetime, **overrides) -> dict:
    context = {
        "trigger_type": "message",
        "message_id": _snowflake(posted_at),
        "message_content": "",
        "author_id": "U1",
        "attachments": [],
        "is_thread": False,
    }
    context.update(overrides)
    return context


def _file(filename: str = "1.png", size: int = 48_211) -> dict:
    return {
        "url": f"https://cdn.example/{size}/{filename}",
        "content_type": "image/png",
        "filename": filename,
        "size": size,
    }


async def _note(
    redis, posted_at: datetime, channel: str, written_at: datetime, **context
):
    await record_recent_message(
        redis,
        "G1",
        "U1",
        recent_message_entry(_context(posted_at, **context), channel, written_at),
        written_at,
    )


@pytest.fixture
def redis():
    return fakeredis_aioredis.FakeRedis(decode_responses=True)


def test_entry_takes_its_time_from_the_message_id_not_the_dispatch():
    posted_at = NOW - timedelta(seconds=7)
    entry = recent_message_entry(_context(posted_at), "C1", NOW)
    assert entry["posted_at"] == pytest.approx(posted_at.timestamp(), abs=0.001)


def test_entry_falls_back_to_the_dispatch_time_for_a_non_snowflake_id():
    assert message_posted_at("M1", NOW) == NOW
    assert message_posted_at("", NOW) == NOW


def test_entry_records_shape_and_never_the_text():
    context = _context(
        NOW,
        message_content="free crypto at https://scam.example/claim now",
        attachments=[{"url": "https://cdn.example/1.png", "filename": "1.png"}] * 4,
    )
    entry = recent_message_entry(context, "C1", NOW)
    assert entry["attachment_count"] == 4
    assert entry["has_link"] is True
    assert set(entry) == {
        "channel_id",
        "message_id",
        "posted_at",
        "attachment_count",
        "has_link",
        "content_hash",
    }
    stored = json.dumps(entry)
    assert "scam.example" not in stored
    assert "cdn.example" not in stored
    assert "1.png" not in stored


def test_entry_without_link_or_files():
    entry = recent_message_entry(_context(NOW, message_content="hello"), "C1", NOW)
    assert entry["attachment_count"] == 0
    assert entry["has_link"] is False


def test_copies_of_one_post_share_a_hash_whatever_the_channel_or_upload():
    # The same text and the same files pasted into two channels: every upload
    # gets its own CDN url, and the text may differ in case and spacing.
    first = _context(
        NOW, message_content="Free  crypto, claim now", attachments=[_file()]
    )
    second = _context(
        NOW + timedelta(seconds=2),
        message_content="free crypto, CLAIM now ",
        attachments=[_file() | {"url": "https://cdn.example/other-upload/1.png"}],
    )
    assert content_hash(first) == content_hash(second)
    assert len(content_hash(first)) == 16


@pytest.mark.parametrize(
    "change",
    [
        {"message_content": "free crypto, claim today"},
        {"attachments": [_file(size=51_000)]},
        {"attachments": [_file("2.png")]},
        {"attachments": [_file(), _file("2.png")]},
        {"attachments": []},
        {"author_id": "U2"},
    ],
)
def test_a_different_post_has_a_different_hash(change):
    # Two screenshots both named image.png differ in size; another member's
    # identical post is not comparable either.
    post = {"message_content": "free crypto, claim now", "attachments": [_file()]}
    assert content_hash(_context(NOW, **post)) != content_hash(
        _context(NOW, **(post | change))
    )


def test_hash_does_not_depend_on_the_order_files_were_attached():
    one = _context(NOW, attachments=[_file("1.png", 10), _file("2.png", 20)])
    other = _context(NOW, attachments=[_file("2.png", 20), _file("1.png", 10)])
    assert content_hash(one) == content_hash(other)


def test_hash_still_works_for_a_bot_that_does_not_send_file_sizes():
    # A bot one release behind sends no size: the hash falls back to names.
    old_shape = [{"url": "u", "content_type": "image/png", "filename": "1.png"}]
    assert content_hash(_context(NOW, attachments=old_shape)) == content_hash(
        _context(NOW + timedelta(seconds=1), attachments=list(old_shape))
    )


def test_entry_for_a_thread_message_names_the_thread_not_its_parent():
    # Dispatch keys a thread message off the PARENT channel; the message itself
    # lives in the thread, which is where delete_message must be aimed.
    context = _context(NOW, is_thread=True, thread_id="T9")
    assert recent_message_entry(context, "C-PARENT", NOW)["channel_id"] == "T9"


async def test_read_returns_newest_first_with_ages(redis):
    for seconds_ago, channel in ((9, "C1"), (5, "C2"), (1, "C3")):
        await _note(redis, NOW - timedelta(seconds=seconds_ago), channel, NOW)

    rows = await read_recent_messages(redis, "G1", "U1", now=NOW)

    assert [row["channel_id"] for row in rows] == ["C3", "C2", "C1"]
    assert [row["age_seconds"] for row in rows] == [1.0, 5.0, 9.0]
    assert set(rows[0]) == {
        "channel_id",
        "message_id",
        "age_seconds",
        "attachment_count",
        "has_link",
        "content_hash",
    }


async def test_read_is_per_author_and_per_guild(redis):
    await _note(redis, NOW, "C1", NOW)

    assert await read_recent_messages(redis, "G1", "U2", now=NOW) == []
    assert await read_recent_messages(redis, "G2", "U1", now=NOW) == []
    assert len(await read_recent_messages(redis, "G1", "U1", now=NOW)) == 1


async def test_key_expires_two_minutes_after_the_last_message(redis):
    await _note(redis, NOW, "C1", NOW)
    ttl = await redis.ttl(recent_messages_key("G1", "U1"))
    assert 0 < ttl <= RECENT_MESSAGES_TTL_SECONDS


async def test_read_hides_a_note_past_the_window_that_nothing_has_erased_yet(redis):
    old = NOW - timedelta(seconds=RECENT_MESSAGES_TTL_SECONDS + 5)
    await _note(redis, old, "C-OLD", old)

    assert await read_recent_messages(redis, "G1", "U1", now=NOW) == []
    assert len(await read_recent_messages(redis, "G1", "U1", now=old)) == 1


async def test_a_later_message_erases_notes_past_the_window(redis):
    # Each message refreshes the key's TTL. Without the erase, a member who
    # posts every minute and a half would keep every note alive until the cap
    # pushed it out; hiding it from the read would not delete it.
    first = NOW - timedelta(seconds=RECENT_MESSAGES_TTL_SECONDS + 5)
    await _note(redis, first, "C-OLD", first)
    await _note(redis, NOW, "C-NEW", NOW)

    key = recent_messages_key("G1", "U1")
    stored = [json.loads(raw) for raw in await redis.zrange(key, 0, -1)]
    assert [entry["channel_id"] for entry in stored] == ["C-NEW"]


async def test_a_note_inside_the_window_survives_a_later_message(redis):
    earlier = NOW - timedelta(seconds=RECENT_MESSAGES_TTL_SECONDS - 1)
    await _note(redis, earlier, "C1", earlier)
    await _note(redis, NOW, "C2", NOW)

    assert await redis.zcard(recent_messages_key("G1", "U1")) == 2


async def test_no_note_outlives_twice_the_window(redis):
    # The longest a note can stay in Redis: written, then kept by a second
    # message just inside the window, whose own TTL then runs out.
    await _note(redis, NOW, "C1", NOW)
    just_inside = NOW + timedelta(seconds=RECENT_MESSAGES_TTL_SECONDS - 1)
    await _note(redis, just_inside, "C2", just_inside)

    ttl = await redis.ttl(recent_messages_key("G1", "U1"))
    lifetime = (just_inside - NOW).total_seconds() + ttl
    assert lifetime < 2 * RECENT_MESSAGES_TTL_SECONDS


async def test_the_same_message_noted_twice_is_one_row(redis):
    await _note(redis, NOW, "C1", NOW)
    await _note(redis, NOW, "C1", NOW)

    assert len(await read_recent_messages(redis, "G1", "U1", now=NOW)) == 1


async def test_set_is_capped_keeping_the_newest(redis):
    for index in range(MAX_RECENT_MESSAGES + 10):
        posted_at = NOW - timedelta(seconds=60) + timedelta(seconds=index)
        await _note(redis, posted_at, f"C{index}", NOW)

    assert await redis.zcard(recent_messages_key("G1", "U1")) == MAX_RECENT_MESSAGES
    rows = await read_recent_messages(redis, "G1", "U1", now=NOW)
    assert rows[0]["channel_id"] == f"C{MAX_RECENT_MESSAGES + 9}"


# -- the fire job hands the reader to a real script ----------------------------


class _SessionCtx:
    """``get_db_session_context()`` over the test engine."""

    def __init__(self, engine):
        self._maker = async_sessionmaker(engine, expire_on_commit=False)

    def __call__(self):
        return self

    async def __aenter__(self):
        self._session = self._maker()
        return self._session

    async def __aexit__(self, *exc):
        await self._session.close()
        return False


async def test_admin_fire_job_lets_a_script_read_what_dispatch_noted(
    monkeypatch, test_engine, redis
):
    # Nothing between the note and the script is faked: the real fire job binds
    # the reader, and the real runtime runs a script that calls it.
    handler_id = uuid4()
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        session.add(
            AdminHandler(
                id=handler_id,
                guild_id="G1",
                name="burst-probe",
                trigger_type="message",
                settings={},
                channel_ids=[],
                description="d",
                script=(
                    "rows = await list_recent_messages(context['author_id'])\n"
                    "await memory_set('seen', [[r['channel_id'], r['content_hash']] "
                    "for r in rows])\n"
                ),
                created_by_admin="A1",
            )
        )
        await session.commit()

    now = datetime.now(UTC)
    for seconds_ago, channel in ((3, "C1"), (1, "C2")):
        posted_at = now - timedelta(seconds=seconds_ago)
        await record_recent_message(
            redis, "G1", "U7", recent_message_entry(_context(posted_at), channel, now)
        )
    # Another guild's note for the same member must stay out of reach.
    await record_recent_message(
        redis, "G2", "U7", recent_message_entry(_context(now), "C-ELSEWHERE", now)
    )

    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        admin_handlers_jobs, "get_db_session_context", _SessionCtx(test_engine)
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: redis)

    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=str(handler_id),
            channel_id="C2",
            trigger_context={"trigger_type": "message", "author_id": "U7"},
        ),
        SimpleNamespace(job=SimpleNamespace(id=uuid4().hex)),
    )

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        handler = await session.get(AdminHandler, handler_id)
        [run] = list(
            await session.scalars(
                select(HandlerRun).where(HandlerRun.handler_id == handler_id)
            )
        )
    assert run.outcome == "ok", run.error
    assert run.lookups == 1
    # Both channels, newest first, and the two notes are copies of one post.
    assert [channel for channel, _ in handler.memory["seen"]] == ["C2", "C1"]
    assert len({shared for _, shared in handler.memory["seen"]}) == 1
