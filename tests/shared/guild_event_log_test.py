"""Tests for the short-term guild event log.

Every Redis-touching test runs TWICE — once against a ``decode_responses=True``
handle (the worker/web tier) and once against ``decode_responses=False`` (the
bot tier). That mismatch is the whole cross-tier hazard of this module, so the
``redis_client`` fixture is parametrized rather than fixed, and no test is
allowed to pick a mode. The pure builders touch no handle and so take none.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

try:
    import fakeredis.aioredis as fakeredis_aioredis
except ImportError:  # pragma: no cover - fakeredis is a dev-only dependency
    fakeredis_aioredis = None

from smarter_dev.bot import guild_event_recorder as bot_recorder
from smarter_dev.shared.guild_event_log import EVENT_WINDOW_SECONDS
from smarter_dev.shared.guild_event_log import MAX_EVENTS_PER_GUILD
from smarter_dev.shared.guild_event_log import MAX_REASON_CHARS
from smarter_dev.shared.guild_event_log import MAX_SUMMARY_CHARS
from smarter_dev.shared.guild_event_log import GuildEvent
from smarter_dev.shared.guild_event_log import append_event
from smarter_dev.shared.guild_event_log import bot_message_event
from smarter_dev.shared.guild_event_log import chat_memory_enabled
from smarter_dev.shared.guild_event_log import guild_events_key
from smarter_dev.shared.guild_event_log import mod_action_event
from smarter_dev.shared.guild_event_log import read_since
from smarter_dev.shared.guild_event_log import read_window
from smarter_dev.web import guild_event_recorder as web_recorder

pytestmark = pytest.mark.skipif(
    fakeredis_aioredis is None,
    reason="fakeredis is not installed",
)

GUILD_ID = "123456789"
FIXED_NOW = datetime(2026, 8, 6, 14, 0, 0, tzinfo=UTC)


@pytest.fixture(params=[True, False], ids=["decoded", "raw_bytes"])
async def redis_client(request):
    """A fake Redis handle in BOTH decode modes the two tiers really use."""
    client = fakeredis_aioredis.FakeRedis(decode_responses=request.param)
    yield client
    await client.aclose()


def event_at(offset_seconds: float, *, summary: str = "I said something") -> GuildEvent:
    """A minimal bot-message event ``offset_seconds`` away from ``FIXED_NOW``."""
    return bot_message_event(
        guild_id=GUILD_ID,
        summary=summary,
        channel_id="555",
        channel_name="general",
        at=FIXED_NOW + timedelta(seconds=offset_seconds),
    )


# --------------------------------------------------------------------------
# Pure builders
# --------------------------------------------------------------------------


def test_mod_action_event_maps_the_dispatch_context():
    context = {
        "trigger_type": "mod_action",
        "action_type": "timeout",
        "target_user_id": "8",
        "target_username": "mallory",
        "moderator_user_id": "9",
        "moderator_username": "zech",
        "reason": "invite-link spam after a warning",
        "duration_seconds": 600,
        "source": "manual",
        "channel_id": "555",
        "trigger_message_id": "777",
        "created_at": "2026-08-06T13:10:00+00:00",
    }

    event = mod_action_event(context, guild_id=GUILD_ID, channel_name="general")

    assert event.kind == "mod_action"
    assert event.guild_id == GUILD_ID
    assert event.action == "timeout"
    assert event.target_username == "mallory"
    assert event.moderator_username == "zech"
    assert event.reason == "invite-link spam after a warning"
    assert event.duration_seconds == 600
    assert event.source == "manual"
    assert event.channel_id == "555"
    assert event.channel_name == "general"
    assert event.at == datetime(2026, 8, 6, 13, 10, tzinfo=UTC)


def test_mod_action_event_does_not_mutate_the_context():
    context = {"action_type": "warn", "reason": "spam"}
    context_before = dict(context)

    mod_action_event(context, guild_id=GUILD_ID)

    assert context == context_before


def test_mod_action_event_is_frozen():
    event = mod_action_event({"action_type": "warn"}, guild_id=GUILD_ID)

    with pytest.raises(FrozenInstanceError):
        event.action = "ban"


def test_mod_action_event_falls_back_to_now_without_a_created_at():
    event = mod_action_event({"action_type": "warn"}, guild_id=GUILD_ID)

    assert (datetime.now(UTC) - event.at).total_seconds() < 5


def test_mod_action_event_explicit_at_wins_over_the_context():
    context = {"action_type": "warn", "created_at": "2026-08-06T13:10:00+00:00"}

    event = mod_action_event(context, guild_id=GUILD_ID, at=FIXED_NOW)

    assert event.at == FIXED_NOW


def test_mod_action_event_ignores_an_unparseable_created_at():
    context = {"action_type": "warn", "created_at": "not a timestamp"}

    event = mod_action_event(context, guild_id=GUILD_ID)

    assert (datetime.now(UTC) - event.at).total_seconds() < 5


def test_mod_action_event_truncates_a_long_reason():
    context = {"action_type": "ban", "reason": "x" * 500}

    event = mod_action_event(context, guild_id=GUILD_ID)

    assert len(event.reason) == MAX_REASON_CHARS
    assert event.reason.endswith("…")


def test_bot_message_event_defaults_to_a_channel_message():
    event = bot_message_event(
        guild_id=GUILD_ID,
        summary="the weekly challenge announcement",
        channel_id="555",
        channel_name="announcements",
        at=FIXED_NOW,
    )

    assert event.kind == "bot_message"
    assert event.summary == "the weekly challenge announcement"
    assert event.channel_name == "announcements"
    assert event.at == FIXED_NOW


def test_bot_message_event_carries_a_dm_kind_and_recipient():
    event = bot_message_event(
        guild_id=GUILD_ID,
        summary="the timeout notice",
        kind="bot_dm",
        target_username="mallory",
        at=FIXED_NOW,
    )

    assert event.kind == "bot_dm"
    assert event.target_username == "mallory"
    assert event.channel_id is None


def test_bot_message_event_truncates_a_long_summary():
    event = bot_message_event(guild_id=GUILD_ID, summary="y" * 500, at=FIXED_NOW)

    assert len(event.summary) == MAX_SUMMARY_CHARS
    assert event.summary.endswith("…")


def test_guild_event_rejects_a_naive_timestamp():
    with pytest.raises(ValueError):
        GuildEvent(kind="bot_message", guild_id=GUILD_ID, at=datetime(2026, 8, 6, 14))


# --------------------------------------------------------------------------
# append_event / read_window / read_since
# --------------------------------------------------------------------------


async def test_append_then_read_window_round_trips_every_field(redis_client):
    context = {
        "action_type": "timeout",
        "target_username": "mallory",
        "moderator_username": "zech",
        "reason": "invite-link spam",
        "duration_seconds": 600,
        "source": "manual",
        "channel_id": "555",
    }
    original = mod_action_event(
        context, guild_id=GUILD_ID, channel_name="general", at=FIXED_NOW
    )

    assert await append_event(redis_client, original, now=FIXED_NOW) is True

    events, cursor = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)

    assert events == [original]
    assert cursor == FIXED_NOW.timestamp()


async def test_append_sets_the_window_expiry(redis_client):
    await append_event(redis_client, event_at(0), now=FIXED_NOW)

    ttl = await redis_client.ttl(guild_events_key(GUILD_ID))

    assert 0 < ttl <= EVENT_WINDOW_SECONDS


async def test_read_window_returns_events_oldest_first(redis_client):
    for offset in (-30, -300, -120):
        await append_event(redis_client, event_at(offset), now=FIXED_NOW)

    events, _ = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)

    assert [event.at for event in events] == [
        FIXED_NOW - timedelta(seconds=300),
        FIXED_NOW - timedelta(seconds=120),
        FIXED_NOW - timedelta(seconds=30),
    ]


async def test_a_later_append_trims_what_fell_out_of_the_window(redis_client):
    await append_event(redis_client, event_at(-10, summary="an hour ago"), now=FIXED_NOW)
    assert await redis_client.zcard(guild_events_key(GUILD_ID)) == 1

    much_later = FIXED_NOW + timedelta(seconds=EVENT_WINDOW_SECONDS)
    await append_event(
        redis_client,
        bot_message_event(guild_id=GUILD_ID, summary="just now", at=much_later),
        now=much_later,
    )

    events, _ = await read_window(redis_client, GUILD_ID, now=much_later)
    assert [event.summary for event in events] == ["just now"]
    assert await redis_client.zcard(guild_events_key(GUILD_ID)) == 1


async def test_appending_an_already_stale_event_stores_nothing(redis_client):
    stale = event_at(-EVENT_WINDOW_SECONDS - 60, summary="ancient history")

    await append_event(redis_client, stale, now=FIXED_NOW)

    assert await redis_client.zcard(guild_events_key(GUILD_ID)) == 0


async def test_read_window_ignores_an_event_that_fell_out_of_the_window(redis_client):
    await append_event(redis_client, event_at(-10), now=FIXED_NOW)

    later = FIXED_NOW + timedelta(seconds=EVENT_WINDOW_SECONDS)
    events, cursor = await read_window(redis_client, GUILD_ID, now=later)

    assert events == []
    assert cursor == later.timestamp()


async def test_append_caps_the_log_at_the_newest_events(redis_client):
    for index in range(MAX_EVENTS_PER_GUILD + 5):
        await append_event(
            redis_client,
            event_at(-MAX_EVENTS_PER_GUILD - 5 + index, summary=f"line {index}"),
            now=FIXED_NOW,
        )

    events, _ = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)

    assert len(events) == MAX_EVENTS_PER_GUILD
    assert events[0].summary == "line 5"
    assert events[-1].summary == f"line {MAX_EVENTS_PER_GUILD + 4}"


async def test_two_events_at_the_same_instant_both_survive(redis_client):
    first = event_at(-5, summary="I timed out mallory")
    second = event_at(-5, summary="I deleted a message")

    await append_event(redis_client, first, now=FIXED_NOW)
    await append_event(redis_client, second, now=FIXED_NOW)

    events, _ = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)

    assert {event.summary for event in events} == {
        "I timed out mallory",
        "I deleted a message",
    }


async def test_read_since_returns_only_strictly_newer_events(redis_client):
    await append_event(redis_client, event_at(-60, summary="old"), now=FIXED_NOW)
    _, cursor = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)

    await append_event(redis_client, event_at(-10, summary="new"), now=FIXED_NOW)

    events, new_cursor = await read_since(redis_client, GUILD_ID, cursor)

    assert [event.summary for event in events] == ["new"]
    assert new_cursor == (FIXED_NOW - timedelta(seconds=10)).timestamp()


async def test_read_since_never_delivers_the_same_event_twice(redis_client):
    await append_event(redis_client, event_at(-60), now=FIXED_NOW)
    _, cursor = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)
    await append_event(redis_client, event_at(-10), now=FIXED_NOW)

    _, cursor = await read_since(redis_client, GUILD_ID, cursor)
    events, unchanged_cursor = await read_since(redis_client, GUILD_ID, cursor)

    assert events == []
    assert unchanged_cursor == cursor


async def test_read_since_leaves_the_cursor_alone_when_nothing_is_new(redis_client):
    events, cursor = await read_since(redis_client, GUILD_ID, FIXED_NOW.timestamp())

    assert events == []
    assert cursor == FIXED_NOW.timestamp()


async def test_read_window_on_an_empty_log_returns_now_as_the_cursor(redis_client):
    events, cursor = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)

    assert events == []
    assert cursor == FIXED_NOW.timestamp()


async def test_malformed_members_are_skipped_not_raised(redis_client):
    key = guild_events_key(GUILD_ID)
    good = event_at(-10, summary="the real one")
    await append_event(redis_client, good, now=FIXED_NOW)
    await redis_client.zadd(
        key,
        {
            "not json at all": (FIXED_NOW - timedelta(seconds=30)).timestamp(),
            json.dumps([1, 2, 3]): (FIXED_NOW - timedelta(seconds=25)).timestamp(),
            json.dumps({"kind": "bot_message"}): (
                FIXED_NOW - timedelta(seconds=20)
            ).timestamp(),
            json.dumps({"kind": "bot_message", "guild_id": GUILD_ID, "at": "soon"}): (
                FIXED_NOW - timedelta(seconds=15)
            ).timestamp(),
        },
    )

    events, cursor = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)

    assert events == [good]
    assert cursor == (FIXED_NOW - timedelta(seconds=10)).timestamp()


async def test_non_ascii_text_round_trips_through_both_handles(redis_client):
    """The members this module writes are ASCII-escaped JSON, so a summary with
    emoji or accents survives the ``decode_responses=False`` handle's bytes."""
    event = event_at(-10, summary="I posted the résumé thread 🎉 for zoë")

    await append_event(redis_client, event, now=FIXED_NOW)

    events, _ = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)
    assert events == [event]


async def test_read_since_skips_malformed_members_too(redis_client):
    key = guild_events_key(GUILD_ID)
    await redis_client.zadd(
        key, {"garbage": (FIXED_NOW - timedelta(seconds=5)).timestamp()}
    )
    good = event_at(-1, summary="the real one")
    await append_event(redis_client, good, now=FIXED_NOW)

    events, _ = await read_since(
        redis_client, GUILD_ID, (FIXED_NOW - timedelta(seconds=30)).timestamp()
    )

    assert events == [good]


# --------------------------------------------------------------------------
# Global kill switch
# --------------------------------------------------------------------------


@pytest.mark.parametrize("flag_value", ["false", "False", "0", "no", "off", " off "])
async def test_append_event_is_a_no_op_when_memory_is_disabled(
    redis_client, monkeypatch, flag_value
):
    monkeypatch.setenv("CHAT_MEMORY_ENABLED", flag_value)

    assert await append_event(redis_client, event_at(0), now=FIXED_NOW) is False
    assert await redis_client.exists(guild_events_key(GUILD_ID)) == 0


@pytest.mark.parametrize("flag_value", ["true", "1", "yes", "on"])
async def test_append_event_writes_when_the_flag_is_truthy(
    redis_client, monkeypatch, flag_value
):
    monkeypatch.setenv("CHAT_MEMORY_ENABLED", flag_value)

    assert await append_event(redis_client, event_at(0), now=FIXED_NOW) is True


async def test_append_event_defaults_to_enabled(redis_client, monkeypatch):
    monkeypatch.delenv("CHAT_MEMORY_ENABLED", raising=False)

    assert await append_event(redis_client, event_at(0), now=FIXED_NOW) is True


async def test_the_kill_switch_is_read_per_call(redis_client, monkeypatch):
    monkeypatch.setenv("CHAT_MEMORY_ENABLED", "false")
    await append_event(redis_client, event_at(-20), now=FIXED_NOW)

    monkeypatch.setenv("CHAT_MEMORY_ENABLED", "true")
    await append_event(redis_client, event_at(-10, summary="after the flip"), now=FIXED_NOW)

    events, _ = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)

    assert [event.summary for event in events] == ["after the flip"]


def test_chat_memory_enabled_reports_the_current_environment(monkeypatch):
    monkeypatch.setenv("CHAT_MEMORY_ENABLED", "false")
    assert chat_memory_enabled() is False

    monkeypatch.setenv("CHAT_MEMORY_ENABLED", "true")
    assert chat_memory_enabled() is True


async def test_readers_still_drain_a_disabled_log(redis_client, monkeypatch):
    """Disabling writes must not strand events already in the window."""
    await append_event(redis_client, event_at(-10, summary="written earlier"), now=FIXED_NOW)
    monkeypatch.setenv("CHAT_MEMORY_ENABLED", "false")

    events, _ = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)

    assert [event.summary for event in events] == ["written earlier"]


# --------------------------------------------------------------------------
# Tier adapters — the 15 capture sites' entry point, and they NEVER raise
# --------------------------------------------------------------------------


class BotStub:
    def __init__(self, chat_memory_redis):
        self.d = {"chat_memory_redis": chat_memory_redis}


class ExplodingRedis:
    def pipeline(self, *args, **kwargs):
        raise ConnectionError("redis is down")


async def test_bot_recorder_writes_through_the_bot_handle(redis_client):
    event = event_at(-10, summary="I timed out mallory")

    await bot_recorder.record_guild_event(BotStub(redis_client), event, now=FIXED_NOW)

    events, _ = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)
    assert events == [event]


async def test_bot_recorder_is_a_no_op_without_a_handle():
    await bot_recorder.record_guild_event(BotStub(None), event_at(0))
    await bot_recorder.record_guild_event(BotStub({}.get("missing")), event_at(0))


async def test_bot_recorder_swallows_a_redis_failure():
    await bot_recorder.record_guild_event(BotStub(ExplodingRedis()), event_at(0))


async def test_bot_recorder_swallows_a_bot_without_services():
    class BotWithoutServices:
        d = {}

    await bot_recorder.record_guild_event(BotWithoutServices(), event_at(0))


async def test_web_recorder_writes_through_the_global_client(redis_client, monkeypatch):
    monkeypatch.setattr(
        web_recorder, "get_redis_client", lambda: redis_client, raising=True
    )
    event = event_at(-10, summary="I posted the weekly challenge")

    await web_recorder.record_guild_event(event, now=FIXED_NOW)

    events, _ = await read_window(redis_client, GUILD_ID, now=FIXED_NOW)
    assert events == [event]


async def test_web_recorder_swallows_a_client_failure(monkeypatch):
    def explode():
        raise RuntimeError("no redis configured")

    monkeypatch.setattr(web_recorder, "get_redis_client", explode, raising=True)

    await web_recorder.record_guild_event(event_at(0))


async def test_web_recorder_swallows_a_redis_failure(monkeypatch):
    monkeypatch.setattr(
        web_recorder, "get_redis_client", lambda: ExplodingRedis(), raising=True
    )

    await web_recorder.record_guild_event(event_at(0))
