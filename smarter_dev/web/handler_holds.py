"""Holds: a member timed out while something they posted is under review.

A scam posted across several channels fires an admin handler once per copy,
and each fire wants the member timed out until a review has looked at the
post. A member has one Discord timeout, though, and the fires share it: one
fire's clean verdict must not lift the timeout another fire still needs, a
fire that ran late must not put a five-minute timeout over a day-long one, and
a hold call still in flight must not land after the review that ended it.

This module is the script's side of that. A hold is named by a ``key`` (what
is under review), prefixed with the handler id so two handlers cannot release
each other:

* ``hold`` times the member out for the key, as a timeout *claim* in the
  moderation gateway (``smarter_dev.web.moderation_gateway``). The gateway
  never shortens a timeout that already runs longer and leaves one somebody
  else placed alone; the claim is recorded either way. ``hold`` does nothing
  for a key that is already held or was released in the last
  :data:`SETTLED_SECONDS`.
* ``release`` ends the key's claim. The gateway drops the member's timeout to
  the longest claim still held (another key's hold, a day-long repeat hold, a
  ``timeout_user``), or lifts it when none is, and never lifts a timeout a
  moderator placed.

What is kept here, in one Redis hash per member per guild, is only which keys
are held (with when their hold runs out) and which were released in the last
:data:`SETTLED_SECONDS`, so a late fire for a cleared post does nothing. Every
change for one member happens under that member's Redis lock, and the gateway
serialises the timeout itself under its own per-member lock, shared with the
bot's detectors. Every write drops entries that have run out or settled, and
the hash expires :data:`SETTLED_SECONDS` after its longest hold — so it holds
at most the live holds, and lives at most :data:`HOLD_MAX_SECONDS` plus
:data:`SETTLED_SECONDS`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from smarter_dev.web.admin_actions import AdminActor
from smarter_dev.web.moderation_gateway import types as gw

logger = logging.getLogger(__name__)

# Discord's own ceiling for a timeout.
HOLD_MAX_SECONDS = 28 * 86400
HOLD_KEY_MAX_LEN = 128
# How long a released key refuses a new hold. A fire for a copy of a post can
# run after the review that cleared the post; this has to outlast it.
SETTLED_SECONDS = 300

# The lease on a member's lock. What runs under it is Redis and at most two
# Discord calls, each capped at 15 seconds plus one retry after a short wait
# (``DiscordBotClient._request``), so a holder is done well inside this unless
# the whole worker is paused; a fire is cut off at two minutes anyway. A lock
# found lost at release is logged, not hidden.
LOCK_TTL_SECONDS = 120
LOCK_WAIT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05
_HELD = "h:"
_RELEASED = "r:"

# Free the lock only while it is still ours: one step, so a lease that ran out
# between the check and the delete cannot take the next holder's lock with it.
_UNLOCK = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


class HoldLockTimeout(RuntimeError):
    """The member's lock stayed taken for :data:`LOCK_WAIT_SECONDS`."""


def member_holds_key(guild_id: str, user_id: str) -> str:
    return f"hhold:{guild_id}:{user_id}"


def member_holds_lock_key(guild_id: str, user_id: str) -> str:
    return f"hhold:lock:{guild_id}:{user_id}"


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _is_held(state: str | None, now: float) -> bool:
    return bool(state) and state.startswith(_HELD) and float(state[2:]) > now


def _is_settled(state: str | None, now: float) -> bool:
    return (
        bool(state)
        and state.startswith(_RELEASED)
        and now - float(state[2:]) < SETTLED_SECONDS
    )


@dataclass
class MemberHolds:
    """One handler's view of the holds on a guild's members."""

    redis: Any
    guild_id: str
    handler_id: str
    actor: AdminActor

    @asynccontextmanager
    async def lock(self, user_id: str):
        """Hold this member's lock; every write to their timeout goes inside."""
        name = member_holds_lock_key(self.guild_id, user_id)
        token = secrets.token_hex(8)
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while not await self.redis.set(name, token, nx=True, ex=LOCK_TTL_SECONDS):
            if time.monotonic() >= deadline:
                raise HoldLockTimeout(
                    f"member {user_id} timeout lock was not free after "
                    f"{int(LOCK_WAIT_SECONDS)}s"
                )
            await asyncio.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            if not await self.redis.eval(_UNLOCK, 1, name, token):
                logger.warning(
                    "hold lock for member %s in guild %s was lost before release; "
                    "another fire may have changed their timeout meanwhile",
                    user_id,
                    self.guild_id,
                )

    async def hold(self, user_id: str, key: str, seconds: int) -> bool:
        """Time the member out for ``key``; True when this call began the hold.

        False, and nothing done, when the key is already held or was released
        in the last :data:`SETTLED_SECONDS`. A timeout already running longer
        than ``seconds`` is left as it is, and the hold still counts (its
        claim keeps the member timed out if the longer one is lifted).

        A protected member (staff, a bot, or anyone the staff-protection rule
        cannot clear) is never held: False, and nothing recorded.
        """
        field = self._field(key)
        if refusal := await self.actor.staff_refusal(user_id):
            logger.info(
                "hold on member %s in guild %s refused: %s",
                user_id,
                self.guild_id,
                refusal,
            )
            return False
        async with self.lock(user_id):
            now = time.time()
            states = await self._states(user_id)
            state = states.get(field)
            if _is_held(state, now) or _is_settled(state, now):
                return False
            outcome = await self.actor.hold_timeout(user_id, f"hold:{key}", seconds)
            if outcome.state == gw.SKIPPED and outcome.detail.startswith("protected: "):
                return False
            # A whole second, so the expiry reads back the same at any precision.
            states[field] = f"{_HELD}{math.ceil(now + seconds)}"
            await self._save(user_id, states, now)
            return True

    async def release(self, user_id: str, key: str) -> bool:
        """End ``key``'s hold; True when the member's timeout was ended or shortened.

        The key is marked released either way, so a hold for it that arrives
        late does nothing. The gateway then drops the timeout to the longest
        claim still held, or lifts it, and only ever touches a timeout the
        automation placed.
        """
        field = self._field(key)
        async with self.lock(user_id):
            now = time.time()
            states = await self._states(user_id)
            states[field] = f"{_RELEASED}{now}"
            await self._save(user_id, states, now)
            outcome = await self.actor.release_timeout(user_id, f"hold:{key}")
            return outcome.state == gw.CONFIRMED

    async def timeout(self, user_id: str, duration_seconds: int) -> str:
        """``timeout_user`` under the member's lock.

        Its claim is not a hold's, so no release ends it.
        """
        async with self.lock(user_id):
            return await self.actor.timeout_user(user_id, duration_seconds)

    async def remove_timeout(self, user_id: str) -> str:
        """``remove_timeout`` under the member's lock."""
        async with self.lock(user_id):
            return await self.actor.remove_timeout(user_id)

    def _field(self, key: str) -> str:
        return f"{self.handler_id}:{key}"

    async def _states(self, user_id: str) -> dict[str, str]:
        raw = await self.redis.hgetall(member_holds_key(self.guild_id, user_id))
        return {_text(field): _text(value) for field, value in raw.items()}

    async def _save(self, user_id: str, states: dict[str, str], now: float) -> None:
        """Write the member's record with only what is still live in it.

        Holds that ran out and releases that settled are dropped. The hash
        expires :data:`SETTLED_SECONDS` after the latest hold runs out or
        release settles, and goes entirely once nothing is live.
        """
        name = member_holds_key(self.guild_id, user_id)
        live = {
            field: state
            for field, state in states.items()
            if _is_held(state, now) or _is_settled(state, now)
        }
        if not live:
            await self.redis.delete(name)
            return
        ends = [float(state[2:]) + SETTLED_SECONDS for state in live.values()]
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(name)
            pipe.hset(name, mapping=live)
            pipe.expire(name, max(1, math.ceil(max(ends) - now)))
            await pipe.execute()
