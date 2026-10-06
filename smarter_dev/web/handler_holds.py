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
  already held or was released in the last :data:`SETTLED_SECONDS`.
* ``release`` ends the key's hold. The member's timeout is lifted only when no
  other key is still held and the timeout in place is the one a hold put
  there — never a moderator's, and never one ``timeout_user`` set.

``timeout_user`` and ``remove_timeout`` take the same lock, so a hold never
reads the member's timeout and then writes over a change made in between by
another fire. A moderator acting by hand at that same instant is outside the
lock; Discord offers no compare-and-set to close that.

What is kept, in one Redis hash per member per guild: each key with when its
hold runs out or when it was released, and the expiry of the timeout a hold
last placed. Keys are whatever the script passed (a content hash or a message
id), prefixed with the handler id so two handlers cannot release each other.
The hash expires with its longest hold plus :data:`SETTLED_SECONDS`.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

from smarter_dev.web.admin_actions import AdminActor

# Discord's own ceiling for a timeout.
HOLD_MAX_SECONDS = 28 * 86400
HOLD_KEY_MAX_LEN = 128
# How long a released key refuses a new hold. A fire for a copy of a post can
# run after the review that cleared the post; this has to outlast it.
SETTLED_SECONDS = 300

# A fire is cut off after two minutes, so no holder of the lock outlives this.
LOCK_TTL_SECONDS = 120
LOCK_WAIT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05
# A hold's expiry is a whole second and Discord hands it back as stored; this
# only absorbs float rounding.
_SAME_EXPIRY_SECONDS = 0.002

_PLACED_FIELD = "placed"
_HELD = "h:"
_RELEASED = "r:"


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
            held_by = await self.redis.get(name)
            if held_by is not None and _text(held_by) == token:
                await self.redis.delete(name)

    async def hold(self, user_id: str, key: str, seconds: int) -> bool:
        """Time the member out for ``key``; True when this call began the hold.

        False, and nothing done, when the key is already held or was released
        in the last :data:`SETTLED_SECONDS`. A timeout already running longer
        than ``seconds`` is left as it is, and the hold still counts.
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
            written = {field: f"{_HELD}{until.timestamp()}"}
            current = await self.actor.timeout_until(user_id)
            if current is None or current < until:
                stored = await self.actor.set_timeout_until(user_id, until, seconds)
                if stored is not None:
                    written[_PLACED_FIELD] = str(stored.timestamp())
            await self._write(user_id, written, seconds + SETTLED_SECONDS)
            return True

    async def release(self, user_id: str, key: str) -> bool:
        """End ``key``'s hold; True when the member's timeout was lifted.

        The key is marked released either way, so a hold for it that arrives
        late does nothing. The timeout is lifted only when this key was held,
        no other key still is, and the timeout in place is the one a hold put
        there.
        """
        field = self._field(key)
        async with self.lock(user_id):
            now = time.time()
            states = await self._states(user_id)
            await self._write(user_id, {field: f"{_RELEASED}{now}"}, SETTLED_SECONDS)
            if not _is_held(states.get(field), now):
                return False
            for other, state in states.items():
                if other not in (field, _PLACED_FIELD) and _is_held(state, now):
                    return False
            placed = states.get(_PLACED_FIELD)
            if placed is None:
                return False
            current = await self.actor.timeout_until(user_id)
            if (
                current is None
                or abs(current.timestamp() - float(placed)) > _SAME_EXPIRY_SECONDS
            ):
                return False
            await self.actor.remove_timeout(user_id)
            return True

    async def timeout(self, user_id: str, duration_seconds: int) -> str:
        """``timeout_user`` under the member's lock.

        No hold owns the timeout this leaves, even one that happens to end at
        the instant a hold's did, so no release lifts it.
        """
        async with self.lock(user_id):
            result = await self.actor.timeout_user(user_id, duration_seconds)
            await self.redis.hdel(
                member_holds_key(self.guild_id, user_id), _PLACED_FIELD
            )
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

    async def _write(self, user_id: str, fields: dict[str, str], keep: int) -> None:
        """Set ``fields`` and keep the hash at least ``keep`` seconds longer."""
        name = member_holds_key(self.guild_id, user_id)
        remaining = await self.redis.ttl(name)
        await self.redis.hset(name, mapping=fields)
        await self.redis.expire(name, max(int(remaining), int(keep)))
