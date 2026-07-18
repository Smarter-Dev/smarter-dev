"""Tests for the Redis-backed windowed caps (atomic INCR + EXPIRE NX)."""

from __future__ import annotations

from smarter_dev.web.handler_caps import (
    GUILD_MEMBER_EVENTS_PER_MIN,
    GUILD_ROLE_CHANGES_PER_MIN,
    GUILD_THREAD_OPS_PER_MIN,
    HANDLER_TIMERS_PER_HOUR,
    RENAME_WINDOW_SECONDS,
    RENAMES_PER_WINDOW,
    TIMER_ARMING_WINDOW_SECONDS,
    WindowedLimiter,
    channel_rename_key,
    fires_per_min_for_trigger,
    guild_member_events_key,
    guild_role_changes_key,
    guild_thread_ops_key,
    handler_timer_arm_key,
    HANDLER_FIRES_PER_MIN_MESSAGE,
    HANDLER_FIRES_PER_MIN_REACTION,
)


class _FakePipeline:
    """Mimics redis.asyncio pipeline: queued sync ops, awaited execute()."""

    def __init__(self, store: dict, expiries: dict):
        self._store = store
        self._expiries = expiries
        self._ops: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def incr(self, key):
        self._ops.append(("incr", key))
        return self

    def expire(self, key, seconds, nx=False):
        self._ops.append(("expire", key, seconds, nx))
        return self

    async def execute(self):
        results = []
        for op in self._ops:
            if op[0] == "incr":
                self._store[op[1]] = self._store.get(op[1], 0) + 1
                results.append(self._store[op[1]])
            else:
                _, key, seconds, nx = op
                if nx and key in self._expiries:
                    results.append(False)
                else:
                    self._expiries[key] = seconds
                    results.append(True)
        self._ops.clear()
        return results


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.expiries: dict = {}

    def pipeline(self, transaction=True):
        return _FakePipeline(self.store, self.expiries)


async def test_limiter_allows_up_to_limit_then_denies():
    limiter = WindowedLimiter(redis=_FakeRedis())
    key = "hcap:test"
    allowed = [await limiter.hit(key, limit=3) for _ in range(5)]
    assert allowed == [True, True, True, False, False]


async def test_expiry_fixed_on_first_hit_not_slid():
    fake = _FakeRedis()
    limiter = WindowedLimiter(redis=fake, window_seconds=60)
    await limiter.hit("k", limit=10)
    await limiter.hit("k", limit=10)
    # EXPIRE was only set once (NX), so the window does not slide.
    assert fake.expiries == {"k": 60}
    assert fake.store["k"] == 2


def test_reaction_triggers_have_tighter_fire_ceiling():
    assert fires_per_min_for_trigger("reaction") == HANDLER_FIRES_PER_MIN_REACTION
    assert fires_per_min_for_trigger("message") == HANDLER_FIRES_PER_MIN_MESSAGE
    assert HANDLER_FIRES_PER_MIN_REACTION < HANDLER_FIRES_PER_MIN_MESSAGE


def test_new_triggers_pinned_at_ten_fires_per_min():
    # §3.4: the five new triggers use the default (non-reaction) fire ceiling of
    # 10 — pinned so a future refactor cannot silently move it.
    for trigger in (
        "member_join",
        "member_leave",
        "member_rules_accepted",
        "member_role_change",
        "thread_create",
    ):
        assert fires_per_min_for_trigger(trigger) == 10
        assert fires_per_min_for_trigger(trigger) == HANDLER_FIRES_PER_MIN_MESSAGE


def test_guild_member_events_window():
    assert GUILD_MEMBER_EVENTS_PER_MIN == 60
    assert guild_member_events_key("G1") == "hcap:memberevt:G1"


def test_guild_thread_ops_window():
    assert GUILD_THREAD_OPS_PER_MIN == 30
    assert guild_thread_ops_key("G1") == "hcap:threadop:G1"


def test_guild_role_changes_key_format():
    assert GUILD_ROLE_CHANGES_PER_MIN == 30
    assert guild_role_changes_key("G1") == "hcap:rolechg:G1"


async def test_role_changes_window_declines_over_limit():
    limiter = WindowedLimiter(redis=_FakeRedis())
    key = guild_role_changes_key("G1")
    allowed = [
        await limiter.hit(key, GUILD_ROLE_CHANGES_PER_MIN)
        for _ in range(GUILD_ROLE_CHANGES_PER_MIN + 1)
    ]
    assert allowed[:GUILD_ROLE_CHANGES_PER_MIN] == [True] * GUILD_ROLE_CHANGES_PER_MIN
    assert allowed[GUILD_ROLE_CHANGES_PER_MIN] is False


async def test_limiter_window_seconds_override_uses_custom_ttl():
    fake = _FakeRedis()
    limiter = WindowedLimiter(redis=fake, window_seconds=60)
    await limiter.hit("rk", limit=2, window_seconds=600)
    # The override fixes a 600s expiry, not the instance's 60s default.
    assert fake.expiries == {"rk": 600}


async def test_limiter_default_window_unchanged_by_override_param():
    fake = _FakeRedis()
    limiter = WindowedLimiter(redis=fake, window_seconds=60)
    await limiter.hit("k", limit=2)
    assert fake.expiries == {"k": 60}


def test_channel_rename_key_shape():
    assert channel_rename_key("C1") == "hcap:rename:C1"


def test_rename_window_constants_are_two_per_600():
    assert RENAMES_PER_WINDOW == 2
    assert RENAME_WINDOW_SECONDS == 600


def test_handler_timer_arm_key_shape():
    assert handler_timer_arm_key("H1") == "hcap:timersched:H1"


def test_timer_arming_window_constants():
    assert HANDLER_TIMERS_PER_HOUR == 30
    assert TIMER_ARMING_WINDOW_SECONDS == 3600
