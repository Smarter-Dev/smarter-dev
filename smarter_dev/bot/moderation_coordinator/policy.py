"""A guild's moderation policy as the coordinator reads it.

The coordinator has no settings of its own yet (step 5 adds one versioned
policy). It reads what each legacy actor reads, so it judges with exactly the
configuration that actor is running under, and a rule whose actor is off is
off here too: a disabled or missing policy never silently activates.

* spam engine and spam ring: ``moderation_filter_configs`` (the ring runs on
  the model defaults when the guild has no row, as it does today);
* attachment filter: ``attachment_filter_configs``;
* scam gate, edit-ping and the Disboard channel rule: whether the guild's
  ``scam-banner``, ``edit-ping-catch`` and ``disboard-bump-tracker`` admin
  handlers are installed and enabled, and the bump channel from the last
  one's scope.

The scam gate is a port of the reference copy of scam-banner
(``scripts/handler_scripts/scam-banner.monty``). The live script is a database
row that can drift from that copy, so its sha256 is compared with
:data:`SCAM_BANNER_REFERENCE_SHA256` and the result is kept with every
decision: a shadow comparison against a drifted live script says so.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace

from sqlalchemy import select

from smarter_dev.bot.spam_engine import SpamEngineSettings
from smarter_dev.bot.spam_ring import SpamRingSettings
from smarter_dev.web.crud import AttachmentFilterConfigOperations
from smarter_dev.web.crud import ModerationFilterConfigOperations
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.moderation_gateway.gateway import default_session_factory

logger = logging.getLogger(__name__)

#: sha256 of ``scripts/handler_scripts/scam-banner.monty``, the script the
#: scam gate was ported from. A test fails when the file changes without this
#: (and the rules) changing with it.
SCAM_BANNER_REFERENCE_SHA256 = (
    "e8ba764de2d9e026ef96c774c4ad9eadc90f0538ee28f2207982a3edeb0af623"
)

SCAM_BANNER_HANDLER = "scam-banner"
EDIT_PING_HANDLER = "edit-ping-catch"
BUMP_TRACKER_HANDLER = "disboard-bump-tracker"

#: How long one guild's policy snapshot is reused. Config edits reach the
#: shadow within this; the cutover replaces it with the revision check.
POLICY_TTL_SECONDS = 60.0


def script_sha256(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AttachmentPolicy:
    ignored_extensions: tuple[str, ...]
    warn_extensions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GuildPolicy:
    """Which rules run for a guild, and with what settings. ``None`` is off."""

    guild_id: str
    spam: SpamEngineSettings | None = None
    attachments: AttachmentPolicy | None = None
    ring: SpamRingSettings | None = None
    scam_gate: bool = False
    #: Whether the live scam-banner script is the reference copy; None when
    #: there is no live script to compare.
    scam_banner_matches_reference: bool | None = None
    edit_ping: bool = False
    #: The live handlers' scopes; empty means every channel, as for handlers.
    scam_gate_channel_ids: frozenset[str] = frozenset()
    edit_ping_channel_ids: frozenset[str] = frozenset()
    bump_channel_ids: frozenset[str] = frozenset()
    staff_exempt_role_ids: tuple[str, ...] = ()
    loaded_at: float = field(default=0.0, compare=False)

    def as_record(self) -> dict:
        return {
            "spam_engine": self.spam is not None,
            "attachment_filter": self.attachments is not None,
            "spam_ring": self.ring is not None,
            "scam_gate": self.scam_gate,
            "scam_banner_matches_reference": self.scam_banner_matches_reference,
            "edit_ping": self.edit_ping,
            "bump_channel": bool(self.bump_channel_ids),
        }


async def load_policy(guild_id: str) -> GuildPolicy:
    """Read the guild's policy; raises when the database cannot be read."""
    async with default_session_factory()() as session:
        config = await ModerationFilterConfigOperations().get_config(session, guild_id)
        attachment_config = await AttachmentFilterConfigOperations().get_config(
            session, guild_id
        )
        handlers = list(
            (
                await session.execute(
                    select(
                        AdminHandler.name,
                        AdminHandler.enabled,
                        AdminHandler.script,
                        AdminHandler.channel_ids,
                    ).where(
                        AdminHandler.guild_id == guild_id,
                        AdminHandler.name.in_(
                            (
                                SCAM_BANNER_HANDLER,
                                EDIT_PING_HANDLER,
                                BUMP_TRACKER_HANDLER,
                            )
                        ),
                    )
                )
            ).all()
        )
        spam = (
            SpamEngineSettings.from_config(config)
            if config is not None and config.spam_engine_enabled
            else None
        )
        # The ring runs on the model defaults without a row (spam_ring.py).
        ring_config = config or ModerationFilterConfig.get_defaults(guild_id)
        ring = (
            SpamRingSettings.from_config(ring_config)
            if ring_config.spam_ring_enabled
            else None
        )
        attachments = (
            AttachmentPolicy(
                ignored_extensions=tuple(attachment_config.ignored_extensions or ()),
                warn_extensions=tuple(attachment_config.warn_extensions or ()),
            )
            if attachment_config is not None and attachment_config.is_active
            else None
        )
        staff_roles = tuple(
            str(role_id)
            for role_id in ((config.staff_exempt_role_ids or []) if config else [])
        )
    by_name = {row.name: row for row in handlers}
    scam = by_name.get(SCAM_BANNER_HANDLER)
    edit = by_name.get(EDIT_PING_HANDLER)
    bump = by_name.get(BUMP_TRACKER_HANDLER)
    return GuildPolicy(
        guild_id=guild_id,
        spam=spam,
        attachments=attachments,
        ring=ring,
        scam_gate=bool(scam and scam.enabled),
        scam_banner_matches_reference=(
            script_sha256(scam.script) == SCAM_BANNER_REFERENCE_SHA256
            if scam is not None
            else None
        ),
        edit_ping=bool(edit and edit.enabled),
        scam_gate_channel_ids=_scope(scam),
        edit_ping_channel_ids=_scope(edit),
        bump_channel_ids=frozenset(
            str(channel_id) for channel_id in (bump.channel_ids or [])
        )
        if bump is not None and bump.enabled
        else frozenset(),
        staff_exempt_role_ids=staff_roles,
        loaded_at=time.time(),
    )


def _scope(handler) -> frozenset[str]:
    if handler is None:
        return frozenset()
    return frozenset(str(channel_id) for channel_id in (handler.channel_ids or []))


def in_scope(channel_ids: frozenset[str], channel_id: str) -> bool:
    """A handler's scope test: no channels listed means every channel.

    The handler dispatch scopes a thread's message by its parent channel; the
    shadow sees the thread's own id, so a scoped handler's threads read as out
    of scope here. Both live handlers are guild-wide.
    """
    return not channel_ids or channel_id in channel_ids


class PolicyCache:
    """One snapshot per guild, reloaded after :data:`POLICY_TTL_SECONDS`.

    A failed reload keeps no stale snapshot: it raises, and the message is
    recorded as not evaluated rather than judged under old settings.
    """

    def __init__(self, loader=load_policy, ttl: float = POLICY_TTL_SECONDS) -> None:
        self._loader = loader
        self._ttl = ttl
        self._policies: dict[str, GuildPolicy] = {}

    async def get(self, guild_id: str, now: float | None = None) -> GuildPolicy:
        now = time.time() if now is None else now
        cached = self._policies.get(guild_id)
        if cached is not None and now - cached.loaded_at < self._ttl:
            return cached
        self._policies.pop(guild_id, None)
        policy = await self._loader(guild_id)
        if not policy.loaded_at:
            policy = replace(policy, loaded_at=now)
        self._policies[guild_id] = policy
        if policy.scam_banner_matches_reference is False and (
            cached is None or cached.scam_banner_matches_reference is not False
        ):
            logger.warning(
                "The live scam-banner script in guild %s is not the reference "
                "copy the coordinator's scam gate was ported from",
                guild_id,
            )
        return policy
