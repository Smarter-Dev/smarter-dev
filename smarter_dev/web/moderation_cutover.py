"""The moderation cutover (#240): one guild from the legacy actors to the
coordinator.

``switch`` writes the guild's ``moderation_ownership`` row and nothing else:
owner ``coordinator`` and the next generation, in one transaction. The bot
reads the ownership row with every message, so the coordinator owns the
guild from the next message after the commit.

What the coordinator then does is the guild's moderation policy (#241). A
guild without one gets nothing until the import
(``scripts/moderation_policy_import.py``) writes revision 1, with every rule
off; an admin turns rules on from the Moderation page.

Until #242 the switch also turned the legacy actors' own switches off
(the spam engine and ring, the attachment filter, the scam-banner and
edit-ping-catch handlers), patched the live Disboard bump tracker to leave
members' chatter to the coordinator, and saved what it changed as
``legacy_state``. Those actors and their config tables are gone, so there is
nothing left to switch off, and the bump channel is the policy's
``bump_channel`` rule (off after the import): the Disboard handler is left as
it is. An existing row's ``rules`` and ``legacy_state`` stay as the record of
the first cutover and are neither read nor written. There is no rollback.

``status`` only reads.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy import select

from smarter_dev.bot.moderation_coordinator import ownership as own
from smarter_dev.web.models import ModerationOwnership
from smarter_dev.web.models import ModerationPolicy

SCAM_BANNER_HANDLER = "scam-banner"
EDIT_PING_HANDLER = "edit-ping-catch"
DISBOARD_SLUG = "disboard-bumping"
#: The handlers the coordinator replaced outright (the handler API refuses
#: to recreate them under these names).
RETIRED_HANDLERS = (SCAM_BANNER_HANDLER, EDIT_PING_HANDLER)


class CutoverRefused(Exception):
    """The switch was refused; nothing was changed."""


async def _ownership(session, guild_id: str, *, lock: bool = False):
    query = select(ModerationOwnership).where(ModerationOwnership.guild_id == guild_id)
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def status(session, guild_id: str) -> dict[str, Any]:
    """The guild's ownership row and policy revision, as they are now. Reads
    only."""
    ownership = await _ownership(session, guild_id)
    revision = (
        await session.execute(
            select(ModerationPolicy.revision).where(
                ModerationPolicy.guild_id == guild_id
            )
        )
    ).scalar_one_or_none()
    return {
        "guild_id": guild_id,
        "owner": ownership.owner if ownership else own.LEGACY,
        "generation": ownership.generation if ownership else 0,
        "legacy_state_saved": bool(ownership and ownership.legacy_state),
        "changed_by": ownership.changed_by if ownership else None,
        "changed_at": ownership.changed_at.isoformat()
        if ownership and ownership.changed_at
        else None,
        "policy_revision": revision,
    }


async def switch(session, guild_id: str, *, by: str) -> dict[str, Any]:
    """Hand the guild to the coordinator. Commits, or raises CutoverRefused."""
    ownership = await _ownership(session, guild_id, lock=True)
    if ownership is not None and ownership.owner == own.COORDINATOR:
        raise CutoverRefused(
            f"guild {guild_id} is already owned by the coordinator "
            f"(generation {ownership.generation}); nothing changed"
        )
    if ownership is None:
        ownership = ModerationOwnership(guild_id=guild_id, generation=0)
        session.add(ownership)
    ownership.owner = own.COORDINATOR
    ownership.generation = int(ownership.generation or 0) + 1
    ownership.changed_by = by[:100]
    ownership.changed_at = datetime.now(UTC)
    await session.commit()
    return await status(session, guild_id)
