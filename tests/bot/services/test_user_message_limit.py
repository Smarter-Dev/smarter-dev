"""Tests for the per-user rolling chat message limit (Redis sorted set)."""

from __future__ import annotations

import time

import pytest

from smarter_dev.bot.services.user_message_limit import (
    LIMIT_WINDOW_SECONDS,
    USER_MESSAGE_LIMIT,
    claim_notice_throttle,
    counted_messages,
    format_counted_window,
    format_over_limit_notice,
    format_usage_warning_notice,
    limit_key,
    notice_throttle_key,
    over_limit_status,
    record_directed_messages,
    usage_warning_key,
)


class _FakePipeline:
    """Mimics redis.asyncio pipeline: queued sync ops, awaited execute()."""

    def __init__(self, redis: "_FakeRedis"):
        self._redis = redis
        self._ops: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, dict(mapping)))
        return self

    def expire(self, key, seconds):
        self._ops.append(("expire", key, seconds))
        return self

    def zremrangebyscore(self, key, low, high):
        self._ops.append(("zremrangebyscore", key, low, high))
        return self

    def zcard(self, key):
        self._ops.append(("zcard", key))
        return self

    async def execute(self):
        results = [self._redis.apply(op) for op in self._ops]
        self._ops.clear()
        return results


class _FakeRedis:
    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.strings: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    def pipeline(self, transaction=True):
        return _FakePipeline(self)

    def apply(self, op: tuple):
        kind = op[0]
        if kind == "zadd":
            _, key, mapping = op
            members = self.zsets.setdefault(key, {})
            added = sum(1 for member in mapping if member not in members)
            members.update(mapping)
            return added
        if kind == "expire":
            self.expiries[op[1]] = op[2]
            return True
        if kind == "zremrangebyscore":
            _, key, low, high = op
            members = self.zsets.get(key, {})
            low_score = float("-inf") if low == "-inf" else float(low)
            removed = [m for m, s in members.items() if low_score <= s <= float(high)]
            for member in removed:
                del members[member]
            return len(removed)
        if kind == "zcard":
            return len(self.zsets.get(op[1], {}))
        raise AssertionError(f"unexpected op {op}")

    async def zrange(self, key, start, stop, withscores=False):
        by_score = sorted(self.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]))
        end = None if stop == -1 else stop + 1
        window = by_score[start:end]
        if withscores:
            return [(member, score) for member, score in window]
        return [member for member, _ in window]

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        if ex is not None:
            self.expiries[key] = ex
        return True

    async def expire(self, key, seconds, gt=False):
        if not gt or seconds > self.expiries.get(key, -1):
            self.expiries[key] = seconds
        return key in self.strings or key in self.zsets


async def _seed_messages(redis, user_id: str, epochs: list[float]) -> None:
    await record_directed_messages(
        redis, user_id, {f"msg-{i}": epoch for i, epoch in enumerate(epochs)}
    )


@pytest.mark.asyncio
async def test_record_adds_members_and_refreshes_window_ttl():
    redis = _FakeRedis()
    now = time.time()
    await record_directed_messages(redis, "u1", {"1": now, "2": now})

    key = limit_key("u1")
    assert set(redis.zsets[key]) == {"1", "2"}
    assert redis.expiries[key] == LIMIT_WINDOW_SECONDS


@pytest.mark.asyncio
async def test_record_same_message_id_never_double_counts():
    redis = _FakeRedis()
    now = time.time()
    await record_directed_messages(redis, "u1", {"1": now})
    await record_directed_messages(redis, "u1", {"1": now})
    assert len(redis.zsets[limit_key("u1")]) == 1


@pytest.mark.asyncio
async def test_record_empty_mapping_is_noop():
    redis = _FakeRedis()
    await record_directed_messages(redis, "u1", {})
    assert redis.zsets == {}


@pytest.mark.asyncio
async def test_record_returns_one_time_80_and_90_percent_warnings():
    redis = _FakeRedis()
    now = time.time()

    warnings = await record_directed_messages(
        redis,
        "u1",
        {f"msg-{i}": now + i / 100 for i in range(48)},
    )
    assert [warning.percentage for warning in warnings] == [80]
    assert warnings[0].reset_epoch == int(now) + LIMIT_WINDOW_SECONDS

    warnings = await record_directed_messages(
        redis,
        "u1",
        {f"msg-{i}": now + i / 100 for i in range(48, 54)},
    )
    assert [warning.percentage for warning in warnings] == [90]

    warnings = await record_directed_messages(
        redis, "u1", {"msg-54": now + 0.54}
    )
    assert warnings == []


def test_usage_warning_notice_matches_discord_message_format():
    from smarter_dev.bot.services.user_message_limit import UsageWarning

    notice = format_usage_warning_notice(
        "42", UsageWarning(percentage=80, reset_epoch=1_800_000_000)
    )
    assert notice == (
        "-# <@42> you've used 80% of your 4hr chat bot limit, "
        "resets <t:1800000000:R>"
    )


@pytest.mark.asyncio
async def test_warning_marker_ttl_moves_with_rolling_threshold_reset():
    redis = _FakeRedis()
    now = time.time()
    await record_directed_messages(
        redis,
        "u1",
        {f"msg-{i}": now + i for i in range(48)},
    )
    warning_key = usage_warning_key("u1", 80)
    first_ttl = redis.expiries[warning_key]

    assert await record_directed_messages(
        redis, "u1", {"msg-48": now + 60}
    ) == []
    assert redis.expiries[warning_key] > first_ttl


@pytest.mark.asyncio
async def test_under_limit_returns_none():
    redis = _FakeRedis()
    now = time.time()
    await _seed_messages(redis, "u1", [now - i for i in range(USER_MESSAGE_LIMIT - 1)])
    assert await over_limit_status(redis, "u1") is None


@pytest.mark.asyncio
async def test_at_limit_reports_oldest_message_and_retry_epoch():
    redis = _FakeRedis()
    now = time.time()
    oldest_epoch = now - 600
    epochs = [oldest_epoch + i for i in range(USER_MESSAGE_LIMIT)]
    await _seed_messages(redis, "u1", epochs)

    status = await over_limit_status(redis, "u1")
    assert status is not None
    assert status.window_started_epoch == pytest.approx(oldest_epoch)
    assert status.retry_epoch == int(oldest_epoch) + LIMIT_WINDOW_SECONDS


@pytest.mark.asyncio
async def test_messages_older_than_the_window_age_out():
    redis = _FakeRedis()
    now = time.time()
    stale = [now - LIMIT_WINDOW_SECONDS - 10 - i for i in range(USER_MESSAGE_LIMIT)]
    await _seed_messages(redis, "u1", stale)

    assert await over_limit_status(redis, "u1") is None
    # The stale entries were trimmed, not just ignored.
    assert redis.zsets[limit_key("u1")] == {}


@pytest.mark.asyncio
async def test_over_limit_frees_when_enough_messages_age_out():
    """With 3 extra messages, the user unblocks when the 4th-oldest expires —
    that entry is also the oldest of the LIMIT newest (the reported span)."""
    redis = _FakeRedis()
    now = time.time()
    epochs = [now - 1000 + i for i in range(USER_MESSAGE_LIMIT + 3)]
    await _seed_messages(redis, "u1", epochs)

    status = await over_limit_status(redis, "u1")
    assert status is not None
    assert status.window_started_epoch == pytest.approx(epochs[3])
    assert status.retry_epoch == int(epochs[3]) + LIMIT_WINDOW_SECONDS


@pytest.mark.asyncio
async def test_limits_are_per_user():
    redis = _FakeRedis()
    now = time.time()
    await _seed_messages(redis, "u1", [now - i for i in range(USER_MESSAGE_LIMIT)])
    assert await over_limit_status(redis, "u1") is not None
    assert await over_limit_status(redis, "u2") is None


def _status(window_started_epoch: float, retry_epoch: int = 1_800_000_000):
    from smarter_dev.bot.services.user_message_limit import OverLimitStatus

    return OverLimitStatus(
        window_started_epoch=window_started_epoch, retry_epoch=retry_epoch
    )


def test_notice_spans_under_two_hours_read_in_minutes():
    now = 1_700_000_000.0
    notice = format_over_limit_notice("42", _status(now - 30 * 60), now)
    assert notice.startswith("> -# <@42> ")
    assert f"you've sent {USER_MESSAGE_LIMIT} messages to the bot" in notice
    assert "in the last 30 minutes" in notice
    assert "you can try again <t:1800000000:R>" in notice


def test_notice_span_never_reads_below_one_minute():
    now = 1_700_000_000.0
    notice = format_over_limit_notice("42", _status(now - 5), now)
    assert "in the last 1 minute," in notice


def test_notice_span_just_under_the_hours_cutoff_stays_in_minutes():
    now = 1_700_000_000.0
    notice = format_over_limit_notice("42", _status(now - 119 * 60), now)
    assert "in the last 119 minutes" in notice


def test_notice_spans_of_two_plus_hours_read_in_hours():
    now = 1_700_000_000.0
    notice = format_over_limit_notice("42", _status(now - 2 * 60 * 60), now)
    assert "in the last 2 hours" in notice


def test_notice_hours_are_rounded():
    now = 1_700_000_000.0
    # 3h50m rounds to 4 hours.
    notice = format_over_limit_notice("42", _status(now - (3 * 60 + 50) * 60), now)
    assert "in the last 4 hours" in notice


@pytest.mark.asyncio
async def test_notice_throttle_claims_once_per_episode():
    redis = _FakeRedis()
    retry_epoch = int(time.time()) + 900
    assert await claim_notice_throttle(redis, "u1", retry_epoch) is True
    assert await claim_notice_throttle(redis, "u1", retry_epoch) is False
    # The claim lives exactly until the limit frees.
    assert redis.expiries[notice_throttle_key("u1")] == pytest.approx(900, abs=2)


@pytest.mark.asyncio
async def test_notice_throttle_ttl_never_drops_below_a_minute():
    redis = _FakeRedis()
    assert await claim_notice_throttle(redis, "u1", int(time.time()) - 50) is True
    assert redis.expiries[notice_throttle_key("u1")] == 60


@pytest.mark.asyncio
async def test_counted_messages_empty_counter_has_no_window():
    redis = _FakeRedis()
    assert await counted_messages(redis, "u1") == (0, None)


@pytest.mark.asyncio
async def test_counted_messages_reports_count_and_oldest():
    redis = _FakeRedis()
    now = time.time()
    oldest_epoch = now - 900
    await _seed_messages(redis, "u1", [oldest_epoch, now - 300, now - 30])

    counted, window_started = await counted_messages(redis, "u1")
    assert counted == 3
    assert window_started == pytest.approx(oldest_epoch)


@pytest.mark.asyncio
async def test_counted_messages_trims_aged_out_entries():
    redis = _FakeRedis()
    now = time.time()
    await _seed_messages(
        redis, "u1", [now - LIMIT_WINDOW_SECONDS - 10, now - 60]
    )

    counted, window_started = await counted_messages(redis, "u1")
    assert counted == 1
    assert window_started == pytest.approx(now - 60)


def test_counted_window_with_no_messages_names_the_full_window():
    assert format_counted_window(None, 1_700_000_000.0) == "the last 4 hours"


def test_counted_window_under_an_hour_reads_singular():
    now = 1_700_000_000.0
    assert format_counted_window(now - 30 * 60, now) == "the last hour"


def test_counted_window_rounds_hours_up():
    now = 1_700_000_000.0
    assert format_counted_window(now - 61 * 60, now) == "the last 2 hours"
    assert format_counted_window(now - 150 * 60, now) == "the last 3 hours"


def test_counted_window_is_capped_at_the_window_length():
    now = 1_700_000_000.0
    assert format_counted_window(now - LIMIT_WINDOW_SECONDS, now) == "the last 4 hours"
    # Defensive: an epoch slightly past the window still caps at 4.
    assert (
        format_counted_window(now - LIMIT_WINDOW_SECONDS - 90, now)
        == "the last 4 hours"
    )
