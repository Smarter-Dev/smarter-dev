"""The blocked-users list: whose Discord messages never reach a model.

The web app owns the list (``GET /api/privacy/blocked-users``); a privacy
purge adds its target first. Each bot process caches the list, refreshes it
every ``REFRESH_SECONDS`` and keeps the last list when a refresh fails.

Until a list has loaded once, nothing from Discord may reach the chat or
proactive model. ``is_blocked`` answers True for everyone in that state, so a
path that forgets to check ``loaded`` still feeds the model only
``[BLOCKED BY USER]``; the routing paths check ``loaded`` and skip or defer.

Once a list has loaded, the process keeps going on the last list it loaded
for as long as fetches fail (Zech's decision: "The bot should always keep
going. The only case where it couldn't load the list would be during a
broader failure."). Every refresh cycle, failed or not, renews
``privacy:v1:enforcing:bot:{host-pid}`` (EX ``ENFORCING_TTL_SECONDS``) with
the revision it actually holds, so the web's minimum over live processes
stays at the stale revision and a purge that needs a newer one waits. The
stale state is logged at most once a minute (age and revision only).

The list is deliberately generic: #74 takes it over as the opt-out list.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
# A Discord user mention; a fixed pattern, the ids come from the list.
_USER_MENTION = re.compile(r"<@!?([0-9]{15,22})>")
_BARE_ID = re.compile(r"(?<![0-9])([0-9]{15,22})(?![0-9])")
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

    ``loaded`` means a list has loaded at least once. After that the last
    list stays in force however long fetches fail; only a process that has
    never loaded one counts everyone as blocked.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._snapshot: BlockedUsersSnapshot | None = None
        self._fetched_at: float | None = None
        self._loaded = asyncio.Event()

    @property
    def loaded(self) -> bool:
        """Whether a list has loaded at least once in this process."""
        return self._snapshot is not None

    def age_seconds(self) -> float | None:
        """Seconds since the last successful fetch, or None before one."""
        return None if self._fetched_at is None else self._clock() - self._fetched_at

    @property
    def revision(self) -> int | None:
        return None if self._snapshot is None else self._snapshot.revision

    def is_blocked(self, user_id: Any) -> bool:
        """True when this user's messages must not reach a model.

        Fails closed only on cold start: with no list ever loaded every
        author counts as blocked.
        """
        if self._snapshot is None:
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
        blocked.replace(snapshot)
        ok = True
    except Exception as error:  # noqa: BLE001 — keep the last list, retry later
        ok = False
        _log_stale(blocked, type(error).__name__)
    if redis is not None and blocked.revision is not None:
        # Renewed every cycle with the revision actually held, fetched now or
        # not: the key expires only when this process (or Redis) is gone.
        try:
            await report_enforcing(redis, blocked.revision)
        except Exception as error:  # noqa: BLE001 — the keys just expire
            logger.warning(
                "could not publish %s (%s)", ENFORCING_KEY, type(error).__name__
            )
    return ok


STALE_LOG_INTERVAL_SECONDS = 60
_last_stale_log: dict[int, float] = {}


def _log_stale(blocked: BlockedUsersCache, error_type: str) -> None:
    """At most once a minute per cache: the age and revision only."""
    now = time.monotonic()
    last = _last_stale_log.get(id(blocked))
    if last is not None and now - last < STALE_LOG_INTERVAL_SECONDS:
        return
    _last_stale_log[id(blocked)] = now
    age = blocked.age_seconds()
    logger.warning(
        "blocked-users refresh failed (%s); keeping revision=%s age=%s s",
        error_type,
        blocked.revision,
        "n/a (never loaded)" if age is None else int(age),
    )


def consumer_key(process_id: str | None = None) -> str:
    """This process's purge-consumer heartbeat (read by the web page)."""
    return f"privacy:v1:consumer:bot:{process_id or PROCESS_ID}"


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
    """Replace ``<@id>`` / ``<@!id>`` mentions of blocked users in ``text``
    with ``@[blocked user]``, and any other blocked id with
    ``[blocked user]``.

    A bystander's message (or a code block, or a bot's own message) can carry
    a blocked user's id as mention syntax; it becomes ``@[blocked user]`` so
    neither the id nor (after mention resolution) the name reaches the model.
    """

    def mention(match: re.Match[str]) -> str:
        return "@[blocked user]" if blocked.is_blocked(match.group(1)) else match[0]

    def bare(match: re.Match[str]) -> str:
        return "[blocked user]" if blocked.is_blocked(match.group(1)) else match[0]

    if "<@" in text:
        text = _USER_MENTION.sub(mention, text)
    # Any other appearance of a blocked id as a digit run: a bare id, an
    # unclosed "<@id", "<@ id>", a profile URL.
    return _BARE_ID.sub(bare, text)
