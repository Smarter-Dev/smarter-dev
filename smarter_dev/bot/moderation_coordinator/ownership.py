"""Who moderates a guild: the legacy actors or the coordinator.

One row per switched guild (``moderation_ownership``), written only by
``scripts/moderation_cutover.py``. No row means legacy: the coordinator
judges in shadow and acts on nothing.

``generation`` goes up on every switch. The coordinator reads it with the
guild's policy and reads it again just before acting; a decision made under
an older generation (a rollback landed while it was being judged) does not
act. That is the fence: a rollback changes the owner for new events, and
what the coordinator already did is left to the gateway's reconciliation.

The same read carries the guild's moderation policy revision
(``moderation_policies.revision``, #241): the bot reads it with every message,
so a saved revision reaches the next message, and the coordinator checks it
again before it acts.

Nothing here imports the bot runtime, so the cutover script can use it.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from smarter_dev.web.models import ModerationOwnership
from smarter_dev.web.models import ModerationPolicy
from smarter_dev.web.moderation_gateway.gateway import default_session_factory

LEGACY = "legacy"
COORDINATOR = "coordinator"

# The coordinator's rules, by the names its decisions use (``rules.py``).
SPAM_ENGINE = "spam_engine"
ATTACHMENT_FILTER = "attachment_filter"
SPAM_RING = "spam_ring"
SCAM_GATE = "scam_gate"
EDIT_PING = "edit_ping"
BUMP_CHANNEL = "bump_channel"
#: New with the coordinator (Zech, Oct 23 22:36), no legacy actor behind it:
#: a member's ``@everyone``/``@here`` is removed.
MASS_MENTION = "mass_mention"
RULES = (
    SPAM_ENGINE,
    ATTACHMENT_FILTER,
    SPAM_RING,
    SCAM_GATE,
    EDIT_PING,
    BUMP_CHANNEL,
    MASS_MENTION,
)


@dataclass(frozen=True, slots=True)
class Ownership:
    guild_id: str
    owner: str = LEGACY
    generation: int = 0
    rules: frozenset[str] = frozenset()
    #: The guild's policy revision; None while it has no policy.
    policy_revision: int | None = None

    @property
    def coordinator_acts(self) -> bool:
        return self.owner == COORDINATOR


def ownership_from(
    row: ModerationOwnership | None,
    guild_id: str,
    policy_revision: int | None = None,
) -> Ownership:
    if row is None:
        return Ownership(guild_id=guild_id, policy_revision=policy_revision)
    return Ownership(
        guild_id=guild_id,
        owner=row.owner,
        generation=int(row.generation or 0),
        rules=frozenset(str(rule) for rule in (row.rules or ()) if rule in RULES),
        policy_revision=policy_revision,
    )


async def read_ownership(session, guild_id: str) -> Ownership:
    """The ownership row and the policy revision, in one round trip."""
    revision = (
        select(ModerationPolicy.revision)
        .where(ModerationPolicy.guild_id == guild_id)
        .scalar_subquery()
    )
    found = (
        await session.execute(
            select(ModerationOwnership, revision).where(
                ModerationOwnership.guild_id == guild_id
            )
        )
    ).one_or_none()
    if found is not None:
        row, policy_revision = found
        return ownership_from(row, guild_id, policy_revision)
    # No ownership row (a guild never switched): the revision alone.
    policy_revision = (await session.execute(select(revision))).scalar_one_or_none()
    return ownership_from(None, guild_id, policy_revision)


async def load_ownership(guild_id: str) -> Ownership:
    """The guild's ownership now; raises when the database cannot be read."""
    async with default_session_factory()() as session:
        return await read_ownership(session, guild_id)
