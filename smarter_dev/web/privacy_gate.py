"""The opt-out gate for web code that keeps Discord-sourced content (#100).

The opt-out covers Discord, where the bot pulls a member's messages in by
itself; it does not cover the website (web chat, site conversations, what a
signed-in person does themselves). Web code still writes and summarises
Discord-sourced content: memory notes from the ``remember`` tool, the nightly
dream, proactive history, handler memories from Discord dispatch. Those
writers ask this gate before persisting anything about or from a person.

The gate reads the block list from the database, so it is never stale, and
answers exactly as the bot's :class:`BlockedUsersCache` does, opt-in cutoffs
included. Opting out deletes nothing already held.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.bot.privacy.blocked_users import BlockedUsersCache
from smarter_dev.bot.privacy.blocked_users import redact_blocked_mentions
from smarter_dev.shared.privacy_purge import BLOCKED_PLACEHOLDER
from smarter_dev.web.chat_bot_purge import read_blocked_users

__all__ = ["BLOCKED_PLACEHOLDER", "OptOutGate", "load_gate"]


class OptOutGate:
    """One read of the block list, to check a batch of writes against."""

    def __init__(self, blocked: BlockedUsersCache) -> None:
        self._blocked = blocked

    def is_blocked(self, user_id: Any, message_id: Any = None) -> bool:
        return self._blocked.is_blocked(user_id, message_id)

    def refuses(self, *user_ids: Any) -> bool:
        """True when a write about or from any of ``user_ids`` must not
        happen. ``None`` or empty ids are ignored."""
        return any(u not in (None, "") and self._blocked.is_blocked(u) for u in user_ids)

    def redact(self, text: str) -> str:
        """``text`` with every blocked person's id or mention replaced."""
        return redact_blocked_mentions(text, self._blocked)

    def carries_blocked_id(self, text: str) -> bool:
        return self.redact(text) != text

    def scrub(self, value: Any) -> Any:
        """A JSON value with blocked ids redacted in every string and number,
        and every mapping entry whose key carries a blocked id left out."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, int) and not isinstance(value, bool):
            # A snowflake kept as a number; replaced by the same text a
            # string id gets.
            redacted = self.redact(str(value))
            return value if redacted == str(value) else redacted
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, dict):
            return {
                key: self.scrub(item)
                for key, item in value.items()
                if not self.carries_blocked_id(str(key))
            }
        return value

    def scrub_memory(self, new: dict, previous: dict | None) -> dict:
        """What a handler's memory may be saved as (#100).

        Nothing new about someone on the list is written: a top-level key
        that carries their id keeps its stored value (or stays absent), and
        everything else goes through :meth:`scrub`, so their id does not
        survive elsewhere in what the script saves.
        """
        previous = previous or {}
        out: dict = {}
        for key, value in new.items():
            if self.carries_blocked_id(str(key)):
                if key in previous:
                    out[key] = previous[key]
                continue
            out[key] = self.scrub(value)
        return out

    @property
    def revision(self) -> int | None:
        return self._blocked.revision


async def load_gate(session: AsyncSession) -> OptOutGate:
    """The gate over the block list as it is in the database now."""
    listing = await read_blocked_users(session)
    cache = BlockedUsersCache()
    cache.load(listing.revision, listing.user_ids, listing.read_from)
    return OptOutGate(cache)
