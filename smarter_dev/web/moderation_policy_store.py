"""Reading and saving a guild's moderation policy, and who has loaded it (#241).

A save writes the next revision to ``moderation_policies`` and appends it to
``moderation_policy_revisions`` in one transaction, and only on top of the
revision it was edited from (:class:`StaleRevision` otherwise): two admins
saving at once cannot silently undo each other. The bot reads the revision
with every message (:func:`~smarter_dev.bot.moderation_coordinator.ownership.
read_ownership`), so the next message runs on the saved one.

``moderation_policy_loads`` is each process's own account of what it runs
on: one row per process and guild, written when the process loads a
revision and on its heartbeat, and pruned once nobody has seen it for
:data:`LOAD_ROW_MAX_AGE`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy import select

from smarter_dev.shared.moderation_policy import PolicyDocument
from smarter_dev.shared.moderation_policy import changed_fields
from smarter_dev.shared.moderation_policy import validate
from smarter_dev.web.models import ModerationPolicy
from smarter_dev.web.models import ModerationPolicyLoad
from smarter_dev.web.models import ModerationPolicyRevision

SOURCE_IMPORT = "import"
SOURCE_ADMIN = "admin"
SOURCE_API = "api"
#: A process not seen for this long is gone (a replaced pod).
LOAD_ROW_MAX_AGE = timedelta(days=1)


class StaleRevision(Exception):
    """The policy moved on since it was read; nothing was saved."""

    def __init__(self, current: int | None, base: int | None) -> None:
        super().__init__(
            f"the policy is at revision {current}, not {base}: reload it and "
            "make the change again"
        )
        self.current = current
        self.base = base


@dataclass(frozen=True, slots=True)
class StoredPolicy:
    guild_id: str
    revision: int
    document: PolicyDocument
    updated_by: str | None
    updated_at: datetime | None


async def read_policy(session, guild_id: str) -> StoredPolicy | None:
    """The guild's current revision, validated; None without a policy.

    A stored document this module no longer accepts raises
    :class:`~smarter_dev.shared.moderation_policy.PolicyError`: the reader
    fails closed rather than guessing."""
    row = await session.get(ModerationPolicy, guild_id)
    if row is None:
        return None
    return StoredPolicy(
        guild_id=guild_id,
        revision=int(row.revision),
        document=validate(row.document),
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


async def save_policy(
    session,
    guild_id: str,
    document,
    *,
    base_revision: int | None,
    by: str,
    source: str,
) -> int:
    """Save ``document`` as the next revision and commit; returns it.

    ``base_revision`` is the revision the change was made from (None: the
    guild has no policy yet). Raises :class:`StaleRevision` when that is not
    the current one, and ``PolicyError`` for a document :func:`validate`
    refuses; nothing is written either way."""
    document = validate(document)
    row = (
        await session.execute(
            select(ModerationPolicy)
            .where(ModerationPolicy.guild_id == guild_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    current = int(row.revision) if row is not None else None
    if current != base_revision:
        await session.rollback()
        raise StaleRevision(current, base_revision)
    stored = document.as_json()
    previous = row.document if row is not None else None
    revision = (current or 0) + 1
    if row is None:
        row = ModerationPolicy(guild_id=guild_id, revision=revision, document=stored)
        session.add(row)
    else:
        row.revision = revision
        row.document = stored
    row.updated_by = by[:100]
    row.updated_at = datetime.now(UTC)
    session.add(
        ModerationPolicyRevision(
            guild_id=guild_id,
            revision=revision,
            document=stored,
            changed_by=by[:100],
            source=source,
            changed_fields=changed_fields(previous, stored),
        )
    )
    await session.commit()
    return revision


async def revisions(session, guild_id: str, limit: int = 20) -> list:
    """The guild's latest revisions, newest first."""
    return list(
        (
            await session.execute(
                select(ModerationPolicyRevision)
                .where(ModerationPolicyRevision.guild_id == guild_id)
                .order_by(ModerationPolicyRevision.revision.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def record_loads(
    session,
    process: str,
    loaded: Mapping[str, tuple[int, datetime]],
    now: datetime,
) -> None:
    """This process's revision per guild, seen ``now``; prunes rows nobody has
    seen for :data:`LOAD_ROW_MAX_AGE`. Commits.

    Only this process writes its rows, so a read then a write is no race."""
    if loaded:
        existing = {
            row.guild_id: row
            for row in (
                await session.execute(
                    select(ModerationPolicyLoad).where(
                        ModerationPolicyLoad.process == process,
                        ModerationPolicyLoad.guild_id.in_(list(loaded)),
                    )
                )
            ).scalars()
        }
        for guild_id, (revision, loaded_at) in loaded.items():
            row = existing.get(guild_id)
            if row is None:
                session.add(
                    ModerationPolicyLoad(
                        process=process,
                        guild_id=guild_id,
                        revision=revision,
                        loaded_at=loaded_at,
                        seen_at=now,
                    )
                )
            else:
                row.revision = revision
                row.loaded_at = loaded_at
                row.seen_at = now
        # Written before the prune reads seen_at: a row this beat refreshes
        # is never pruned on its old value.
        await session.flush()
    await session.execute(
        delete(ModerationPolicyLoad).where(
            ModerationPolicyLoad.seen_at < now - LOAD_ROW_MAX_AGE
        )
    )
    await session.commit()


async def loads(session, guild_id: str) -> list:
    """Every process's row for the guild, most recently seen first."""
    return list(
        (
            await session.execute(
                select(ModerationPolicyLoad)
                .where(ModerationPolicyLoad.guild_id == guild_id)
                .order_by(ModerationPolicyLoad.seen_at.desc())
            )
        ).scalars()
    )
