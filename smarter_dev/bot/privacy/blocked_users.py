"""The blocked-users list: whose Discord messages never reach a model.

The web app owns the list (``GET /api/privacy/blocked-users``); a privacy
purge adds its target first. Each bot process caches the list, refreshes it
every ``REFRESH_SECONDS`` and keeps the last list when a refresh fails.

Until a list has loaded once, nothing from Discord may reach the chat or
proactive model. ``is_blocked`` answers True for everyone in that state, so a
path that forgets to check ``loaded`` still feeds the model only
``[BLOCKED BY USER]``; the routing paths check ``loaded`` and skip or defer.

After every successful fetch this process sets ``privacy:v1:enforcing:bot`` to
the list's revision for ``ENFORCING_TTL_SECONDS``. The admin page and the
purge job read it to know the bot enforces the list.

The list is deliberately generic: #74 takes it over as the opt-out list.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from smarter_dev.shared.privacy_purge import BLOCKED_PLACEHOLDER
from smarter_dev.shared.privacy_purge import ENFORCING_TTL_SECONDS
from smarter_dev.shared.privacy_purge import enforcing_key

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 60
ENFORCING_KEY = enforcing_key("bot")
PROCESS_ID = f"{socket.gethostname()}-{os.getpid()}"
__all__ = ["BLOCKED_PLACEHOLDER", "BlockedUsersCache", "get_blocked_users"]


@dataclass(frozen=True)
class BlockedUsersSnapshot:
    """One fetch of the list: its revision and the blocked Discord user ids."""

    revision: int
    user_ids: frozenset[str]

    def __repr__(self) -> str:
        # The ids are the people who asked to be forgotten; never print them.
        return (
            f"BlockedUsersSnapshot(revision={self.revision}, "
            f"count={len(self.user_ids)})"
        )

    __str__ = __repr__


class BlockedUsersCache:
    """In-process cache of the blocked-users list.

    The list counts as loaded only while this process's last successful fetch
    is younger than ``ENFORCING_TTL_SECONDS``: past that, the bot has stopped
    reporting itself as enforcing (its per-process key expired), so it must
    also stop sending Discord text to the models, exactly as on cold start,
    until a fetch succeeds again.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._snapshot: BlockedUsersSnapshot | None = None
        self._fetched_at: float | None = None
        self._loaded = asyncio.Event()

    @property
    def loaded(self) -> bool:
        """Whether a list loaded within the last ``ENFORCING_TTL_SECONDS``."""
        return (
            self._snapshot is not None
            and self._fetched_at is not None
            and self._clock() - self._fetched_at < ENFORCING_TTL_SECONDS
        )

    @property
    def revision(self) -> int | None:
        return None if self._snapshot is None else self._snapshot.revision

    def is_blocked(self, user_id: Any) -> bool:
        """True when this user's messages must not reach a model.

        Fails closed: with no fresh list every author counts as blocked.
        """
        if self._snapshot is None or not self.loaded:
            return True
        return str(user_id) in self._snapshot.user_ids

    def replace(self, snapshot: BlockedUsersSnapshot) -> None:
        self._snapshot = snapshot
        self._fetched_at = self._clock()
        self._loaded.set()

    def load(self, revision: int, user_ids: Iterable[Any]) -> None:
        """Install a list directly (startup wiring and tests)."""
        self.replace(
            BlockedUsersSnapshot(
                revision=revision, user_ids=frozenset(str(u) for u in user_ids)
            )
        )

    def reset(self) -> None:
        """Forget the list, back to the cold-start state (tests)."""
        self._snapshot = None
        self._fetched_at = None
        self._loaded = asyncio.Event()

    async def wait_loaded(self) -> None:
        await self._loaded.wait()
        while not self.loaded:  # loaded once, but stale now
            await asyncio.sleep(1.0)


_blocked_users = BlockedUsersCache()


def get_blocked_users() -> BlockedUsersCache:
    """The process-wide blocked-users cache."""
    return _blocked_users


async def refresh_once(
    blocked: BlockedUsersCache,
    fetch: Callable[[], Awaitable[BlockedUsersSnapshot]],
    redis: Any | None,
) -> bool:
    """Fetch the list once; on failure keep the last one. Returns success."""
    try:
        snapshot = await fetch()
    except Exception as error:  # noqa: BLE001 — keep the last list, retry later
        logger.warning(
            "blocked-users refresh failed (%s); keeping the last list (loaded=%s)",
            type(error).__name__,
            blocked.loaded,
        )
        return False
    blocked.replace(snapshot)
    if redis is not None:
        try:
            await report_enforcing(redis, snapshot.revision)
        except Exception as error:  # noqa: BLE001 — the keys just expire
            logger.warning(
                "could not publish %s (%s)", ENFORCING_KEY, type(error).__name__
            )
    return True


def process_key(process_id: str | None = None) -> str:
    """This process's own enforcing report."""
    return f"{ENFORCING_KEY}:{process_id or PROCESS_ID}"


async def report_enforcing(
    redis: Any, revision: int, *, process_id: str | None = None
) -> None:
    """Report this process, then publish the minimum over live processes.

    Every bot process (acting or standby, old or new during a deploy) reads
    the list; the purge job reads only ``privacy:v1:enforcing:bot``, so that
    key must never claim a revision a live process has not loaded. A process
    that stops fetching drops out when its key expires, the same moment it
    stops sending Discord input to the models.
    """
    await redis.set(process_key(process_id), str(revision), ex=ENFORCING_TTL_SECONDS)
    keys = [key async for key in redis.scan_iter(match=f"{ENFORCING_KEY}:*")]
    revisions = []
    for value in await redis.mget(keys) if keys else ():
        if value is None:
            continue
        try:
            revisions.append(int(value))
        except ValueError:
            continue
    if not revisions:
        revisions = [revision]
    await redis.set(ENFORCING_KEY, str(min(revisions)), ex=ENFORCING_TTL_SECONDS)


async def refresh_loop(
    blocked: BlockedUsersCache,
    fetch: Callable[[], Awaitable[BlockedUsersSnapshot]],
    redis: Any | None,
    *,
    interval: float = REFRESH_SECONDS,
) -> None:
    """Refresh the list every ``interval`` seconds, forever."""
    while True:
        await refresh_once(blocked, fetch, redis)
        await asyncio.sleep(interval)


def redact_blocked_mentions(text: str, blocked: BlockedUsersCache) -> str:
    """Replace ``<@id>`` / ``<@!id>`` mentions of blocked users in ``text``.

    A bystander's message can carry a blocked user's id as mention syntax;
    the mention becomes ``@[blocked user]`` so neither the id nor (after
    mention resolution) the name reaches the model. No regex: the ids come
    from the list, never from a pattern.
    """
    if "<@" not in text:
        return text
    out: list[str] = []
    index = 0
    while True:
        start = text.find("<@", index)
        if start < 0:
            out.append(text[index:])
            break
        end = text.find(">", start)
        if end < 0:
            out.append(text[index:])
            break
        inner = text[start + 2 : end]
        user_id = inner[1:] if inner.startswith("!") else inner
        out.append(text[index:start])
        if user_id.isdigit() and blocked.is_blocked(user_id):
            out.append("@[blocked user]")
        else:
            out.append(text[start : end + 1])
        index = end + 1
    return "".join(out)
