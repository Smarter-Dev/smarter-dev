"""Shared channel-history paging + bulk-delete core for message purging.

Reused by the AI triage tool (``mod_tools.purge_messages``) and the ``/purge``
slash command so the 14-day bulk-delete rule and the single-vs-bulk delete
choice live in exactly one place. See
docs/v2/feature-parity/automated-and-command-moderation.md §4.2.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Discord epoch for snowflake timestamp decoding.
DISCORD_EPOCH_MS = 1420070400000

# Discord's bulk-delete endpoint rejects messages older than 14 days.
BULK_DELETE_MAX_AGE = timedelta(days=14)


def snowflake_to_datetime(snowflake: str | int) -> datetime:
    """Decode a Discord snowflake ID to its creation timestamp."""
    timestamp_ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)


@dataclass(frozen=True)
class PurgeSelection:
    """The outcome of walking channel history for purgeable messages.

    ``skipped_too_old`` counts messages that matched the filter but were older
    than Discord's 14-day bulk-delete limit, so callers can report the shortfall
    instead of failing.
    """

    message_ids: list[int]
    skipped_too_old: int


async def select_purgeable_messages(
    messages: AsyncIterable,
    *,
    count: int,
    user_id: str | None = None,
    now: datetime | None = None,
) -> PurgeSelection:
    """Walk a channel-history async iterator selecting up to ``count`` message
    ids to delete.

    ``user_id`` restricts selection to that author; ``None`` selects any author.
    Messages older than 14 days are skipped (Discord's bulk-delete rejects them)
    and counted in ``skipped_too_old``.
    """
    cutoff = (now or datetime.now(timezone.utc)) - BULK_DELETE_MAX_AGE
    message_ids: list[int] = []
    skipped_too_old = 0
    async for message in messages:
        if user_id is not None and str(message.author.id) != user_id:
            continue
        if snowflake_to_datetime(message.id) < cutoff:
            skipped_too_old += 1
            continue
        message_ids.append(message.id)
        if len(message_ids) >= count:
            break
    return PurgeSelection(message_ids=message_ids, skipped_too_old=skipped_too_old)


async def delete_selected_messages(rest, channel_id: int, message_ids: list[int]) -> None:
    """Delete the selected messages, using single-delete for one and Discord's
    bulk-delete (2-100) for many. A no-op for an empty selection."""
    if not message_ids:
        return
    if len(message_ids) == 1:
        await rest.delete_message(channel_id, message_ids[0])
    else:
        await rest.delete_messages(channel_id, message_ids)
