"""Holds: a member timed out while something they posted is under review.

A scam posted across several channels fires an admin handler once per copy,
and each fire wants the member timed out until a review has looked at the
post. A member has one Discord timeout, though, and the fires share it. With
only ``timeout_user`` and ``remove_timeout`` a script cannot share it safely:
one fire's clean verdict lifts the timeout another fire still needs, a fire
that ran late puts a five-minute timeout over a day-long one, and a timeout
call still in flight lands after the review that should have ended it.

This module is the member's side of that. A hold is named by a ``key`` (what
is under review) and every change to one member's timeout made through here
happens under that member's lock:

* ``hold`` times the member out for the key. It never shortens a timeout that
  already runs longer, whoever placed it, and does nothing for a key that is
  already held or was released in the last :data:`SETTLED_SECONDS`. When it
  extends a timeout somebody else placed, it remembers that timeout's expiry.
* ``release`` ends the key's hold. Once no key is held any more, and the
  timeout in place is the one a hold put there — never a moderator's, and
  never one ``timeout_user`` set — the member gets back whatever the holds
  extended, if it still has time to run, and is otherwise freed.

``timeout_user`` and ``remove_timeout`` take the same lock, so a hold never
reads the member's timeout and then writes over a change made in between by
another fire. A moderator acting by hand at that same instant is outside the
lock; Discord offers no compare-and-set to close that.

What is kept, in one Redis hash per member per guild: each held key with when
its hold runs out, each key released in the last :data:`SETTLED_SECONDS` with
when, the expiry of the timeout a hold last placed, and the expiry of the
timeout that was in place before the holds extended it. Keys are whatever the
script passed (a content hash or a message id), prefixed with the handler id
so two handlers cannot release each other. Every write drops the entries that
have run out or settled, and the hash expires :data:`SETTLED_SECONDS` after
its longest hold — so it holds at most the live holds, and lives at most
:data:`HOLD_MAX_SECONDS` plus :data:`SETTLED_SECONDS`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

from smarter_dev.web.admin_actions import AdminActor

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
# A hold's expiry is a whole second and Discord hands it back as stored; this
# only absorbs float rounding.
_SAME_EXPIRY_SECONDS = 0.002

_PLACED_FIELD = "placed"
_PRIOR_FIELD = "prior"
_BOOKKEEPING = frozenset({_PLACED_FIELD, _PRIOR_FIELD})
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


def _same_expiry(current: datetime | None, recorded: str | None) -> bool:
    return (
        current is not None
        and recorded is not None
        and abs(current.timestamp() - float(recorded)) <= _SAME_EXPIRY_SECONDS
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
        than ``seconds`` is left as it is, and the hold still counts. One
        running shorter is extended, and its expiry kept so a release can
        hand it back.
        """
        field = self._field(key)
        async with self.lock(user_id):
            now = time.time()
            states = await self._states(user_id)
            state = states.get(field)
            if _is_held(state, now) or _is_settled(state, now):
                return False
            # A whole second, so the expiry reads back the same at any precision.
            until = datetime.fromtimestamp(math.ceil(now + seconds), tz=UTC)
            states[field] = f"{_HELD}{until.timestamp()}"
            current = await self.actor.timeout_until(user_id)
            if current is None or current < until:
                stored = await self.actor.set_timeout_until(user_id, until, seconds)
                if stored is not None:
                    if not _same_expiry(current, states.get(_PLACED_FIELD)):
                        # Not a hold's: a moderator's or timeout_user's, or
                        # none. That is what the member goes back to.
                        if current is None:
                            states.pop(_PRIOR_FIELD, None)
                        else:
                            states[_PRIOR_FIELD] = str(current.timestamp())
                    states[_PLACED_FIELD] = str(stored.timestamp())
            await self._save(user_id, states, now)
            return True

    async def release(self, user_id: str, key: str) -> bool:
        """End ``key``'s hold; True when the member's timeout was ended.

        The key is marked released either way, so a hold for it that arrives
        late does nothing. The timeout is ended only when no key is held any
        more — this one's hold may already have run out — and the timeout in
        place is the one a hold put there. Ending it means lifting it, or
        putting back the timeout the holds extended when that still has time
        to run.
        """
        field = self._field(key)
        async with self.lock(user_id):
            now = time.time()
            states = await self._states(user_id)
            states[field] = f"{_RELEASED}{now}"
            await self._save(user_id, states, now)
            for other, state in states.items():
                if other not in _BOOKKEEPING and _is_held(state, now):
                    return False
            if _PLACED_FIELD not in states:
                return False
            current = await self.actor.timeout_until(user_id)
            if not _same_expiry(current, states[_PLACED_FIELD]):
                return False
            prior = states.get(_PRIOR_FIELD)
            if prior is not None and float(prior) > now:
                # To the millisecond, which is how Discord stored it.
                await self.actor.set_timeout_until(
                    user_id,
                    datetime.fromtimestamp(round(float(prior), 3), tz=UTC),
                    math.ceil(float(prior) - now),
                )
            else:
                await self.actor.remove_timeout(user_id)
            states.pop(_PLACED_FIELD, None)
            states.pop(_PRIOR_FIELD, None)
            await self._save(user_id, states, now)
            return True

    async def timeout(self, user_id: str, duration_seconds: int) -> str:
        """``timeout_user`` under the member's lock.

        No hold owns the timeout this leaves, even one that happens to end at
        the instant a hold's did, so no release lifts it.
        """
        async with self.lock(user_id):
            result = await self.actor.timeout_user(user_id, duration_seconds)
            now = time.time()
            states = await self._states(user_id)
            states.pop(_PLACED_FIELD, None)
            states.pop(_PRIOR_FIELD, None)
            await self._save(user_id, states, now)
            return result

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
        release settles, and goes entirely once nothing is live — the
        bookkeeping of which timeout is a hold's own goes with it.
        """
        name = member_holds_key(self.guild_id, user_id)
        live = {
            field: state
            for field, state in states.items()
            if field not in _BOOKKEEPING
            and (_is_held(state, now) or _is_settled(state, now))
        }
        if not live:
            await self.redis.delete(name)
            return
        ends = [float(state[2:]) + SETTLED_SECONDS for state in live.values()]
        live.update({field: states[field] for field in _BOOKKEEPING if field in states})
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(name)
            pipe.hset(name, mapping=live)
            pipe.expire(name, max(1, math.ceil(max(ends) - now)))
            await pipe.execute()
