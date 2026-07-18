"""Guild-scoped shared memory — a key/value store every admin handler in a guild
sees, unlike per-handler :class:`~smarter_dev.web.handler_memory.HandlerMemory`.

Some behaviors span two handler rows (a `dm_message` mirror and a `message`
relay are separate handlers, one `trigger_type` each) yet must share one fact —
the auto-bind relay target is the driving case. Private per-handler memory can't
carry that; this store can.

Storage is PER-KEY rows in ``guild_handler_memory`` (not one blob) because guild
memory is read/written by handler rows that fire concurrently on the same queue:
a single-blob read-modify-write would clobber a sibling fire's different key. A
fire loads a snapshot before it runs and, after, persists only the keys it set
(upsert) or deleted (delete) — untouched keys are never rewritten, so
different-key concurrent writes both survive and same-key writes are
last-write-wins.

Rails mirror ``HandlerMemory``: values must be JSON-serializable and the whole
store is size-capped (:data:`~smarter_dev.web.handler_memory.MAX_MEMORY_BYTES`,
16KB) — a breach raises :class:`CapExceeded` with cap ``guild_memory_size``. The
cap is checked against the snapshot this fire loaded, so under concurrent writes
the true row set can briefly exceed it before a later fire's check catches up —
the same soft-rail behavior as the windowed caps, not a hard transactional
ceiling.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.handler_budget import CapExceeded
from smarter_dev.web.handler_memory import MAX_MEMORY_BYTES
from smarter_dev.web.models import GuildHandlerMemory


class GuildMemory:
    """Host-owned guild-shared key/value store, snapshot-loaded per fire.

    Mirrors :class:`HandlerMemory` (copy-on-write ``set``/``delete``, JSON-only,
    a whole-store size cap) but tracks *which* keys changed so the job persists
    only those rows — the per-key discipline that makes concurrent different-key
    writes safe. ``writes()`` is the set-or-updated keys, ``deletes()`` the
    removed ones; a key can be in at most one (a later ``set`` un-marks a
    ``delete`` and vice-versa).
    """

    def __init__(self, initial: dict | None = None, max_bytes: int = MAX_MEMORY_BYTES):
        self._data: dict = dict(initial or {})
        self._max_bytes = max_bytes
        self._dirty_keys: set[str] = set()
        self._deleted_keys: set[str] = set()

    @property
    def dirty(self) -> bool:
        return bool(self._dirty_keys or self._deleted_keys)

    def get(self, key: str, default=None):
        return self._data.get(str(key), default)

    def set(self, key: str, value) -> bool:
        """Store ``value`` under ``key``. Fails loud on non-JSON or over-cap."""
        key = str(key)
        candidate = dict(self._data)
        candidate[key] = value
        try:
            encoded = json.dumps(candidate)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"guild memory values must be JSON-serializable (str/int/float/"
                f"bool/None/list/dict): {exc}"
            ) from exc
        if len(encoded.encode("utf-8")) > self._max_bytes:
            raise CapExceeded(
                "guild_memory_size",
                f"guild memory would exceed {self._max_bytes} bytes",
            )
        self._data = candidate
        self._dirty_keys.add(key)
        self._deleted_keys.discard(key)
        return True

    def delete(self, key: str) -> bool:
        """Remove ``key`` if present. Returns whether anything was removed."""
        key = str(key)
        if key not in self._data:
            return False
        candidate = dict(self._data)
        del candidate[key]
        self._data = candidate
        self._deleted_keys.add(key)
        self._dirty_keys.discard(key)
        return True

    def all(self) -> dict:
        """A deep copy of the whole store (so the script can iterate it safely)."""
        return copy.deepcopy(self._data)

    def snapshot(self) -> dict:
        """The current state, for reference by the caller."""
        return copy.deepcopy(self._data)

    def writes(self) -> dict:
        """The keys this fire set/updated, with their current values (to upsert)."""
        return {key: copy.deepcopy(self._data[key]) for key in self._dirty_keys}

    def deletes(self) -> list[str]:
        """The keys this fire removed (to delete)."""
        return list(self._deleted_keys)


async def load_guild_memory(session: AsyncSession, guild_id: str) -> dict:
    """Read every stored key for ``guild_id`` into a plain dict (the fire's snapshot)."""
    rows = await session.scalars(
        select(GuildHandlerMemory).where(
            GuildHandlerMemory.guild_id == str(guild_id)
        )
    )
    return {row.key: row.value for row in rows}


async def persist_guild_memory(
    session: AsyncSession, guild_id: str, writes: dict, deletes: list[str]
) -> None:
    """Upsert ``writes`` and remove ``deletes`` for ``guild_id``, per key.

    Only the touched keys are written, so a sibling fire that changed a different
    key is never clobbered; a same-key write is last-write-wins. The caller
    commits.
    """
    guild_id = str(guild_id)
    now = datetime.now(timezone.utc)
    for key in deletes:
        await session.execute(
            sql_delete(GuildHandlerMemory).where(
                GuildHandlerMemory.guild_id == guild_id,
                GuildHandlerMemory.key == str(key),
            )
        )
    for key, value in writes.items():
        key = str(key)
        existing = await session.scalar(
            select(GuildHandlerMemory).where(
                GuildHandlerMemory.guild_id == guild_id,
                GuildHandlerMemory.key == key,
            )
        )
        if existing is None:
            session.add(
                GuildHandlerMemory(
                    guild_id=guild_id, key=key, value=value, updated_at=now
                )
            )
        else:
            existing.value = value
            existing.updated_at = now
