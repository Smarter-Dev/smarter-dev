"""Per-member serialisation across the bot and the handler workers.

Every gateway action on a member happens while holding that member's lock,
so two detectors cannot both read "no timeout yet" and both apply one, and a
release cannot interleave with a new hold. The lock is a row in
``moderation_member_locks`` with a lease: the bot and the workers are separate
processes, and a holder that dies frees it when the lease runs out. Inside one
process an ``asyncio.Lock`` per member queues the waiters, so they do not poll
the database against each other.

Taking a row is an INSERT; taking an expired one is an UPDATE guarded by the
expiry. Both are single statements, which is all the atomicity this needs on
Postgres and SQLite alike (SQLite ignores ``FOR UPDATE``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from smarter_dev.web.models import ModerationMemberLock

logger = logging.getLogger(__name__)

#: The lease. What runs under a member's lock is a few reads and writes and at
#: most two Discord calls, each bounded (hikari and the REST client time out
#: well inside this); a holder past it is presumed dead.
LOCK_TTL_SECONDS = 120.0
LOCK_WAIT_SECONDS = 60.0
_POLL_SECONDS = 0.05


class MemberLockTimeout(RuntimeError):
    """A member's lock stayed taken for :data:`LOCK_WAIT_SECONDS`."""


class _LocalLocks:
    """One ``asyncio.Lock`` per member, dropped when nobody holds or waits."""

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: tuple[str, str]) -> AsyncIterator[None]:
        lock, users = self._locks.get(key, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


#: The process's queue of local waiters. Shared by every gateway in the
#: process, so the bot's detectors queue behind each other here and only one
#: of them at a time contends for the database row.
PROCESS_LOCAL_LOCKS = _LocalLocks()


class MemberLocks:
    """Takes and frees members' leases."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        local: _LocalLocks | None = None,
        clock: Callable[[], float] = time.time,
        ttl: float = LOCK_TTL_SECONDS,
        wait: float = LOCK_WAIT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._local = local if local is not None else PROCESS_LOCAL_LOCKS
        self._clock = clock
        self.ttl = ttl
        self.wait = wait

    @contextlib.asynccontextmanager
    async def hold(self, guild_id: str, user_id: str) -> AsyncIterator[None]:
        key = (str(guild_id), str(user_id))
        async with self._local.hold(key):
            token = secrets.token_hex(16)
            await self._acquire(key, token)
            # A Discord call can outlast the lease (a long rate-limit wait):
            # renewed while held, so nobody takes it from a live holder.
            renewing = asyncio.create_task(self._renew(key, token))
            try:
                yield
            finally:
                renewing.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewing
                await self._release(key, token)

    @contextlib.asynccontextmanager
    async def hold_all(
        self, guild_id: str, user_ids: Iterable[str]
    ) -> AsyncIterator[None]:
        """Several members at once, always in the same order."""
        async with contextlib.AsyncExitStack() as stack:
            for user_id in sorted({str(user_id) for user_id in user_ids}):
                await stack.enter_async_context(self.hold(guild_id, user_id))
            yield

    async def _acquire(self, key: tuple[str, str], token: str) -> None:
        guild_id, user_id = key
        deadline = time.monotonic() + self.wait
        while True:
            now = self._clock()
            async with self._session_factory() as session:
                session.add(
                    ModerationMemberLock(
                        guild_id=guild_id,
                        user_id=user_id,
                        token=token,
                        expires_at=now + self.ttl,
                    )
                )
                try:
                    await session.commit()
                    return
                except IntegrityError:
                    await session.rollback()
                taken = await session.execute(
                    update(ModerationMemberLock)
                    .where(
                        ModerationMemberLock.guild_id == guild_id,
                        ModerationMemberLock.user_id == user_id,
                        ModerationMemberLock.expires_at < now,
                    )
                    .values(token=token, expires_at=now + self.ttl)
                )
                await session.commit()
                if taken.rowcount == 1:
                    logger.warning(
                        "Took over member %s's moderation lock in guild %s after "
                        "its lease ran out",
                        user_id,
                        guild_id,
                    )
                    return
            if time.monotonic() >= deadline:
                raise MemberLockTimeout(
                    f"member {user_id}'s moderation lock was not free after "
                    f"{int(self.wait)}s"
                )
            await asyncio.sleep(_POLL_SECONDS)

    async def _renew(self, key: tuple[str, str], token: str) -> None:
        guild_id, user_id = key
        while True:
            await asyncio.sleep(self.ttl / 3)
            try:
                async with self._session_factory() as session:
                    await session.execute(
                        update(ModerationMemberLock)
                        .where(
                            ModerationMemberLock.guild_id == guild_id,
                            ModerationMemberLock.user_id == user_id,
                            ModerationMemberLock.token == token,
                        )
                        .values(expires_at=self._clock() + self.ttl)
                    )
                    await session.commit()
            except Exception:  # noqa: BLE001 — the next beat tries again
                logger.warning(
                    "Could not renew member %s's moderation lock",
                    user_id,
                    exc_info=True,
                )

    async def _release(self, key: tuple[str, str], token: str) -> None:
        guild_id, user_id = key
        try:
            async with self._session_factory() as session:
                freed = await session.execute(
                    delete(ModerationMemberLock).where(
                        ModerationMemberLock.guild_id == guild_id,
                        ModerationMemberLock.user_id == user_id,
                        ModerationMemberLock.token == token,
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 — the lease frees it anyway
            logger.warning(
                "Could not free member %s's moderation lock in guild %s; its "
                "lease will",
                user_id,
                guild_id,
                exc_info=True,
            )
            return
        if freed.rowcount != 1:
            logger.warning(
                "Member %s's moderation lock in guild %s was lost before release",
                user_id,
                guild_id,
            )
