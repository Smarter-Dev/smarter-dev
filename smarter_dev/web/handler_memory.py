"""Per-handler persistent memory — a small key/value dict that survives fires.

A handler script gets no state of its own between firings; this gives it one.
The host owns the dict (loaded from the handler row before the fire, saved after);
the script reads and writes it through metered external functions, mirroring how
every other side effect in the runtime works. Keeping the host as the owner means
a sandbox mutation can never corrupt host state — the script only changes memory
by asking, and only ``set``/``delete`` persist.

Two rails: values must be JSON-serializable (it lands in a JSON column) and the
whole blob is size-capped. A size breach raises :class:`CapExceeded` so it flows
through the runtime's normal cap path; a non-serializable value is an author bug,
so it raises ``ValueError`` and fails the fire loud.
"""

from __future__ import annotations

import copy
import json

from smarter_dev.web.handler_budget import CapExceeded

# Cap the serialized blob. Memory is for counters, seen-sets, and timestamps —
# not bulk storage — so a small ceiling keeps a runaway handler from bloating the
# row (and the per-fire load/save) without being limiting in practice.
MAX_MEMORY_BYTES = 16 * 1024


class HandlerMemory:
    """Host-owned key/value store for one handler, snapshot-loaded per fire.

    Writes copy-on-write so a rejected ``set`` (non-serializable or over-cap)
    leaves the prior state intact. ``dirty`` lets the caller skip a DB write when
    a fire never touched memory — the common case for a busy message handler.
    """

    def __init__(self, initial: dict | None = None, max_bytes: int = MAX_MEMORY_BYTES):
        self._data: dict = dict(initial or {})
        self._max_bytes = max_bytes
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    def get(self, key: str, default=None):
        return self._data.get(str(key), default)

    def set(self, key: str, value) -> bool:
        """Store ``value`` under ``key``. Fails loud on non-JSON or over-cap."""
        candidate = dict(self._data)
        candidate[str(key)] = value
        try:
            encoded = json.dumps(candidate)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"memory values must be JSON-serializable (str/int/float/bool/"
                f"None/list/dict): {exc}"
            ) from exc
        if len(encoded.encode("utf-8")) > self._max_bytes:
            raise CapExceeded(
                "memory_size",
                f"handler memory would exceed {self._max_bytes} bytes",
            )
        self._data = candidate
        self._dirty = True
        return True

    def delete(self, key: str) -> bool:
        """Remove ``key`` if present. Returns whether anything was removed."""
        key = str(key)
        if key not in self._data:
            return False
        candidate = dict(self._data)
        del candidate[key]
        self._data = candidate
        self._dirty = True
        return True

    def all(self) -> dict:
        """A deep copy of the whole store (so the script can iterate it safely)."""
        return copy.deepcopy(self._data)

    def snapshot(self) -> dict:
        """The current state, for persisting back to the handler row."""
        return copy.deepcopy(self._data)
