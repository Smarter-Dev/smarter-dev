"""A guild's moderation policy as the coordinator reads it.

**With a policy** (``moderation_policies``, #241) everything comes from its
current revision (:mod:`smarter_dev.shared.moderation_policy`): which rules
run and in which mode, their scopes and settings, the protected roles, the
scam ladder, the risk windows, the provisional hold, the classifier's limits
and where reports go. A rule in ``evaluate`` is judged and recorded but never
acted on; only ``enforce`` acts, and only while the coordinator owns the
guild. The cutover's ``rules`` list is not read then: the policy's modes are.

**Without one**, as before the policy existed: the coordinator
reads what each legacy actor reads, so it judges with exactly the
configuration that actor is running under, and a rule whose actor is off is
off here too: a disabled or missing policy never silently activates.

* spam engine and spam ring: ``moderation_filter_configs`` (the ring runs on
  the model defaults when the guild has no row, as it does today);
* attachment filter: ``attachment_filter_configs``;
* scam gate, edit-ping and the Disboard channel rule: whether the guild's
  ``scam-banner``, ``edit-ping-catch`` and ``disboard-bump-tracker`` admin
  handlers are installed and enabled, and the bump channel from the last
  one's scope.

Once a guild is switched to the coordinator (:mod:`.ownership`), those legacy
toggles are off: the cutover turned them off. The rules then run from the
ownership row's rule list, still with each actor's own settings (thresholds,
file tiers, the ring's windows, the handlers' scopes).

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

from sqlalchemy import select

from smarter_dev.bot.moderation_coordinator import ownership as own
from smarter_dev.bot.moderation_coordinator.ownership import Ownership
from smarter_dev.bot.moderation_coordinator.ownership import read_ownership
from smarter_dev.bot.spam_engine import SpamEngineSettings
from smarter_dev.bot.spam_engine import SpamThresholds
from smarter_dev.bot.spam_ring import SpamRingSettings
from smarter_dev.bot.spam_ring import clamp_timeout_seconds
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web.crud import AttachmentFilterConfigOperations
from smarter_dev.web.crud import ModerationFilterConfigOperations
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.moderation_gateway.config import load_report_channel
from smarter_dev.web.moderation_gateway.gateway import default_session_factory
from smarter_dev.web.moderation_policy_store import read_policy

logger = logging.getLogger(__name__)

SCAM_BANNER_HANDLER = "scam-banner"
EDIT_PING_HANDLER = "edit-ping-catch"
BUMP_TRACKER_HANDLER = "disboard-bump-tracker"

#: How long one guild's policy snapshot is reused at most. A saved revision
#: is picked up on the next message (the revision is read with the
#: ownership); this only bounds edits to the old tables a guild without a
#: policy still runs on.
POLICY_TTL_SECONDS = 60.0
#: The defaults a guild without a policy runs on: the code's values.
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
    #: The live handlers' scopes; empty means every channel, as for handlers.
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
    #: The policy revision this snapshot is; None: no policy (the old tables).
    revision: int | None = None
    #: The policy's settings; the code's defaults without a policy.
    document: mp.PolicyDocument = DEFAULT_DOCUMENT
    #: The rules that may act (``enforce``); None without a policy, where
    #: every rule that runs acts while the coordinator owns the guild.
    enforced: frozenset[str] | None = None
    #: Scopes of the rules that had none before the policy; empty is every
    #: channel.
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
        return self.acting and (self.enforced is None or rule in self.enforced)

    def allows(self, sanction: str) -> bool:
        """Whether the policy lets automation use this sanction
        (``warn``/``timeout``/``delete``/``ban``; ``kick`` never)."""
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
            "enforced": None if self.enforced is None else sorted(self.enforced),
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
    its stored policy is one this code does not accept)."""
    async with default_session_factory()() as session:
        ownership = await read_ownership(session, guild_id)
        stored = await read_policy(session, guild_id)
        if stored is not None:
            return from_document(
                guild_id,
                stored.document,
                revision=stored.revision,
                ownership=ownership,
            )
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
        acting = ownership.coordinator_acts
        on = _switched_on(ownership) if acting else None
        spam = (
            SpamEngineSettings.from_config(config)
            if config is not None
            and (on(own.SPAM_ENGINE) if on else config.spam_engine_enabled)
            else None
        )
        # The ring runs on the model defaults without a row (spam_ring.py).
        ring_config = config or ModerationFilterConfig.get_defaults(guild_id)
        ring = (
            SpamRingSettings.from_config(ring_config)
            if (on(own.SPAM_RING) if on else ring_config.spam_ring_enabled)
            else None
        )
        attachments = (
            AttachmentPolicy(
                ignored_extensions=tuple(attachment_config.ignored_extensions or ()),
                warn_extensions=tuple(attachment_config.warn_extensions or ()),
                delete_message=attachment_config.delete_message,
                warn_message=attachment_config.warn_message,
            )
            if attachment_config is not None
            and (on(own.ATTACHMENT_FILTER) if on else attachment_config.is_active)
            else None
        )
        staff_roles = tuple(
            str(role_id)
            for role_id in ((config.staff_exempt_role_ids or []) if config else [])
        )
        report_channel_id = (
            await load_report_channel(session, guild_id, config) if acting else None
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
        scam_gate=on(own.SCAM_GATE) if on else bool(scam and scam.enabled),
        edit_ping=on(own.EDIT_PING) if on else bool(edit and edit.enabled),
        scam_gate_channel_ids=_scope(scam),
        edit_ping_channel_ids=_scope(edit),
        # Disboard keeps its handler (scoring, crown, reminders), so its row
        # stays enabled and still names the channel.
        bump_channel_ids=frozenset(
            str(channel_id) for channel_id in (bump.channel_ids or [])
        )
        if bump is not None and bump.enabled and (on(own.BUMP_CHANNEL) if on else True)
        else frozenset(),
        mass_mention=bool(on and on(own.MASS_MENTION)),
        staff_exempt_role_ids=staff_roles,
        owner=ownership.owner,
        generation=ownership.generation,
        report_channel_id=report_channel_id,
        loaded_at=time.time(),
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


def _switched_on(ownership: Ownership):
    return lambda rule: rule in ownership.rules


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

    With ``ownership`` (:func:`.ownership.load_ownership`, as the bot runs it)
    the guild's ownership and policy revision are read on every message (one
    round trip), and a snapshot from another generation or revision is
    reloaded at once: the moment the cutover turns the legacy actors off, the
    coordinator is already acting, with no TTL-long gap where neither
    moderates, and a saved policy revision judges the very next message.

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
