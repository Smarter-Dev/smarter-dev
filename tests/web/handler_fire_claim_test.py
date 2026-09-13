"""The at-most-once claim that makes fire-job retries safe.

Fire jobs retry (``max_attempts`` > 1) so a transient failure can't dead-letter a
fire and, for a recurring schedule, silently end the chain. But a fire's side
effects are not idempotent — re-running a script that already called
send_message double-posts — so exactly one attempt per job id may reach the
script. These tests pin that boundary.
"""

from __future__ import annotations

import pytest

from smarter_dev.web.handler_caps import (
    FIRE_CLAIM_TTL_SECONDS,
    claim_fire_attempt,
    claim_handler_key,
    handler_claim_key,
    handler_fire_claim_key,
)


class _FakeRedis:
    """Just enough Redis to model SET NX: first writer wins, others get None."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[dict] = []

    async def set(self, key, value, ex=None, nx=False):
        self.set_calls.append({"key": key, "value": value, "ex": ex, "nx": nx})
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


async def test_first_attempt_claims_the_job():
    redis = _FakeRedis()
    assert await claim_fire_attempt(redis, "job-1") is True


async def test_second_attempt_on_the_same_job_is_refused():
    redis = _FakeRedis()
    assert await claim_fire_attempt(redis, "job-1") is True
    # The retry: same job id, so the script must not run a second time.
    assert await claim_fire_attempt(redis, "job-1") is False


async def test_a_different_job_is_unaffected():
    redis = _FakeRedis()
    await claim_fire_attempt(redis, "job-1")
    assert await claim_fire_attempt(redis, "job-2") is True


async def test_claim_is_set_nx_with_a_ttl():
    """Without NX two racing workers both run the script; without a TTL the key
    leaks forever."""
    redis = _FakeRedis()
    await claim_fire_attempt(redis, "job-1")
    call = redis.set_calls[0]
    assert call["nx"] is True
    assert call["ex"] == FIRE_CLAIM_TTL_SECONDS
    assert call["key"] == handler_fire_claim_key("job-1")


async def test_claim_keys_are_namespaced_per_job():
    assert handler_fire_claim_key("a") != handler_fire_claim_key("b")
    assert handler_fire_claim_key("a").startswith("hfire:claim:")


@pytest.mark.parametrize("redis_reply", [None, 0, False, ""])
async def test_falsey_redis_replies_read_as_refused(redis_reply):
    """A real client returns None (not False) when NX declines — anything falsey
    must mean refused, never 'go ahead and re-run the script'."""

    class _Reply:
        async def set(self, *args, **kwargs):
            return redis_reply

    assert await claim_fire_attempt(_Reply(), "job-1") is False


# -- claim_handler_key: the script-facing claim, across CONCURRENT fires --------
#
# Same SET NX EX primitive, different race: not one job's retries but two fires of
# the SAME handler running at once (the scam posted in two channels). The
# per-handler memory store cannot dedupe those — it is read at fire start and
# written at fire end, so both fires see "not actioned yet".


async def test_first_claim_of_a_key_wins_and_the_second_is_refused():
    redis = _FakeRedis()
    assert await claim_handler_key(redis, "H1", "actioned:U1", 86400) is True
    # The concurrent fire, same handler, same author: must not act again.
    assert await claim_handler_key(redis, "H1", "actioned:U1", 86400) is False


async def test_claims_are_namespaced_per_handler():
    """Two handlers picking the same obvious key must not suppress each other."""
    redis = _FakeRedis()
    assert await claim_handler_key(redis, "H1", "actioned:U1", 86400) is True
    assert await claim_handler_key(redis, "H2", "actioned:U1", 86400) is True


async def test_a_different_key_on_the_same_handler_is_unaffected():
    redis = _FakeRedis()
    await claim_handler_key(redis, "H1", "actioned:U1", 86400)
    assert await claim_handler_key(redis, "H1", "actioned:U2", 86400) is True


async def test_claim_uses_set_nx_with_the_requested_ttl_and_key_format():
    redis = _FakeRedis()
    await claim_handler_key(redis, "H1", "actioned:U1", 3600)
    call = redis.set_calls[0]
    assert call["nx"] is True
    assert call["ex"] == 3600  # the script's ttl_seconds, passed straight through
    assert call["key"] == handler_claim_key("H1", "actioned:U1")
    assert call["key"] == "hclaim:H1:actioned:U1"


async def test_claim_key_is_distinct_from_the_fire_claim_namespace():
    assert not handler_claim_key("H1", "k").startswith("hfire:")


@pytest.mark.parametrize("redis_reply", [None, 0, False, ""])
async def test_falsey_replies_read_as_not_claimed(redis_reply):
    """A real client returns None when NX declines — anything falsey must mean
    "someone else holds it", never "go ahead and act again"."""

    class _Reply:
        async def set(self, *args, **kwargs):
            return redis_reply

    assert await claim_handler_key(_Reply(), "H1", "k", 60) is False
