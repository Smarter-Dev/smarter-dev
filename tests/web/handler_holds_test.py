"""Holds on a member's timeout, shared by every fire that wants one.

A member has one Discord timeout. A burst fires an admin handler once per
copy, each fire wants the member held while a review runs, and reviews of
different posts finish at different times. These tests pin what
:mod:`smarter_dev.web.handler_holds` promises, now that a hold is a timeout
claim in the moderation gateway: a hold never shortens a longer timeout, a
clean review never lifts a timeout another claim still needs, a timeout a
moderator placed is never touched, and a call that lands late does nothing.

The holds run over a real AdminActor and gateway (a per-test database); only
Discord is stood in for.
"""

from __future__ import annotations

import asyncio
import math

import fakeredis.aioredis as fakeredis_aioredis
import pytest

import smarter_dev.web.handler_holds as handler_holds
from smarter_dev.web.admin_actions import AdminActionError
from smarter_dev.web.admin_actions import AdminActor
from smarter_dev.web.admin_actions import HandlerFireFacts
from smarter_dev.web.handler_holds import SETTLED_SECONDS
from smarter_dev.web.handler_holds import HoldLockTimeout
from smarter_dev.web.handler_holds import MemberHolds
from smarter_dev.web.handler_holds import member_holds_key
from smarter_dev.web.handler_holds import member_holds_lock_key
from tests.moderation_fakes import GUILD
from tests.moderation_fakes import FakeModerationDiscord
from tests.moderation_fakes import refused

DAY = 86400


class _Discord(FakeModerationDiscord):
    """The guild, with helpers that read like the tests."""

    def seconds_left(self, user_id: str = "U1") -> int | None:
        until = self.members.get(user_id)
        if until is None or until <= self.now():
            return None
        return math.ceil(until - self.now())

    def moderator_times_out(self, seconds: int, user_id: str = "U1") -> None:
        """A moderator acting by hand: straight to Discord, past every lock."""
        self.members[user_id] = round(self.now() + seconds + 0.417, 3)

    def timeouts(self) -> list:
        return [call for call in self.calls if call[0] == "timeout"]


def _actor(discord: _Discord, handler_id: str = "H1") -> AdminActor:
    return AdminActor(
        bot_token="t",
        guild_id=GUILD,
        moderation_port=discord,
        fire=HandlerFireFacts(handler_id=handler_id, handler_name="scam-banner"),
    )


@pytest.fixture
def redis():
    return fakeredis_aioredis.FakeRedis()


@pytest.fixture
def discord() -> _Discord:
    fake = _Discord()
    fake.add_member("U1")
    fake.add_member("U2")
    return fake


@pytest.fixture
def holds(redis, discord) -> MemberHolds:
    return MemberHolds(redis=redis, guild_id=GUILD, handler_id="H1", actor=_actor(discord))


# -- placing a hold ------------------------------------------------------------


async def test_a_hold_times_the_member_out_once_per_key(holds, discord):
    assert await holds.hold("U1", "post-a", 300) is True
    assert await holds.hold("U1", "post-a", 300) is False

    assert len(discord.timeouts()) == 1
    assert discord.seconds_left() in range(295, 302)


async def test_a_hold_never_shortens_a_longer_timeout(holds, discord):
    await holds.hold("U1", "repeat:post-a", DAY)

    assert await holds.hold("U1", "post-b", 300) is True

    assert len(discord.timeouts()) == 1
    assert discord.seconds_left() > DAY - 5


async def test_a_moderators_timeout_is_left_as_it_is(holds, discord):
    discord.moderator_times_out(7 * DAY)

    assert await holds.hold("U1", "repeat:post-a", DAY) is True

    assert discord.timeouts() == []
    assert discord.seconds_left() > 6 * DAY


async def test_a_moderators_shorter_timeout_is_not_replaced_either(holds, discord):
    # Automation never takes over a timeout a moderator chose: replacing it
    # would lose it when the hold ends.
    discord.moderator_times_out(120)

    assert await holds.hold("U1", "post-a", DAY) is True

    assert discord.timeouts() == []
    assert discord.seconds_left() in range(115, 122)


async def test_a_longer_hold_extends_a_shorter_timeout(holds, discord):
    await holds.hold("U1", "post-a", 300)
    await holds.hold("U1", "repeat:post-a", DAY)

    assert len(discord.timeouts()) == 2
    assert discord.seconds_left() > DAY - 5


async def test_a_hold_for_a_member_who_left_still_counts(holds, discord):
    del discord.members["U1"]

    assert await holds.hold("U1", "post-a", 300) is True
    assert await holds.hold("U1", "post-a", 300) is False
    assert discord.timeouts() == []


async def test_a_protected_member_is_never_held(holds, discord, redis):
    discord.protected["U1"] = "holds a staff role"

    assert await holds.hold("U1", "post-a", 300) is False

    assert discord.timeouts() == []
    assert await redis.exists(member_holds_key(GUILD, "U1")) == 0


# -- releasing a hold ----------------------------------------------------------


async def test_releasing_the_only_hold_lifts_the_timeout(holds, discord):
    await holds.hold("U1", "post-a", 300)

    assert await holds.release("U1", "post-a") is True
    assert discord.seconds_left() is None


async def test_a_release_leaves_the_timeout_another_key_still_needs(holds, discord):
    await holds.hold("U1", "post-a", 300)
    await holds.hold("U1", "post-b", 300)

    assert await holds.release("U1", "post-a") is False
    assert discord.seconds_left() is not None
    assert await holds.release("U1", "post-b") is True
    assert discord.seconds_left() is None


async def test_a_release_falls_back_to_the_longest_claim_left(holds, discord):
    await holds.hold("U1", "post-a", 600)
    await holds.hold("U1", "repeat:post-a", DAY)

    assert await holds.release("U1", "repeat:post-a") is True

    assert discord.seconds_left() in range(590, 602)


async def test_a_release_never_lifts_a_timeout_a_moderator_set(holds, discord):
    await holds.hold("U1", "post-a", 300)
    discord.moderator_times_out(DAY)

    assert await holds.release("U1", "post-a") is False
    assert discord.seconds_left() > DAY - 5


async def test_a_release_never_lifts_a_timeout_timeout_user_set(holds, discord):
    await holds.hold("U1", "post-a", 300)
    await holds.timeout("U1", DAY)

    assert await holds.release("U1", "post-a") is False
    assert discord.seconds_left() > DAY - 5


async def test_a_release_never_lifts_a_longer_hold(holds, discord):
    await holds.hold("U1", "repeat:post-a", DAY)
    await holds.hold("U1", "post-b", 300)

    assert await holds.release("U1", "post-b") is False
    assert discord.seconds_left() > DAY - 5


async def test_a_timeout_a_moderator_lifted_by_hand_is_not_touched(holds, discord):
    await holds.hold("U1", "post-a", 300)
    discord.members["U1"] = None

    assert await holds.release("U1", "post-a") is False
    assert len(discord.timeouts()) == 1


async def test_releasing_a_key_nobody_held_lifts_nothing(holds, discord):
    discord.moderator_times_out(300)

    assert await holds.release("U1", "post-a") is False
    assert discord.seconds_left() is not None


# -- late calls ----------------------------------------------------------------


async def test_a_released_key_is_not_held_again_by_a_late_call(holds, discord):
    await holds.hold("U1", "post-a", 300)
    await holds.release("U1", "post-a")

    assert await holds.hold("U1", "post-a", 300) is False
    assert discord.seconds_left() is None


async def test_a_key_released_before_it_was_ever_held_stays_released(holds, discord):
    await holds.release("U1", "post-a")

    assert await holds.hold("U1", "post-a", 300) is False
    assert discord.timeouts() == []


async def test_a_released_key_can_be_held_again_once_it_has_settled(
    holds, discord, monkeypatch
):
    clock = _Clock(monkeypatch)
    await holds.hold("U1", "post-a", 300)
    await holds.release("U1", "post-a")

    clock.advance(SETTLED_SECONDS - 1)
    assert await holds.hold("U1", "post-a", 300) is False
    clock.advance(2)
    assert await holds.hold("U1", "post-a", 300) is True


async def test_a_release_waits_for_a_hold_still_being_placed(holds, discord):
    # One fire's timeout call is still on its way to Discord when another
    # fire's review comes back clean. Lifting first would leave the late call
    # to time the member out with nothing left to end it.
    gate = discord.pause["set_timeout"] = asyncio.Event()
    placing = asyncio.create_task(holds.hold("U1", "post-a", 300))
    await asyncio.wait_for(_started(discord, "set_timeout"), timeout=5)

    releasing = asyncio.create_task(holds.release("U1", "post-a"))
    await asyncio.sleep(0.2)
    assert not releasing.done()

    gate.set()
    assert await asyncio.wait_for(placing, timeout=5) is True
    assert await asyncio.wait_for(releasing, timeout=5) is True
    assert discord.seconds_left() is None


async def test_timeout_user_waits_for_a_hold_still_being_placed(holds, discord):
    gate = discord.pause["set_timeout"] = asyncio.Event()
    placing = asyncio.create_task(holds.hold("U1", "post-a", 300))
    await asyncio.wait_for(_started(discord, "set_timeout"), timeout=5)

    day = asyncio.create_task(holds.timeout("U1", DAY))
    await asyncio.sleep(0.2)
    assert not day.done()

    gate.set()
    await asyncio.wait_for(asyncio.gather(placing, day), timeout=5)
    assert discord.seconds_left() > DAY - 5


async def _started(discord: _Discord, name: str) -> None:
    while name not in discord.started:
        await asyncio.sleep(0.01)
    await discord.started[name].wait()


# -- handlers, members and the lock ---------------------------------------------


async def test_one_handler_cannot_release_another_handlers_key(redis, discord):
    ours = MemberHolds(redis=redis, guild_id=GUILD, handler_id="H1", actor=_actor(discord, "H1"))
    theirs = MemberHolds(redis=redis, guild_id=GUILD, handler_id="H2", actor=_actor(discord, "H2"))
    await theirs.hold("U1", "post-a", 300)

    assert await ours.release("U1", "post-a") is False
    assert discord.seconds_left() is not None

    # And their hold keeps the timeout our own clean review would have lifted.
    await ours.hold("U1", "post-b", 300)
    assert await ours.release("U1", "post-b") is False
    assert await theirs.release("U1", "post-a") is True


async def test_holds_on_one_member_do_not_touch_another(holds, redis, discord):
    await holds.hold("U1", "post-a", 300)

    assert await redis.exists(member_holds_key(GUILD, "U2")) == 0
    assert await holds.release("U2", "post-a") is False
    assert discord.seconds_left() is not None


async def test_the_lock_is_freed_when_discord_refuses(holds, redis, discord):
    discord.fail["set_timeout"] = refused()

    with pytest.raises(AdminActionError):
        await holds.hold("U1", "post-a", 300)

    assert await redis.exists(member_holds_lock_key(GUILD, "U1")) == 0
    # Nothing was recorded for a hold that never took effect.
    assert await redis.exists(member_holds_key(GUILD, "U1")) == 0


async def test_a_lock_that_never_frees_fails_the_call(holds, redis, monkeypatch):
    monkeypatch.setattr(handler_holds, "LOCK_WAIT_SECONDS", 0.2)
    await redis.set(member_holds_lock_key(GUILD, "U1"), "another-fire")

    with pytest.raises(HoldLockTimeout):
        await holds.hold("U1", "post-a", 300)

    # Somebody else's lock is not ours to free.
    assert await redis.get(member_holds_lock_key(GUILD, "U1")) == b"another-fire"


async def test_a_lock_whose_lease_ran_out_does_not_free_its_successors(
    holds, redis, caplog
):
    name = member_holds_lock_key(GUILD, "U1")
    async with holds.lock("U1"):
        await redis.delete(name)
        await redis.set(name, "successor")

    assert await redis.get(name) == b"successor"
    assert "lost before release" in caplog.text


async def test_a_lock_still_held_is_freed_quietly(holds, redis, caplog):
    async with holds.lock("U1"):
        pass

    assert await redis.exists(member_holds_lock_key(GUILD, "U1")) == 0
    assert "lost" not in caplog.text


# -- what the record keeps ------------------------------------------------------


async def test_the_record_expires_with_its_longest_hold(holds, redis):
    await holds.hold("U1", "repeat:post-a", DAY)
    await holds.hold("U1", "post-b", 300)
    await holds.release("U1", "post-b")

    ttl = await redis.ttl(member_holds_key(GUILD, "U1"))
    # The hold's expiry is rounded up to the whole second.
    assert DAY < ttl <= DAY + SETTLED_SECONDS + 1


async def test_the_record_keeps_only_live_holds_and_fresh_releases(
    holds, redis, monkeypatch
):
    clock = _Clock(monkeypatch)
    await holds.hold("U1", "post-a", 60)
    await holds.release("U1", "post-a")
    await holds.hold("U1", "post-b", 60)
    clock.advance(SETTLED_SECONDS + 1)
    await holds.hold("U1", "post-c", 300)

    fields = {key.decode() for key in await redis.hkeys(member_holds_key(GUILD, "U1"))}
    assert fields == {"H1:post-c"}
    assert await redis.ttl(member_holds_key(GUILD, "U1")) <= 300 + SETTLED_SECONDS + 1


async def test_a_record_from_before_the_gateway_is_rewritten_without_its_bookkeeping(
    holds, redis
):
    name = member_holds_key(GUILD, "U1")
    await redis.hset(
        name, mapping={"H1:post-a": "h:1000.0", "placed": "1000.0", "prior": "900.0"}
    )
    await redis.expire(name, 1000)

    await holds.hold("U1", "post-b", 60)

    fields = {key.decode() for key in await redis.hkeys(name)}
    assert fields == {"H1:post-b"}


class _Clock:
    """Moves the holds module's clock forward without sleeping."""

    def __init__(self, monkeypatch):
        import time as real_time

        self._offset = 0.0
        self._real = real_time.time
        monkeypatch.setattr(
            handler_holds,
            "time",
            type("T", (), {"time": staticmethod(lambda: self._real() + self._offset),
                           "monotonic": staticmethod(real_time.monotonic)}),
        )

    def advance(self, seconds: float) -> None:
        self._offset += seconds
