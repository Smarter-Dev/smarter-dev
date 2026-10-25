"""A guild's moderation policy as the coordinator reads it.

Everything comes from the guild's moderation policy (``moderation_policies``,
#241), its current revision (:mod:`smarter_dev.shared.moderation_policy`):
which rules run and in which mode, their scopes and settings, the protected
roles, the scam ladder, the risk windows, the provisional hold, the
classifier's limits and where reports go. A rule in ``evaluate`` is judged and
recorded but never acted on; only ``enforce`` acts, and only while the
coordinator owns the guild (:mod:`.ownership`).

**A guild without a policy gets nothing**: no rule runs, nothing is enforced
and nothing is reported. The old per-actor config tables the coordinator used
to fall back on were dropped in #242. A guild the cutover moves to the
coordinator gets revision 1 from the import with every rule off; an admin
turns rules on from the Moderation page.

The scam gate is a port of scam-banner's reference script, which was removed
with the legacy actors after the #240 cutover.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import timedelta

from smarter_dev.bot.moderation_coordinator import ownership as own
from smarter_dev.bot.moderation_coordinator.ownership import Ownership
from smarter_dev.bot.moderation_coordinator.ownership import read_ownership
from smarter_dev.bot.spam_engine import SpamEngineSettings
from smarter_dev.bot.spam_engine import SpamThresholds
from smarter_dev.bot.spam_ring import SpamRingSettings
from smarter_dev.bot.spam_ring import clamp_timeout_seconds
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web.moderation_gateway.gateway import default_session_factory
from smarter_dev.web.moderation_policy_store import read_policy

logger = logging.getLogger(__name__)

#: How long one guild's policy snapshot is reused at most. A saved revision
#: is picked up on the next message anyway (the revision is read with the
#: ownership).
POLICY_TTL_SECONDS = 60.0
#: The settings of a guild without a policy (nothing of it runs).
DEFAULT_DOCUMENT = mp.default_document()


@dataclass(frozen=True, slots=True)
class AttachmentPolicy:
    ignored_extensions: tuple[str, ...]
    warn_extensions: tuple[str, ...]
    #: The guild's own notice texts (``{user}``, ``{extension}``,
    #: ``{filename}``); None uses the filter's defaults.
    delete_message: str | None = None
    warn_message: str | None = None


@dataclass(frozen=True, slots=True)
class GuildPolicy:
    """Which rules run for a guild, and with what settings. ``None`` is off."""

    guild_id: str
    spam: SpamEngineSettings | None = None
    attachments: AttachmentPolicy | None = None
    ring: SpamRingSettings | None = None
    scam_gate: bool = False
    edit_ping: bool = False
    #: The rules' scopes; empty means every channel, as for handlers.
    scam_gate_channel_ids: frozenset[str] = frozenset()
    edit_ping_channel_ids: frozenset[str] = frozenset()
    bump_channel_ids: frozenset[str] = frozenset()
    #: A member's ``@everyone``/``@here`` is removed; only while acting.
    mass_mention: bool = False
    staff_exempt_role_ids: tuple[str, ...] = ()
    #: Who moderates the guild. The coordinator acts only as ``coordinator``,
    #: and only while the generation it decided under is still current.
    owner: str = own.LEGACY
    generation: int = 0
    #: Where incident reports go, and the role a spam mute pings.
    report_channel_id: str | None = None
    #: The policy revision this snapshot is; None: the guild has no policy.
    revision: int | None = None
    #: The policy's settings; the code's defaults without a policy.
    document: mp.PolicyDocument = DEFAULT_DOCUMENT
    #: The rules that may act (``enforce``); none without a policy.
    enforced: frozenset[str] = frozenset()
    #: Scopes of the other rules; empty is every channel.
    spam_channel_ids: frozenset[str] = frozenset()
    attachment_channel_ids: frozenset[str] = frozenset()
    ring_channel_ids: frozenset[str] = frozenset()
    mass_mention_channel_ids: frozenset[str] = frozenset()
    loaded_at: float = field(default=0.0, compare=False)

    @property
    def acting(self) -> bool:
        return self.owner == own.COORDINATOR

    def enforces(self, rule: str) -> bool:
        """Whether ``rule``'s findings are carried out (the coordinator owns
        the guild, and the policy has the rule in ``enforce``)."""
        return self.acting and rule in self.enforced

    def allows(self, sanction: str) -> bool:
        """Whether the policy lets automation use this sanction
        (``warn``/``timeout``/``delete``/``ban``; ``kick`` never).

        Without a policy no rule is enforced (:meth:`enforces`), so this
        lets nothing act there."""
        if self.revision is None:
            return True
        return sanction in self.document.sanctions.allowed

    def provisional_allowed(self) -> bool:
        """Whether a high-signal scam may be held while it is judged."""
        return (
            self.document.provisional.enabled
            and self.enforces(own.SCAM_GATE)
            and self.allows(mp.TIMEOUT)
        )

    def as_record(self) -> dict:
        return {
            "owner": self.owner,
            "generation": self.generation,
            "revision": self.revision,
            "enforced": sorted(self.enforced),
            "spam_engine": self.spam is not None,
            "attachment_filter": self.attachments is not None,
            "spam_ring": self.ring is not None,
            "scam_gate": self.scam_gate,
            "edit_ping": self.edit_ping,
            "bump_channel": bool(self.bump_channel_ids),
            "mass_mention": self.mass_mention,
        }


async def load_policy(guild_id: str) -> GuildPolicy:
    """Read the guild's policy; raises when the database cannot be read (or
    its stored policy is one this code does not accept).

    A guild without a policy gets :func:`no_policy`: nothing runs."""
    async with default_session_factory()() as session:
        ownership = await read_ownership(session, guild_id)
        stored = await read_policy(session, guild_id)
    if stored is None:
        return no_policy(guild_id, ownership=ownership)
    return from_document(
        guild_id,
        stored.document,
        revision=stored.revision,
        ownership=ownership,
    )


def no_policy(
    guild_id: str, *, ownership: Ownership, loaded_at: float | None = None
) -> GuildPolicy:
    """A guild without a policy: no rule runs, nothing is enforced or
    reported, whoever owns the guild."""
    return GuildPolicy(
        guild_id=guild_id,
        owner=ownership.owner,
        generation=ownership.generation,
        loaded_at=time.time() if loaded_at is None else loaded_at,
    )


def from_document(
    guild_id: str,
    document: mp.PolicyDocument,
    *,
    revision: int,
    ownership: Ownership,
    loaded_at: float | None = None,
) -> GuildPolicy:
    """The coordinator's view of one policy revision.

    A rule ``off`` (or the guild-wide evaluation switch off) does not run; a
    rule in ``evaluate`` runs and is recorded; ``enforce`` acts. The report
    channel is None when reporting is switched off."""
    rules = document.rules
    running = {rule for rule in mp.RULES if document.mode(rule) != mp.OFF}
    enforced = frozenset(rule for rule in running if document.mode(rule) == mp.ENFORCE)
    protected = tuple(document.protected_role_ids)
    destinations = document.destinations
    report_channel_id = (
        destinations.report_channel_id if document.controls.report else None
    )
    spam = None
    if own.SPAM_ENGINE in running:
        settings = rules.spam_engine
        spam = SpamEngineSettings(
            enabled=True,
            staff_exempt_role_ids=protected,
            scam_link_domains=tuple(settings.scam_link_domains),
            thresholds=SpamThresholds(
                message_rate_threshold=settings.message_rate_threshold,
                message_rate_window_seconds=settings.message_rate_window_seconds,
                channel_spread_threshold=settings.channel_spread_threshold,
                channel_spread_window_seconds=settings.channel_spread_window_seconds,
                duplicate_message_min_length=settings.duplicate_message_min_length,
                duplicate_message_window_seconds=(
                    settings.duplicate_message_window_seconds
                ),
                mass_mention_window_seconds=settings.mass_mention_window_seconds,
                warning_reoffense_window_seconds=(
                    settings.warning_reoffense_window_seconds
                ),
                mute_duration_seconds=settings.mute_duration_seconds,
            ),
            mod_alert_channel_id=report_channel_id,
            mod_ping_role_id=destinations.mod_ping_role_id,
            scam_log_channel_id=settings.scam_log_channel_id,
            report_channel_id=report_channel_id,
        )
    ring = None
    if own.SPAM_RING in running:
        settings = rules.spam_ring
        ring = SpamRingSettings(
            enabled=True,
            new_member_window=timedelta(days=settings.new_member_days),
            timeout_seconds=clamp_timeout_seconds(settings.timeout_seconds),
            lookback=timedelta(minutes=settings.lookback_minutes),
            followup=timedelta(minutes=settings.followup_minutes),
            report_channel_id=report_channel_id,
            staff_exempt_role_ids=protected,
        )
    attachments = None
    if own.ATTACHMENT_FILTER in running:
        settings = rules.attachment_filter
        attachments = AttachmentPolicy(
            ignored_extensions=tuple(settings.ignored_extensions),
            warn_extensions=tuple(settings.warn_extensions),
            delete_message=settings.delete_message,
            warn_message=settings.warn_message,
        )

    def channels(rule: str) -> frozenset[str]:
        return frozenset(document.rule(rule).channels)

    return GuildPolicy(
        guild_id=guild_id,
        spam=spam,
        attachments=attachments,
        ring=ring,
        scam_gate=own.SCAM_GATE in running,
        edit_ping=own.EDIT_PING in running,
        scam_gate_channel_ids=channels(own.SCAM_GATE),
        edit_ping_channel_ids=channels(own.EDIT_PING),
        # The bump channels are the rule: none listed is no bump channel.
        bump_channel_ids=channels(own.BUMP_CHANNEL)
        if own.BUMP_CHANNEL in running
        else frozenset(),
        mass_mention=own.MASS_MENTION in running,
        staff_exempt_role_ids=protected,
        owner=ownership.owner,
        generation=ownership.generation,
        report_channel_id=report_channel_id,
        revision=revision,
        document=document,
        enforced=enforced,
        spam_channel_ids=channels(own.SPAM_ENGINE),
        attachment_channel_ids=channels(own.ATTACHMENT_FILTER),
        ring_channel_ids=channels(own.SPAM_RING),
        mass_mention_channel_ids=channels(own.MASS_MENTION),
        loaded_at=time.time() if loaded_at is None else loaded_at,
    )


def in_scope(channel_ids: frozenset[str], channel_id: str) -> bool:
    """A rule's scope test: no channels listed means every channel.

    The shadow sees a thread's own id, not its parent channel's, so a scoped
    rule's threads read as out of scope here.
    """
    return not channel_ids or channel_id in channel_ids


class PolicyCache:
    """One snapshot per guild, reloaded after :data:`POLICY_TTL_SECONDS`.

    With ``ownership`` (:func:`.ownership.load_ownership`, as the bot runs it)
    the guild's ownership and policy revision are read on every message (one
    round trip), and a snapshot from another generation or revision is
    reloaded at once: the moment the cutover hands the guild over, the
    coordinator is already acting, and a saved policy revision judges the
    very next message.

    A failed reload keeps no stale snapshot: it raises, and the message is
    recorded as not evaluated rather than judged under old settings.

    :meth:`loaded` is what this process runs on, for its
    ``moderation_policy_loads`` rows: each guild's revision and when this
    process first loaded it.
    """

    def __init__(
        self,
        loader=load_policy,
        ttl: float = POLICY_TTL_SECONDS,
        ownership=None,
    ) -> None:
        self._loader = loader
        self._ttl = ttl
        self._ownership = ownership
        self._policies: dict[str, GuildPolicy] = {}
        self._loaded: dict[str, tuple[int, float]] = {}

    def peek(self, guild_id: str) -> GuildPolicy | None:
        """The last snapshot, however old; None before the first load."""
        return self._policies.get(guild_id)

    def loaded(self) -> dict[str, tuple[int, float]]:
        """Per guild with a policy: the revision loaded and since when."""
        return dict(self._loaded)

    async def get(self, guild_id: str, now: float | None = None) -> GuildPolicy:
        now = time.time() if now is None else now
        cached = self._policies.get(guild_id)
        if cached is not None and now - cached.loaded_at < self._ttl:
            if self._ownership is None:
                return cached
            current = await self._ownership(guild_id)
            if (current.owner, current.generation, current.policy_revision) == (
                cached.owner,
                cached.generation,
                cached.revision,
            ):
                return cached
        self._policies.pop(guild_id, None)
        policy = await self._loader(guild_id)
        if not policy.loaded_at:
            policy = replace(policy, loaded_at=now)
        self._policies[guild_id] = policy
        self._note_revision(guild_id, policy, now)
        return policy

    def _note_revision(self, guild_id: str, policy: GuildPolicy, now: float) -> None:
        if policy.revision is None:
            self._loaded.pop(guild_id, None)
            return
        previous = self._loaded.get(guild_id)
        if previous is None or previous[0] != policy.revision:
            self._loaded[guild_id] = (policy.revision, now)
            logger.info(
                "Moderation policy for guild %s: revision %d loaded",
                guild_id,
                policy.revision,
            )
