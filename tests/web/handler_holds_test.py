"""Holds on a member's timeout, shared by every fire that wants one.

A member has one Discord timeout. A burst fires an admin handler once per
copy, each fire wants the member held while a review runs, and reviews of
different posts finish at different times. These tests pin what
:mod:`smarter_dev.web.handler_holds` promises about the timeout the member is
left under: a hold never shortens a longer one, a clean review never lifts a
timeout something else still needs, a timeout the holds extended comes back
when they end, and a call that lands late does nothing.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import fakeredis.aioredis as fakeredis_aioredis
import httpx
import pytest

import smarter_dev.web.admin_actions as admin_actions
import smarter_dev.web.handler_holds as handler_holds
from smarter_dev.web.admin_actions import AdminActor
from smarter_dev.web.handler_holds import SETTLED_SECONDS
from smarter_dev.web.handler_holds import HoldLockTimeout
from smarter_dev.web.handler_holds import MemberHolds
from smarter_dev.web.handler_holds import member_holds_key
from smarter_dev.web.handler_holds import member_holds_lock_key

DAY = 86400


def _to_the_millisecond(until: datetime) -> datetime:
    """Discord stores a timeout's expiry to the millisecond."""
    return until.replace(microsecond=until.microsecond // 1000 * 1000)


@dataclass
class _Discord:
    """The member's timeout as Discord holds it, and every write made to it."""

    until: datetime | None = None
    calls: list = field(default_factory=list)
    # Set to keep a timeout write open until the test lets it through.
    write_gate: asyncio.Event | None = None
    write_started: asyncio.Event = field(default_factory=asyncio.Event)
    member_left: bool = False
    reads: int = 0

    async def timeout_until(self, user_id):
        self.reads += 1
        if self.until is None or self.until <= datetime.now(UTC):
            return None
        return self.until

    async def set_timeout_until(self, user_id, until, duration_seconds):
        self.write_started.set()
        if self.write_gate is not None:
            await self.write_gate.wait()
        if self.member_left:
            return None
        self.calls.append(("timeout", duration_seconds))
        self.until = _to_the_millisecond(until)
        return self.until

    async def timeout_user(self, user_id, duration_seconds=600):
        until = datetime.now(UTC) + timedelta(seconds=duration_seconds)
        await self.set_timeout_until(user_id, until, duration_seconds)
        return "ok"

    async def remove_timeout(self, user_id):
        self.calls.append(("remove_timeout",))
        self.until = None
        return "ok"

    def seconds_left(self) -> int | None:
        if self.until is None:
            return None
        return round((self.until - datetime.now(UTC)).total_seconds())

    def moderator_times_out(self, seconds: int) -> None:
        """A moderator acting by hand: straight to Discord, past the lock."""
        self.until = _to_the_millisecond(
            datetime.now(UTC) + timedelta(seconds=seconds, milliseconds=417)
        )


@pytest.fixture
def redis():
    return fakeredis_aioredis.FakeRedis()


@pytest.fixture
def discord() -> _Discord:
    return _Discord()


@pytest.fixture
def holds(redis, discord) -> MemberHolds:
    return MemberHolds(redis=redis, guild_id="G1", handler_id="H1", actor=discord)


# -- placing a hold ------------------------------------------------------------


async def test_a_hold_times_the_member_out_once_per_key(holds, discord):
    assert await holds.hold("U1", "post-a", 300) is True
    assert await holds.hold("U1", "post-a", 300) is False

    assert discord.calls == [("timeout", 300)]
    assert discord.seconds_left() in (300, 301)


async def test_a_hold_never_shortens_a_longer_timeout(holds, discord):
    discord.moderator_times_out(7 * DAY)

    assert await holds.hold("U1", "post-a", 300) is True

    assert discord.calls == []
    assert discord.seconds_left() > 6 * DAY


async def test_a_day_hold_does_not_shorten_a_moderators_week(holds, discord):
    discord.moderator_times_out(7 * DAY)

    await holds.hold("U1", "repeat:post-a", DAY)

    assert discord.calls == []
    assert discord.seconds_left() > 6 * DAY


async def test_a_longer_hold_extends_a_shorter_timeout(holds, discord):
    await holds.hold("U1", "post-a", 300)

    assert await holds.hold("U1", "repeat:post-a", DAY) is True

    assert discord.calls == [("timeout", 300), ("timeout", DAY)]
    assert discord.seconds_left() > DAY - 5


async def test_a_short_hold_after_a_day_hold_leaves_the_day(holds, discord):
    # The fire for another copy ran late, after the repeat was confirmed.
    await holds.hold("U1", "repeat:post-a", DAY)

    assert await holds.hold("U1", "post-b", 300) is True

    assert discord.calls == [("timeout", DAY)]
    assert discord.seconds_left() > DAY - 5


async def test_a_hold_for_a_member_who_left_still_counts(holds, discord):
    discord.member_left = True

    assert await holds.hold("U1", "post-a", 300) is True
    assert await holds.release("U1", "post-a") is False

    assert discord.calls == []


# -- releasing one -------------------------------------------------------------


async def test_releasing_the_only_hold_lifts_the_timeout(holds, discord):
    await holds.hold("U1", "post-a", 300)

    assert await holds.release("U1", "post-a") is True

    assert discord.until is None
    assert discord.calls == [("timeout", 300), ("remove_timeout",)]


async def test_a_release_leaves_the_timeout_another_key_still_needs(holds, discord):
    # Two different posts under review. The first comes back clean while the
    # second is still being looked at.
    await holds.hold("U1", "post-a", 300)
    await holds.hold("U1", "post-b", 300)

    assert await holds.release("U1", "post-a") is False
    assert discord.seconds_left() in (299, 300, 301)

    # The second is clean too: now nothing needs the timeout.
    assert await holds.release("U1", "post-b") is True
    assert discord.until is None


async def test_a_release_never_lifts_a_timeout_a_moderator_set(holds, discord):
    await holds.hold("U1", "post-a", 300)
    discord.moderator_times_out(3600)

    assert await holds.release("U1", "post-a") is False

    assert discord.seconds_left() > 3500
    assert ("remove_timeout",) not in discord.calls


async def test_a_release_never_lifts_a_timeout_timeout_user_set(holds, discord):
    await holds.hold("U1", "post-a", 300)
    await holds.timeout("U1", 300)

    assert await holds.release("U1", "post-a") is False

    assert discord.until is not None


async def test_a_release_never_lifts_a_timeout_user_ending_at_the_same_instant(
    holds, discord
):
    # The script's own timeout_user, placed over the hold, happens to run out
    # at the very moment the hold would have. It is still not the hold's.
    class _SameInstant(_Discord):
        async def timeout_user(self, user_id, duration_seconds=600):
            self.calls.append(("timeout", duration_seconds))
            return "ok"

    holds.actor = same = _SameInstant()
    await holds.hold("U1", "post-a", 300)
    await holds.timeout("U1", 300)

    assert await holds.release("U1", "post-a") is False

    assert same.until is not None


async def test_a_release_never_lifts_a_longer_hold(holds, discord):
    await holds.hold("U1", "post-a", 300)
    await holds.hold("U1", "repeat:post-b", DAY)

    assert await holds.release("U1", "post-a") is False

    assert discord.seconds_left() > DAY - 5


async def test_a_release_leaves_a_timeout_the_hold_did_not_place(holds, discord):
    discord.moderator_times_out(3600)
    await holds.hold("U1", "post-a", 300)

    assert await holds.release("U1", "post-a") is False

    assert discord.seconds_left() > 3500


# -- a timeout the holds extended comes back ------------------------------------


async def test_a_release_hands_back_a_moderators_shorter_timeout(holds, discord):
    # A moderator gave two minutes; the review hold stretched that to five and
    # the post came back clean. The moderator's two minutes still stand.
    discord.moderator_times_out(120)
    theirs = discord.until
    await holds.hold("U1", "post-a", 300)
    assert discord.seconds_left() in (300, 301)

    assert await holds.release("U1", "post-a") is True

    assert discord.until == theirs
    assert ("remove_timeout",) not in discord.calls


async def test_a_moderators_timeout_that_has_run_out_meanwhile_is_not_put_back(
    holds, discord, monkeypatch
):
    discord.moderator_times_out(60)
    await holds.hold("U1", "post-a", 300)
    _Clock(monkeypatch).advance(90)

    assert await holds.release("U1", "post-a") is True

    assert discord.until is None


async def test_the_handed_back_timeout_survives_a_chain_of_holds(holds, discord):
    # The review hold extended the moderator's, then the confirmed repeat
    # extended the review hold. When the last hold ends, the moderator's
    # timeout is what comes back, not the review's five minutes.
    discord.moderator_times_out(120)
    theirs = discord.until
    await holds.hold("U1", "post-a", 300)
    await holds.hold("U1", "repeat:post-a", DAY)

    assert await holds.release("U1", "post-a") is False
    assert await holds.release("U1", "repeat:post-a") is True

    assert discord.until == theirs


async def test_a_release_hands_back_what_timeout_user_set(holds, discord):
    await holds.timeout("U1", 120)
    theirs = discord.until
    await holds.hold("U1", "post-a", 300)

    assert await holds.release("U1", "post-a") is True

    assert discord.until == theirs


async def test_a_timeout_a_moderator_lifted_by_hand_is_not_put_back(holds, discord):
    # The moderator's two minutes were extended by a hold, then the moderator
    # freed the member outright. A later hold starts from a free member, so
    # its release must not bring the two minutes back.
    discord.moderator_times_out(120)
    await holds.hold("U1", "post-a", 300)
    discord.until = None
    assert await holds.release("U1", "post-a") is False
    await holds.hold("U1", "post-b", 300)

    assert await holds.release("U1", "post-b") is True

    assert discord.until is None
    assert discord.calls[-1] == ("remove_timeout",)


async def test_releasing_a_key_nobody_held_lifts_nothing(holds, discord):
    discord.moderator_times_out(3600)

    assert await holds.release("U1", "post-a") is False

    assert discord.calls == []


async def test_releasing_a_key_that_is_not_held_asks_discord_nothing(holds, discord):
    # Most reviews come back clean with no hold ever placed, and a key can be
    # released twice. Neither is worth a request to Discord.
    await holds.hold("U1", "post-a", 300)
    await holds.release("U1", "post-a")
    reads = discord.reads

    assert await holds.release("U1", "post-a") is False
    assert await holds.release("U1", "post-b") is False

    assert discord.reads == reads


async def test_a_hold_that_ran_out_does_not_keep_another_keys_timeout(
    holds, discord, monkeypatch
):
    await holds.hold("U1", "post-a", 60)
    clock = _Clock(monkeypatch)
    clock.advance(90)
    await holds.hold("U1", "post-b", 300)

    assert await holds.release("U1", "post-b") is True


async def test_the_last_release_ends_the_timeout_even_after_its_own_hold_ran_out(
    holds, discord, monkeypatch
):
    # A short hold, a longer one over it. The longer one is cleared first and
    # rightly leaves the timeout for the short one; the short one runs out
    # before its own review clears it. That release is still the last, and the
    # member is still under the longer hold's timeout.
    await holds.hold("U1", "post-a", 1)
    await holds.hold("U1", "post-b", 300)
    assert await holds.release("U1", "post-b") is False
    _Clock(monkeypatch).advance(2)

    assert await holds.release("U1", "post-a") is True

    assert discord.until is None


# -- calls that land late ------------------------------------------------------


async def test_a_released_key_is_not_held_again_by_a_late_call(holds, discord):
    await holds.hold("U1", "post-a", 300)
    await holds.release("U1", "post-a")

    assert await holds.hold("U1", "post-a", 300) is False

    assert discord.until is None


async def test_a_key_released_before_it_was_ever_held_stays_released(holds, discord):
    # The review cleared the post before the fire for its copy ran at all.
    await holds.release("U1", "post-a")

    assert await holds.hold("U1", "post-a", 300) is False

    assert discord.calls == []


async def test_a_released_key_can_be_held_again_once_it_has_settled(
    holds, discord, monkeypatch
):
    # A day-long hold keeps the member's record alive throughout, so it is the
    # release's own age that lets the key be held again, not the record lapsing.
    await holds.hold("U1", "repeat:post-z", DAY)
    await holds.release("U1", "post-a")
    clock = _Clock(monkeypatch)

    clock.advance(SETTLED_SECONDS - 1)
    assert await holds.hold("U1", "post-a", 300) is False
    clock.advance(2)
    assert await holds.hold("U1", "post-a", 300) is True


async def test_a_release_waits_for_a_hold_still_being_placed(holds, discord):
    # One fire's timeout call is still on its way to Discord when another
    # fire's review comes back clean. Lifting first would leave the late call
    # to time the member out with nothing left to end it.
    discord.write_gate = asyncio.Event()
    placing = asyncio.create_task(holds.hold("U1", "post-a", 300))
    await asyncio.wait_for(discord.write_started.wait(), timeout=5)

    releasing = asyncio.create_task(holds.release("U1", "post-a"))
    await asyncio.sleep(0.2)
    assert not releasing.done()

    discord.write_gate.set()
    assert await asyncio.wait_for(placing, timeout=5) is True
    assert await asyncio.wait_for(releasing, timeout=5) is True
    assert discord.calls == [("timeout", 300), ("remove_timeout",)]
    assert discord.until is None


async def test_timeout_user_waits_for_a_hold_still_being_placed(holds, discord):
    # A hold reads the member's timeout and then writes. A day-long timeout
    # written between the two would be replaced by five minutes.
    discord.write_gate = asyncio.Event()
    placing = asyncio.create_task(holds.hold("U1", "post-a", 300))
    await asyncio.wait_for(discord.write_started.wait(), timeout=5)

    day = asyncio.create_task(holds.timeout("U1", DAY))
    await asyncio.sleep(0.2)
    assert not day.done()

    discord.write_gate.set()
    await asyncio.wait_for(asyncio.gather(placing, day), timeout=5)
    assert discord.calls == [("timeout", 300), ("timeout", DAY)]
    assert discord.seconds_left() > DAY - 5


# -- handlers, members and the lock ---------------------------------------------


async def test_one_handler_cannot_release_another_handlers_key(redis, discord):
    ours = MemberHolds(redis=redis, guild_id="G1", handler_id="H1", actor=discord)
    theirs = MemberHolds(redis=redis, guild_id="G1", handler_id="H2", actor=discord)
    await theirs.hold("U1", "post-a", 300)

    assert await ours.release("U1", "post-a") is False
    assert discord.until is not None

    # And their hold keeps the timeout our own clean review would have lifted.
    await ours.hold("U1", "post-b", 300)
    assert await ours.release("U1", "post-b") is False
    assert await theirs.release("U1", "post-a") is True


async def test_holds_on_one_member_do_not_touch_another(holds, redis, discord):
    await holds.hold("U1", "post-a", 300)

    assert await redis.exists(member_holds_key("G1", "U2")) == 0
    assert await holds.release("U2", "post-a") is False
    assert discord.until is not None


async def test_the_lock_is_freed_when_discord_refuses(holds, redis):
    class _Refusing(_Discord):
        async def set_timeout_until(self, user_id, until, duration_seconds):
            raise admin_actions.AdminActionError("PATCH -> 403")

    holds.actor = _Refusing()

    with pytest.raises(admin_actions.AdminActionError):
        await holds.hold("U1", "post-a", 300)

    assert await redis.exists(member_holds_lock_key("G1", "U1")) == 0
    # Nothing was recorded for a hold that never took effect.
    assert await redis.exists(member_holds_key("G1", "U1")) == 0


async def test_a_lock_that_never_frees_fails_the_call(holds, redis, monkeypatch):
    monkeypatch.setattr(handler_holds, "LOCK_WAIT_SECONDS", 0.2)
    await redis.set(member_holds_lock_key("G1", "U1"), "another-fire")

    with pytest.raises(HoldLockTimeout):
        await holds.hold("U1", "post-a", 300)

    # Somebody else's lock is not ours to free.
    assert await redis.get(member_holds_lock_key("G1", "U1")) == b"another-fire"


async def test_a_lock_whose_lease_ran_out_does_not_free_its_successors(
    holds, redis, caplog
):
    # The worker holding the lock was paused past the lease; another fire
    # took the lock meanwhile. Freeing has to check and delete in one step,
    # or the paused fire's cleanup takes the successor's lock with it and a
    # third fire walks in on the second.
    name = member_holds_lock_key("G1", "U1")
    async with holds.lock("U1"):
        await redis.delete(name)
        await redis.set(name, "successor")

    assert await redis.get(name) == b"successor"
    assert "lost before release" in caplog.text


async def test_a_lock_still_held_is_freed_quietly(holds, redis, caplog):
    async with holds.lock("U1"):
        pass

    assert await redis.exists(member_holds_lock_key("G1", "U1")) == 0
    assert "lost" not in caplog.text


# -- what the record keeps ------------------------------------------------------


async def test_the_record_expires_with_its_longest_hold(holds, redis):
    await holds.hold("U1", "repeat:post-a", DAY)
    await holds.hold("U1", "post-b", 300)
    await holds.release("U1", "post-b")

    ttl = await redis.ttl(member_holds_key("G1", "U1"))
    # The hold's expiry is rounded up to the whole second.
    assert DAY < ttl <= DAY + SETTLED_SECONDS + 1


async def test_the_record_keeps_only_live_holds_and_fresh_releases(
    holds, redis, monkeypatch
):
    # A member who keeps the record alive with new holds must not carry
    # every old key along: what ran out or settled is dropped at the next
    # write, and the expiry follows the newest hold rather than piling up.
    clock = _Clock(monkeypatch)
    await holds.hold("U1", "post-a", 60)
    await holds.release("U1", "post-a")
    await holds.hold("U1", "post-b", 60)
    clock.advance(SETTLED_SECONDS + 1)
    await holds.hold("U1", "post-c", 300)

    fields = {key.decode() for key in await redis.hkeys(member_holds_key("G1", "U1"))}
    assert fields == {"H1:post-c", "placed"}
    assert await redis.ttl(member_holds_key("G1", "U1")) <= 300 + SETTLED_SECONDS + 1


async def test_an_expiry_that_has_passed_is_dropped_from_the_record(
    holds, redis, discord, monkeypatch
):
    # A moderator's two minutes, extended by a hold that keeps being renewed
    # by overlapping holds for weeks. The two minutes passed long ago and can
    # never be handed back; they must not ride along in every rewrite.
    clock = _Clock(monkeypatch)
    discord.moderator_times_out(120)
    await holds.hold("U1", "repeat:post-a", 28 * DAY)
    clock.advance(20 * DAY)
    await holds.hold("U1", "repeat:post-b", 28 * DAY)
    clock.advance(20 * DAY)
    await holds.hold("U1", "repeat:post-c", 28 * DAY)

    fields = {key.decode() for key in await redis.hkeys(member_holds_key("G1", "U1"))}
    assert fields == {"H1:repeat:post-b", "H1:repeat:post-c", "placed"}


async def test_a_record_with_nothing_live_in_it_is_dropped(holds, redis):
    # A record whose expiry outlived its contents (the hash is written as a
    # whole; fakeredis shares the test clock, so it is planted here). The
    # next write finds nothing live and drops it, bookkeeping included.
    name = member_holds_key("G1", "U1")
    await redis.hset(
        name, mapping={"H1:post-a": "h:1000.0", "placed": "1000.0", "prior": "900.0"}
    )
    await redis.expire(name, 1000)

    await holds.timeout("U1", 60)

    assert await redis.exists(name) == 0


# -- against the real Discord client --------------------------------------------


def _discord_api(member: dict) -> httpx.MockTransport:
    """Discord's member endpoint: stores a timeout to the millisecond."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            raw = json.loads(request.content)["communication_disabled_until"]
            if raw is not None:
                stored = datetime.fromisoformat(raw)
                raw = stored.replace(
                    microsecond=stored.microsecond // 1000 * 1000
                ).isoformat()
            member["communication_disabled_until"] = raw
            member.setdefault("writes", []).append(raw)
        return httpx.Response(
            200,
            json={
                "user": {"id": "U1"},
                "communication_disabled_until": member.get(
                    "communication_disabled_until"
                ),
            },
        )

    return httpx.MockTransport(handle)


@pytest.fixture
def no_event_log(monkeypatch):
    async def drop(event, **kwargs) -> None:
        return None

    monkeypatch.setattr(admin_actions, "record_guild_event", drop)


async def test_hold_and_release_through_the_real_actor(redis, no_event_log):
    member: dict = {}
    actor = AdminActor(bot_token="t", guild_id="G1", transport=_discord_api(member))
    holds = MemberHolds(redis=redis, guild_id="G1", handler_id="H1", actor=actor)

    assert await holds.hold("U1", "post-a", 300) is True
    assert member["communication_disabled_until"] is not None
    assert await holds.release("U1", "post-a") is True

    assert member["communication_disabled_until"] is None


async def test_real_actor_keeps_a_moderators_timeout_through_hold_and_release(
    redis, no_event_log
):
    week = (datetime.now(UTC) + timedelta(days=7)).isoformat().replace("+00:00", "Z")
    member = {"communication_disabled_until": week}
    actor = AdminActor(bot_token="t", guild_id="G1", transport=_discord_api(member))
    holds = MemberHolds(redis=redis, guild_id="G1", handler_id="H1", actor=actor)

    await holds.hold("U1", "post-a", 300)
    await holds.release("U1", "post-a")

    assert member["communication_disabled_until"] == week
    assert "writes" not in member


async def test_real_actor_hands_back_a_moderators_shorter_timeout(redis, no_event_log):
    # Discord keeps the moderator's expiry to the millisecond; it has to come
    # back exactly as stored, or the moderator's own record no longer matches.
    two_minutes = (datetime.now(UTC) + timedelta(minutes=2)).replace(microsecond=123000)
    member = {"communication_disabled_until": two_minutes.isoformat()}
    actor = AdminActor(bot_token="t", guild_id="G1", transport=_discord_api(member))
    holds = MemberHolds(redis=redis, guild_id="G1", handler_id="H1", actor=actor)

    await holds.hold("U1", "post-a", 300)
    assert member["communication_disabled_until"] != two_minutes.isoformat()
    assert await holds.release("U1", "post-a") is True

    assert member["communication_disabled_until"] == two_minutes.isoformat()


async def test_real_actor_reads_an_expired_timeout_as_none(redis, no_event_log):
    member = {
        "communication_disabled_until": (
            datetime.now(UTC) - timedelta(minutes=5)
        ).isoformat()
    }
    actor = AdminActor(bot_token="t", guild_id="G1", transport=_discord_api(member))

    assert await actor.timeout_until("U1") is None


class _Clock:
    """Moves the holds module's clock forward without sleeping."""

    def __init__(self, monkeypatch):
        import time as real_time

        self._offset = 0.0
        monkeypatch.setattr(
            handler_holds.time, "time", lambda: real_time.time_ns() / 1e9 + self._offset
        )

    def advance(self, seconds: float) -> None:
        self._offset += seconds
