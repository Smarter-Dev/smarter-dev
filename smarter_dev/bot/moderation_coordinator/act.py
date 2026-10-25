"""The coordinator's decision, carried out through the moderation gateway.

The cutover (#240) hands each :class:`~.coordinator.Evaluation` here instead of
only recording it. Everything goes through the gateway, which already gives
the guarantees the decision relies on: staff are asked again under the
member's lock, strongest wins within an incident, a message is deleted at most
once guild-wide, a timeout only ever extends, an uncertain call is reconciled
instead of resent, and one report per incident.

What this adds is the coordinator's side of the old actors' behaviour:

* **Provisional containment** (:meth:`Executor.contain`): the 5-minute
  ``provisional:<post>`` timeout and the ``-# Investigating potential scam``
  notice, placed before the classifier is asked. The verdict then releases the
  claim, upgrades it, or leaves it to expire, and the notice always gets a
  closing line, so it never hangs (#302).
* **One decision** (:meth:`Executor.execute`): every target's sanction and
  cleanup in one gateway call, then the warn notices, the member messages
  (each DM's delivery recorded on the incident) and the report lines: an
  alert when a hold ran out without a verdict, the review lines, the
  member's history drawn from the record (no model call) and how the
  report's summary begins. The hold's other outcomes need no line: the
  report's timeline shows the timeout, its upgrade or its lift (#333).
* **A failed evaluation after a hold** (:meth:`Executor.evaluation_failed`):
  a line for moderators and a closing notice.

**The fence.** Each step reads the guild's ownership again first and acts only
while the coordinator still owns the guild at the generation the decision was
made under. A rollback that lands mid-decision stops it; what already reached
Discord stays and is reconciled by the gateway, never replayed.

**The policy, again.** The same read carries the guild's policy revision
(#241). When it moved after the decision was made, the current revision is
loaded and each target's action is carried out only if that revision still
enforces every rule behind it and allows its sanction and its removals;
otherwise it is left out, and the report says so. A hold is placed only if
the current revision still holds. A policy that cannot be read acts on
nothing.

**What the member is told** follows the policy's ``destinations``: no
channel notices and no DMs when they are off (a warning is still recorded).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import Any
from uuid import UUID

from smarter_dev.bot.attachment_filter import classify_attachments
from smarter_dev.bot.moderation_coordinator import rules
from smarter_dev.bot.moderation_coordinator.coordinator import Evaluation
from smarter_dev.bot.moderation_coordinator.decide import EXPIRED
from smarter_dev.bot.moderation_coordinator.decide import RELEASED
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import NOTICE_ATTACHMENT_BLOCKED
from smarter_dev.bot.moderation_coordinator.model import NOTICE_ATTACHMENT_WARNED
from smarter_dev.bot.moderation_coordinator.model import NOTICE_EDIT_PING
from smarter_dev.bot.moderation_coordinator.model import NOTICE_MASS_MENTION
from smarter_dev.bot.moderation_coordinator.model import NOTICE_SCAM_REMOVED
from smarter_dev.bot.moderation_coordinator.model import NOTICE_SECURE_ACCOUNT
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import MessageFacts
from smarter_dev.bot.moderation_coordinator.model import Provisional
from smarter_dev.bot.moderation_coordinator.model import TargetDecision
from smarter_dev.bot.moderation_coordinator.ownership import Ownership
from smarter_dev.bot.moderation_coordinator.ownership import load_ownership
from smarter_dev.bot.moderation_coordinator.policy import GuildPolicy
from smarter_dev.bot.moderation_coordinator.policy import load_policy
from smarter_dev.bot.spam_engine import analyze_message_content
from smarter_dev.bot.spam_engine import format_scam_log_entry
from smarter_dev.bot.spam_engine import format_warning_message
from smarter_dev.bot.spam_ring import transcript_filename
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.shared.exception_logging import log_exception
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.types import Evidence
from smarter_dev.web.moderation_gateway.types import Proposal
from smarter_dev.web.moderation_gateway.types import Trigger

logger = logging.getLogger(__name__)

ACTOR = "coordinator"

INVESTIGATING = "-# Investigating potential scam"
CLOSED_CLEAN = "-# Checked: no scam found"
CLOSED_REMOVED = "-# Checked: removed as a scam"
CLOSED_UNKNOWN = "-# Check incomplete: a moderator will take a look"

#: scam-banner's own wording for the two warns, and for its hold.
SCAM_REMOVED_TEXT = (
    "Your message was removed after a clear scam or abusive-content review. "
    "Please review the server rules before posting again."
)
#: The public reason a likely hacked account's warning gives in the channel.
#: The member's DM says much more (:func:`hacked_account_dm`).
SECURE_ACCOUNT_TEXT = (
    "Your scam-like post suggests your account may be compromised. Please "
    "secure your account before posting again."
)
SECURE_ACCOUNT_STEPS = (
    "To secure it: change your Discord password (this logs out other "
    "sessions), turn on two-factor authentication, check Settings → "
    "Authorized Apps and Devices and remove anything you don't recognise, "
    'and run a malware scan if you ran any file or "game test" someone sent '
    "you."
)
EDIT_PING_TEXT = (
    "<@{user}> please don't edit a server-wide mention into a message after "
    "posting it — that edit has been removed. Ask a moderator if you need to "
    "reach everyone."
)
HISTORY_LINES = 5


_POLICY_SANCTION = {gw.WARN: mp.WARN, gw.TIMEOUT: mp.TIMEOUT, gw.BAN: mp.BAN}


def still_allowed(
    targets: list[TargetDecision], decided: GuildPolicy, current: GuildPolicy
) -> tuple[list[TargetDecision], list[tuple]]:
    """The targets whose action the current policy revision still allows,
    and a report line for each one left out.

    A target is carried out whole or not at all: every rule its decision
    acted on is still enforced, and its sanction and its removals are still
    allowed. A timeout past the current revision's longest is shortened to
    it, as :func:`~.decide.enforceable` does at decision time."""
    kept: list[TargetDecision] = []
    lines: list[tuple] = []
    for target in targets:
        acted = [rule for rule in dict.fromkeys(target.rules) if decided.enforces(rule)]
        sanction = target.sanction
        why = None
        stopped = [rule for rule in acted if not current.enforces(rule)]
        if stopped:
            why = f"{', '.join(stopped)} no longer enforced"
        elif sanction is not None and not current.allows(
            _POLICY_SANCTION.get(sanction.kind, sanction.kind)
        ):
            why = f"{sanction.kind} no longer allowed"
        elif target.cleanup and not current.allows(mp.DELETE):
            why = "removing messages no longer allowed"
        if why is None:
            limit = current.document.sanctions.max_timeout_seconds
            if (
                current.revision is not None
                and sanction is not None
                and sanction.kind == gw.TIMEOUT
                and (sanction.duration_seconds or 0) > limit
            ):
                target = replace(
                    target, sanction=replace(sanction, duration_seconds=limit)
                )
            kept.append(target)
            continue
        lines.append(
            (
                gw.ENTRY_NOTE,
                f"Not carried out for <@{target.user_id}>: the moderation policy "
                f"changed to revision {current.revision} before it acted ({why}).",
            )
        )
    return kept, lines


def hold_reason(reasons) -> str:
    """Why a hold was placed: its audit reason, its incident's reason, and the
    words the report's "Held … for review" line gives."""
    return "possible scam under review: " + ", ".join(reasons)


def hacked_account_dm(
    guild_name: str | None, removed: int, until: float | None, *, timed_out: bool
) -> str:
    """What a likely hacked account is told (#333): what happened, and how to
    take the account back. ``removed``: how many of its messages this decision
    removed; ``until``: when its timeout ends (a warn has none).

    The text names no count (#342): in a burst, the copies' own decisions may
    remove more after this DM is sent, so any count could undercount."""
    server = f"**{guild_name}**" if guild_name else "the server"
    what = "we removed what it posted" if removed > 0 else ""
    if timed_out:
        when = f"until <t:{int(until)}:f>" if until else "for now"
        what = (
            f"{what} and timed you out {when}" if what else f"we timed you out {when}"
        )
    opening = f"Your account posted what looks like a scam in {server}" + (
        f", so {what}." if what else "."
    )
    closing = (
        "If this was a mistake, reply to a moderator once the timeout ends."
        if timed_out
        else "If this was a mistake, reply to a moderator."
    )
    return (
        f"{opening} This usually means someone else has access to your account.\n\n"
        f"{SECURE_ACCOUNT_STEPS}\n\n{closing}"
    )


def provisional_claim(key: str) -> str:
    """The hold's claim: one per post, shared by its copies."""
    return f"provisional:{key}"


def jump_link(facts: MessageFacts) -> str:
    return (
        f"https://discord.com/channels/{facts.guild_id}/{facts.channel_id}/"
        f"{facts.message_id}"
    )


class Messenger:
    """Member-facing messages: a notice in a channel, a DM. Never raises for
    Discord: a refused or failed send is ``False``."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def post(
        self, channel_id: str, content: str, *, mention: str | None = None
    ) -> bool:
        import hikari

        try:
            await self.bot.rest.create_message(
                hikari.Snowflake(int(channel_id)),
                content,
                mentions_everyone=False,
                user_mentions=[hikari.Snowflake(int(mention))] if mention else False,
                role_mentions=False,
            )
        except hikari.HTTPError as error:
            logger.warning(
                "Coordinator could not post in channel %s (%s)",
                channel_id,
                type(error).__name__,
            )
            return False
        return True

    async def dm(self, user_id: str, content: str) -> bool:
        import hikari

        try:
            channel = await self.bot.rest.create_dm_channel(int(user_id))
            await self.bot.rest.create_message(
                channel,
                content,
                mentions_everyone=False,
                user_mentions=False,
                role_mentions=False,
            )
        except hikari.HTTPError:
            # Closed DMs are an expected outcome, not an error.
            return False
        return True

    def guild_name(self, guild_id: str) -> str | None:
        """The server's name from the bot's cache, for a member's DM."""
        try:
            guild = self.bot.cache.get_guild(int(guild_id))
        except Exception:  # noqa: BLE001 — a DM without the name is fine
            return None
        return getattr(guild, "name", None) or None


@dataclass(slots=True)
class _Hold:
    channel_id: str
    incident_id: UUID | None
    noticed: bool
    #: Messages held under this post's claim whose decision is still out (a
    #: post and its escalated copy). Only the last one releases the claim and
    #: closes the notice: an earlier "clean" must not lift a copy's hold. A
    #: decision for a message the hold never counted cannot close it.
    pending: set[str] = field(default_factory=set)


class Executor:
    """Carries out decisions for one process."""

    def __init__(
        self,
        gateway_for: Callable[[GuildPolicy], Any],
        messenger: Any,
        *,
        ownership: Callable[[str], Awaitable[Ownership]] = load_ownership,
        policies: Callable[[str], Awaitable[GuildPolicy]] = load_policy,
    ) -> None:
        self._gateway_for = gateway_for
        self.messenger = messenger
        self._ownership = ownership
        self._policies = policies
        #: Holds placed and not yet resolved: (guild, author, post key).
        self._holds: dict[tuple[str, str, str], _Hold] = {}

    @classmethod
    def for_bot(cls, bot: Any) -> Executor:
        from smarter_dev.bot.moderation_port import bot_gateway

        return cls(
            lambda policy: bot_gateway(
                bot, extra_role_ids=policy.staff_exempt_role_ids
            ),
            Messenger(bot),
        )

    # ------------------------------------------------------------------
    # The fence
    # ------------------------------------------------------------------

    async def still_owner(self, policy: GuildPolicy) -> bool:
        """Whether the coordinator still owns the guild at the policy's
        generation. An unreadable ownership acts on nothing."""
        return await self._owned(policy) is not None

    async def _owned(self, policy: GuildPolicy) -> Ownership | None:
        if not policy.acting:
            return None
        try:
            current = await self._ownership(policy.guild_id)
        except Exception:  # noqa: BLE001 — fail closed
            log_exception(
                logger,
                "Coordinator could not read guild %s's ownership; not acting",
                policy.guild_id,
                level=logging.WARNING,
            )
            return None
        if current.coordinator_acts and current.generation == policy.generation:
            return current
        logger.warning(
            "Coordinator not acting in guild %s: owner is now %s, generation %d "
            "(decided under %d)",
            policy.guild_id,
            current.owner,
            current.generation,
            policy.generation,
        )
        return None

    async def current_policy(self, policy: GuildPolicy) -> GuildPolicy | None:
        """The policy to act under now: ``policy`` while its revision is still
        current, the current revision when it moved, None when the
        coordinator no longer owns the guild or the policy cannot be read."""
        owned = await self._owned(policy)
        if owned is None:
            return None
        if owned.policy_revision == policy.revision:
            return policy
        try:
            current = await self._policies(policy.guild_id)
        except Exception:  # noqa: BLE001 — fail closed
            log_exception(
                logger,
                "Coordinator could not read guild %s's moderation policy; not acting",
                policy.guild_id,
                level=logging.WARNING,
            )
            return None
        if not current.acting or current.generation != policy.generation:
            return None
        logger.info(
            "Coordinator in guild %s: policy revision %s -> %s before acting",
            policy.guild_id,
            policy.revision,
            current.revision,
        )
        return current

    # ------------------------------------------------------------------
    # Provisional containment
    # ------------------------------------------------------------------

    async def contain(
        self,
        facts: MessageFacts,
        provisional: Provisional,
        key: str,
        *,
        policy: GuildPolicy,
    ) -> None:
        """Place the hold and its notice; never raises (the decision goes on).

        The message counts on the hold before anything is awaited: a post
        decided while its copy's hold is still being placed (or ownership
        read) finds the copy pending, and does not release it. It counts even when placing fails:
        its decision still comes, and is then one of the hold's (#310 round
        3, item 2)."""
        hold_key = (facts.guild_id, facts.author_id, key)
        # Before any await: a post decided while this copy reads ownership
        # must not release the claim the copy is about to hold under.
        hold = self._holds.get(hold_key)
        if hold is None:
            hold = _Hold(facts.channel_id, None, False)
            self._holds[hold_key] = hold
        hold.pending.add(facts.message_id)
        try:
            current = await self.current_policy(policy)
            if current is None or not current.provisional_allowed():
                hold.pending.discard(facts.message_id)
                if (
                    not hold.pending
                    and hold.incident_id is None
                    and self._holds.get(hold_key) is hold
                ):
                    del self._holds[hold_key]
                return
            gateway = self._gateway_for(policy)
            result = await gateway.act(
                self._trigger(facts, reason=hold_reason(provisional.reasons)),
                [
                    Proposal(
                        kind=gw.TIMEOUT,
                        target_user_id=facts.author_id,
                        target_username=facts.author_name or None,
                        reason=hold_reason(provisional.reasons),
                        duration_seconds=provisional.duration_seconds,
                        claim=provisional_claim(key),
                        provisional=True,
                        target_is_bot=False,
                    )
                ],
            )
            outcome = result.first(gw.TIMEOUT)
            if hold.incident_id is None:
                hold.incident_id = result.incident_id
            # One notice per post, where the hold was first placed (a copy
            # finds the hold already in place: covered, not confirmed).
            if (
                outcome is not None
                and outcome.state == gw.CONFIRMED
                and not hold.noticed
            ):
                hold.noticed = await self.messenger.post(
                    facts.channel_id, INVESTIGATING
                )
                hold.channel_id = facts.channel_id
        except Exception:  # noqa: BLE001 — containment failing must not stop the decision
            log_exception(
                logger,
                "Coordinator could not place the hold for message %s",
                facts.message_id,
                level=logging.WARNING,
            )

    async def evaluation_failed(
        self, facts: MessageFacts, key: str, policy: GuildPolicy, error: BaseException
    ) -> None:
        """The decision never came after a hold was placed: say so (#302)."""
        await self._close_unfinished(
            facts,
            key,
            policy,
            f"failed after the hold was placed ({type(error).__name__})",
        )

    async def _close_unfinished(
        self,
        facts: MessageFacts,
        key: str,
        policy: GuildPolicy,
        cause: str,
        *,
        counted: bool = False,
    ) -> None:
        """A hold whose decision did not finish: a line for moderators and a
        closing notice, then the hold is forgotten. Never raises. It runs
        after the guild was handed back too: it only closes what this
        process already posted."""
        hold_key = (facts.guild_id, facts.author_id, key)
        hold = self._holds.get(hold_key)
        if hold is None or (not counted and self._others_pending(hold, facts)):
            return
        try:
            gateway = self._gateway_for(policy)
            if hold.incident_id is not None:
                await gateway.add_notes(
                    hold.incident_id,
                    source=ACTOR,
                    notes=[
                        (
                            gw.ENTRY_ALERT,
                            f"The scam check on <@{facts.author_id}>'s message "
                            f"({jump_link(facts)}) {cause}. The 5-minute hold "
                            "lapses on its own; a moderator should look.",
                        )
                    ],
                    report_channel_id=policy.report_channel_id,
                    target_user_id=facts.author_id,
                )
        except Exception:  # noqa: BLE001
            log_exception(
                logger,
                "Coordinator could not report the unfinished check on message %s",
                facts.message_id,
                level=logging.WARNING,
            )
        try:
            if hold.noticed:
                await self.messenger.post(hold.channel_id, CLOSED_UNKNOWN)
        except Exception:  # noqa: BLE001
            log_exception(
                logger,
                "Coordinator could not close the hold notice for message %s",
                facts.message_id,
                level=logging.WARNING,
            )
        finally:
            if self._holds.get(hold_key) is hold:
                del self._holds[hold_key]

    async def notify(self, policy: GuildPolicy, text: str) -> None:
        """One line in the moderators' channel; never raises."""
        if policy.report_channel_id is None or not await self.still_owner(policy):
            return
        try:
            await self.messenger.post(policy.report_channel_id, text)
        except Exception:  # noqa: BLE001
            log_exception(
                logger,
                "Coordinator could not post to guild %s's report channel",
                policy.guild_id,
                level=logging.WARNING,
            )

    async def close_hold(self, evaluation: Evaluation) -> None:
        """Close this process's hold notice without acting: another process
        recorded (and carries out) the decision for the same message."""
        if evaluation.to_carry_out.provisional is None:
            return
        facts = evaluation.facts
        hold_key = (facts.guild_id, facts.author_id, evaluation.key)
        hold = self._holds.get(hold_key)
        if hold is None or self._others_pending(hold, facts):
            return
        del self._holds[hold_key]
        if not hold.noticed:
            return
        try:
            await self.messenger.post(
                hold.channel_id,
                _closing(evaluation.to_carry_out.provisional, evaluation),
            )
        except Exception:  # noqa: BLE001
            log_exception(
                logger,
                "Coordinator could not close the hold notice for message %s",
                facts.message_id,
                level=logging.WARNING,
            )

    # ------------------------------------------------------------------
    # The decision
    # ------------------------------------------------------------------

    async def execute(self, evaluation: Evaluation) -> gw.ActResult | None:
        """Carry out one decision. Returns the gateway's result, or None when
        there was nothing to do or the coordinator no longer owns the guild.

        A hold this decision owns is always closed: by its closing line when
        the decision finishes, otherwise (a raise, or the guild handed back
        mid-decision) by an "incomplete" line and a note for moderators."""
        facts = evaluation.facts
        policy = evaluation.policy
        provisional = evaluation.to_carry_out.provisional
        # The hold belongs to the post and its copies; a decision with a
        # provisional outcome of its own closes it (a copy without one, still
        # waiting on the post's verdict, must not close it as "incomplete").
        hold_key = (facts.guild_id, facts.author_id, evaluation.key)
        hold = self._holds.get(hold_key) if provisional is not None else None
        if hold is not None and self._others_pending(hold, facts):
            # Another message held under the same claim is still being
            # decided: it releases the hold and closes the notice.
            result, _ = await self._execute(evaluation, None, last=False)
            return result
        closed = False
        try:
            result, closed = await self._execute(evaluation, hold)
            return result
        finally:
            if hold is not None:
                if closed:
                    if self._holds.get(hold_key) is hold:
                        del self._holds[hold_key]
                else:
                    await self._close_unfinished(
                        facts,
                        evaluation.key,
                        policy,
                        "stopped before its decision was carried out",
                        counted=True,
                    )

    @staticmethod
    def _others_pending(hold: _Hold, facts: MessageFacts) -> bool:
        """Count one held message's decision as done; whether others remain."""
        hold.pending.discard(facts.message_id)
        return bool(hold.pending)

    async def _execute(
        self, evaluation: Evaluation, hold: _Hold | None, *, last: bool = True
    ) -> tuple[gw.ActResult | None, bool]:
        """The decision, and whether its hold (if any) got its closing line.
        ``last=False``: other held messages share the claim; it is not
        released here."""
        facts = evaluation.facts
        policy = evaluation.policy
        decision = evaluation.to_carry_out
        targets = [
            target
            for _, target in sorted(decision.targets.items())
            if target.sanction is not None or target.cleanup or target.notices
        ]
        provisional = decision.provisional
        lines = self._report_lines(evaluation)
        if not targets and provisional is None and hold is None:
            # Nothing to do: review lines alone (an unread history, a skipped
            # ring check on a message nothing fired on) open no incident.
            if lines:
                logger.info(
                    "Coordinator review lines for message %s: %s",
                    facts.message_id,
                    review_text(lines),
                )
            return None, False
        current = await self.current_policy(policy)
        if current is None:
            return None, False
        if current is not policy:
            targets, left_out = still_allowed(targets, policy, current)
            lines += left_out
            if not targets and provisional is None and hold is None and not lines:
                return None, False
            # Reported, told and protected as the current revision says.
            policy = current

        gateway = self._gateway_for(policy)
        reportable = bool(lines) or any(
            (t.sanction is not None and t.sanction.kind != gw.WARN)
            or rules.SPAM_RING in t.rules
            or rules.SCAM_GATE in t.rules
            or rules.MASS_MENTION in t.rules
            for t in targets
        )
        trigger = self._trigger(
            facts,
            # No reason (a clean release alone) writes no reason line.
            reason=_reason(decision.targets.values()),
            report_channel_id=policy.report_channel_id if reportable else None,
            ping_role_id=_ping_role(policy, targets),
            source=gw.SOURCE_AI
            if any(t.model_justified for t in targets)
            else gw.SOURCE_HANDLER,
            evidence=_evidence(evaluation),
            related=tuple(item.message_id for t in targets for item in t.cleanup),
            alias=evaluation.ring_alias,
        )

        proposals: list[Proposal] = []
        for target in targets:
            sanction = target.sanction
            if sanction is not None and sanction.kind != gw.WARN:
                proposals.append(
                    Proposal(
                        kind=sanction.kind,
                        target_user_id=target.user_id,
                        target_username=target.name,
                        reason=_reason([target]),
                        duration_seconds=sanction.duration_seconds,
                        claim=sanction.claim,
                        target_is_bot=False,
                    )
                )
            by_channel: dict[str, list[str]] = defaultdict(list)
            for item in target.cleanup:
                by_channel[item.channel_id].append(item.message_id)
            for channel_id, message_ids in by_channel.items():
                proposals.append(
                    Proposal(
                        kind=gw.PURGE if len(message_ids) > 1 else gw.DELETE,
                        target_user_id=target.user_id,
                        target_username=target.name,
                        reason=_reason([target]),
                        channel_id=channel_id,
                        message_ids=tuple(message_ids),
                        target_is_bot=False,
                    )
                )
        if provisional is not None and provisional.resolution == RELEASED and last:
            # Ends this post's claim only: a moderator's or a longer
            # automated timeout stays.
            proposals.append(
                Proposal(
                    kind=gw.UNTIMEOUT,
                    target_user_id=provisional.user_id,
                    target_username=facts.author_name or None,
                    reason="Cleared by the scam check",
                    claim=provisional_claim(evaluation.key),
                    target_is_bot=False,
                )
            )

        if proposals:
            result = await gateway.act(trigger, proposals)
        else:
            result = gw.ActResult(
                incident_id=await gateway.ensure_incident(
                    trigger, subject=facts.author_id
                )
            )

        deleted = {
            message_id
            for outcome in result.outcomes
            if outcome.kind in gw.CLEANUP_KINDS and outcome.applied
            for message_id in outcome.deleted
        }
        for target in targets:
            await self._tell(gateway, trigger, result, target, facts, policy, deleted)
        await self._scam_log(facts, policy, targets, deleted)

        if lines and result.incident_id is not None:
            await gateway.add_notes(
                result.incident_id,
                source=ACTOR,
                notes=lines,
                report_channel_id=policy.report_channel_id,
                target_user_id=facts.author_id,
            )
        if hold is not None and hold.noticed:
            await self.messenger.post(
                hold.channel_id, _closing(provisional, evaluation)
            )
        return result, True

    # ------------------------------------------------------------------
    # Member-facing messages
    # ------------------------------------------------------------------

    async def _tell(
        self,
        gateway,
        trigger: Trigger,
        result: gw.ActResult,
        target: TargetDecision,
        facts: MessageFacts,
        policy: GuildPolicy,
        deleted: set[str],
    ) -> None:
        notices = list(dict.fromkeys(target.notices))
        hacked = NOTICE_SECURE_ACCOUNT in notices
        scam_text = (
            SECURE_ACCOUNT_TEXT
            if hacked
            else SCAM_REMOVED_TEXT
            if NOTICE_SCAM_REMOVED in notices
            else None
        )
        removed = _removed(target, result, deleted)
        destinations = policy.document.destinations
        dms = destinations.member_dms
        channel_notices = destinations.channel_notices
        if target.sanction is not None and target.sanction.kind == gw.WARN:
            if scam_text is not None:
                notice = (
                    f"⚠️ <@{target.user_id}> has been warned.\n**Reason:** {scam_text}"
                )
                dm = (
                    hacked_account_dm(
                        self._guild_name(facts), removed, None, timed_out=False
                    )
                    if hacked
                    else _warning_dm(scam_text)
                )
                await self._warn(
                    gateway,
                    trigger,
                    target,
                    facts,
                    notice,
                    dm if dms else None,
                    post=channel_notices,
                )
            elif NOTICE_MASS_MENTION in notices:
                await self._warn_by_dm(
                    gateway,
                    trigger,
                    target,
                    facts,
                    policy,
                    rules.MASS_MENTION_WARNING,
                    send=dms and policy.document.rules.mass_mention.dm,
                )
            else:
                notice = format_warning_message(target.user_id, _spam_reasons(target))
                await self._warn(
                    gateway, trigger, target, facts, notice, None, post=channel_notices
                )
        elif scam_text is not None and target.sanction is not None and dms:
            # A timeout (high-signal or repeat): the member is told by DM,
            # once it took effect (never after a staff refusal or a reversal).
            applied = next(
                (
                    outcome
                    for outcome in result.outcomes
                    if outcome.kind == target.sanction.kind
                    and outcome.target_user_id == target.user_id
                    and outcome.applied
                ),
                None,
            )
            if (
                applied is not None
                and result.incident_id is not None
                and await gateway.claim_member_message(
                    result.incident_id,
                    kind=gw.MESSAGE_DM,
                    target_user_id=target.user_id,
                )
            ):
                if hacked:
                    # A timeout already in place ends when it ends, not when
                    # this decision asked for (#342).
                    ends = applied.in_place_until or applied.expires_at
                    until = ends.timestamp() if ends else None
                    text = hacked_account_dm(
                        self._guild_name(facts),
                        removed,
                        until,
                        timed_out=target.sanction.kind == gw.TIMEOUT,
                    )
                else:
                    text = scam_text
                await self._dm_and_record(
                    gateway,
                    result.incident_id,
                    gw.MESSAGE_DM,
                    target.user_id,
                    text,
                    report_channel_id=policy.report_channel_id,
                )

        user = f"<@{target.user_id}>"
        if not channel_notices:
            return
        if NOTICE_ATTACHMENT_BLOCKED in notices and facts.message_id in deleted:
            await self.messenger.post(
                facts.channel_id,
                _attachment_notice(facts, policy, user, blocked=True),
                mention=target.user_id,
            )
        elif NOTICE_ATTACHMENT_WARNED in notices:
            await self.messenger.post(
                facts.channel_id,
                _attachment_notice(facts, policy, user, blocked=False),
                mention=target.user_id,
            )
        if NOTICE_EDIT_PING in notices and facts.message_id in deleted:
            await self.messenger.post(
                facts.channel_id,
                EDIT_PING_TEXT.format(user=target.user_id),
                mention=target.user_id,
            )

    async def _scam_log(
        self,
        facts: MessageFacts,
        policy: GuildPolicy,
        targets: list[TargetDecision],
        deleted: set[str],
    ) -> None:
        """The spam engine's archive of the (defanged) scam links it removed."""
        settings = policy.spam
        if settings is None or not settings.scam_log_channel_id:
            return
        removed = facts.message_id in deleted and any(
            rules.SPAM_ENGINE in t.rules
            and any(item.message_id == facts.message_id for item in t.cleanup)
            for t in targets
        )
        if not removed:
            return
        analysis = analyze_message_content(facts.content, settings.scam_link_domains)
        if not analysis.scam_links:
            return
        await self.messenger.post(
            settings.scam_log_channel_id,
            format_scam_log_entry(
                author_id=facts.author_id,
                author_username=facts.author_name or facts.author_id,
                channel_id=facts.channel_id,
                scam_links=analysis.scam_links,
            ),
        )

    async def _warn(
        self,
        gateway,
        trigger: Trigger,
        target: TargetDecision,
        facts: MessageFacts,
        notice: str,
        dm: str | None,
        *,
        post: bool = True,
    ) -> None:
        """As the spam engine and the handlers warn: one row, one notice per
        incident (a warn the incident already covers still tells the member
        once), and for a scam a best-effort DM. ``post=False`` (the policy's
        channel notices off): the warning is recorded, nothing is posted."""
        async with gateway.reserve(
            trigger,
            Proposal(
                kind=gw.WARN,
                target_user_id=target.user_id,
                target_username=target.name,
                reason=_reason([target]),
                channel_id=facts.channel_id,
                target_is_bot=False,
            ),
        ) as slot:
            send = slot.go
            if not post:
                if slot.go:
                    slot.confirm()
                send = False
            elif not send:
                superseded = slot.outcome is not None and slot.outcome.superseded
                send = superseded and await gateway.claim_member_message(
                    slot.incident_id,
                    kind=gw.MESSAGE_NOTICE,
                    target_user_id=target.user_id,
                )
            if send:
                posted = await self.messenger.post(
                    facts.channel_id, notice, mention=target.user_id
                )
                if slot.go:
                    if posted:
                        slot.confirm()
                    else:
                        slot.fail("could not post the warning")
            incident_id = slot.incident_id
        if (
            dm
            and incident_id is not None
            and await gateway.claim_member_message(
                incident_id, kind=gw.MESSAGE_DM, target_user_id=target.user_id
            )
        ):
            await self._dm_and_record(
                gateway,
                incident_id,
                gw.MESSAGE_DM,
                target.user_id,
                dm,
                report_channel_id=trigger.report_channel_id,
            )

    def _guild_name(self, facts: MessageFacts) -> str | None:
        name = getattr(self.messenger, "guild_name", None)
        return name(facts.guild_id) if name is not None else None

    async def _dm_and_record(
        self,
        gateway,
        incident_id: UUID,
        kind: str,
        user_id: str,
        content: str,
        *,
        report_channel_id: str | None,
    ) -> bool:
        """A claimed DM, then whether it arrived, on the incident (#333)."""
        delivered = await self._dm(user_id, content)
        try:
            await gateway.record_member_message(
                incident_id,
                kind=kind,
                target_user_id=user_id,
                delivered=delivered,
                source=ACTOR,
                report_channel_id=report_channel_id,
            )
        except Exception:  # noqa: BLE001 — the DM went (or not) either way
            log_exception(
                logger,
                "Coordinator could not record the DM to %s",
                user_id,
                level=logging.WARNING,
            )
        return delivered

    async def _dm(self, user_id: str, content: str) -> bool:
        """A DM to the member; never raises. False when it was not delivered
        (closed DMs, a rate limit, Discord down, or anything else)."""
        try:
            return await self.messenger.dm(user_id, content)
        except Exception:  # noqa: BLE001 — the decision's other messages go on
            log_exception(
                logger,
                "Coordinator could not DM %s",
                user_id,
                level=logging.WARNING,
            )
            return False

    async def _warn_by_dm(
        self,
        gateway,
        trigger: Trigger,
        target: TargetDecision,
        facts: MessageFacts,
        policy: GuildPolicy,
        reason: str,
        *,
        send: bool = True,
    ) -> None:
        """A warning on mod history, told by DM only (Zech, Oct 24: the
        mass-mention warning). One per incident: the gateway's ``warn`` key
        fences a post's copies and a second pod. It is ``dm_only`` with its
        own DM claim, so a scam decision on the same message still posts its
        notice and sends its DM. An undelivered DM still leaves the warning
        recorded; either way the report says whether the member was told."""
        async with gateway.reserve(
            trigger,
            Proposal(
                kind=gw.WARN,
                target_user_id=target.user_id,
                target_username=target.name,
                reason=reason,
                channel_id=facts.channel_id,
                dm_only=True,
                target_is_bot=False,
            ),
        ) as slot:
            if not slot.go:
                # Already warned in this incident, or a stronger sanction.
                return
            slot.confirm()
            incident_id = slot.incident_id
        if not send:
            # The policy sends no DM (``mass_mention.dm`` or member DMs off).
            return
        if incident_id is None or not await gateway.claim_member_message(
            incident_id, kind=gw.MESSAGE_WARNING_DM, target_user_id=target.user_id
        ):
            return
        await self._dm_and_record(
            gateway,
            incident_id,
            gw.MESSAGE_WARNING_DM,
            target.user_id,
            _warning_dm(reason),
            report_channel_id=policy.report_channel_id,
        )

    # ------------------------------------------------------------------
    # Report lines
    # ------------------------------------------------------------------

    def _report_lines(self, evaluation: Evaluation) -> list[tuple]:
        decision = evaluation.to_carry_out
        facts = evaluation.facts
        lines: list[tuple] = []
        provisional = decision.provisional
        if provisional is not None and provisional.resolution == EXPIRED:
            lines.append(_expired_line(provisional, evaluation))
        lines.extend((gw.ENTRY_NOTE, line) for line in decision.review)
        scam_fired = any(
            line.startswith(rules.SCAM_GATE) for line in decision.rules_fired
        )
        if evaluation.history is not None and (scam_fired or provisional is not None):
            lines.append(
                (
                    gw.ENTRY_HISTORY,
                    history_note(facts.author_id, evaluation.history),
                    {"count": len(evaluation.history.actions)},
                )
            )
        lead = _summary_lead(evaluation)
        if lead is not None:
            lines.append(
                (gw.ENTRY_SUMMARY, lead, {"priority": LEAD_PRIORITY.get(lead, 0)})
            )
        return lines

    def _trigger(
        self,
        facts: MessageFacts,
        *,
        reason: str,
        report_channel_id: str | None = None,
        ping_role_id: str | None = None,
        source: str = gw.SOURCE_HANDLER,
        evidence: tuple[Evidence, ...] = (),
        related: tuple[str, ...] = (),
        alias: str | None = None,
    ) -> Trigger:
        return Trigger(
            guild_id=facts.guild_id,
            actor=ACTOR,
            reason=reason,
            source=source,
            channel_id=facts.channel_id,
            message_id=facts.message_id,
            author_id=facts.author_id,
            content=facts.content or None,
            attachments=tuple(
                gw.attachment_signature(
                    item.filename, item.size, item.width, item.height
                )
                for item in facts.attachments
            ),
            aliases=(alias,) if alias else (),
            related_message_ids=tuple(
                dict.fromkeys(m for m in related if m != facts.message_id)
            ),
            evidence=evidence,
            report_channel_id=report_channel_id,
            ping_role_id=ping_role_id,
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def review_text(lines) -> str:
    """Report lines as one log line. A line is ``(kind, text)`` or, with its
    details, ``(kind, text, details)`` (#342: a history line once raised
    ValueError here)."""
    return " | ".join(line[1] for line in lines)


def _warning_dm(reason: str) -> str:
    """The standard warning DM, as the /warn command words it."""
    return (
        "You have received a warning.\n\n"
        f"**Reason:** {reason}\n\n"
        "Please review the server rules. Continued violations may "
        "result in further action (timeout, kick, or ban)."
    )


def _removed(target: TargetDecision, result: gw.ActResult, deleted: set[str]) -> int:
    """How many of the target's messages are gone, for their DM: every one
    the decision removes, unless a removal failed (then those confirmed)."""
    wanted = {item.message_id for item in target.cleanup}
    failed = any(
        outcome.kind in gw.CLEANUP_KINDS
        and outcome.target_user_id == target.user_id
        and outcome.state in (gw.FAILED, gw.UNKNOWN)
        for outcome in result.outcomes
    )
    return len(wanted & deleted) if failed else len(wanted)


#: Which summary lead wins in an incident with several decisions (#342): a
#: confirmed violation over a clean release over an unanswered check, so a
#: later copy's unknown verdict never overwrites "Confirmed scam".
LEAD_CONFIRMED_HACKED = "Confirmed scam from a likely hacked account"
LEAD_CONFIRMED = "Confirmed scam"
LEAD_CLEAN = "Scam check found nothing"
LEAD_INCOMPLETE = "Scam check incomplete"
LEAD_PRIORITY = {
    LEAD_CONFIRMED_HACKED: 3,
    LEAD_CONFIRMED: 3,
    LEAD_CLEAN: 2,
    LEAD_INCOMPLETE: 1,
}


def _summary_lead(evaluation: Evaluation) -> str | None:
    """How the report's summary begins, for a scam check whose decision
    holds the outcome: a sanction, a clean release, or no verdict."""
    decision = evaluation.to_carry_out
    provisional = decision.provisional
    sanctioned = [t for t in decision.targets.values() if t.sanction is not None]
    if evaluation.scam_outcome == "violation" and sanctioned:
        hacked = any(NOTICE_SECURE_ACCOUNT in t.notices for t in sanctioned)
        return LEAD_CONFIRMED_HACKED if hacked else LEAD_CONFIRMED
    if provisional is not None and provisional.resolution == RELEASED:
        return LEAD_CLEAN
    if provisional is not None and provisional.resolution == EXPIRED:
        return LEAD_INCOMPLETE
    return None


def _reason(targets) -> str:
    """The targets' reasons, as the incident and the audit log carry them. A
    copy's "already sanctioned" qualifier is dropped: it says why this
    decision carries no sanction, not why anything was done."""
    return "; ".join(
        dict.fromkeys(
            reason.removesuffix(rules.ALREADY_SANCTIONED)
            for t in targets
            for reason in t.reasons
        )
    )


def _spam_reasons(target: TargetDecision) -> list[str]:
    return [
        reason
        for rule, reason in zip(target.rules, target.reasons, strict=False)
        if rule == rules.SPAM_ENGINE
    ] or list(target.reasons)


def _ping_role(policy: GuildPolicy, targets: list[TargetDecision]) -> str | None:
    """A spam mute pings the moderators' role, as the engine's did."""
    if policy.spam is None:
        return None
    for target in targets:
        if (
            rules.SPAM_ENGINE in target.rules
            and target.sanction is not None
            and target.sanction.kind == gw.TIMEOUT
        ):
            return policy.spam.mod_ping_role_id
    return None


def _evidence(evaluation: Evaluation) -> tuple[Evidence, ...]:
    if not evaluation.ring_context:
        return ()
    facts = evaluation.facts
    from datetime import UTC
    from datetime import datetime

    return (
        Evidence(
            kind=gw.ENTRY_TRANSCRIPT,
            content="\n".join(evaluation.ring_context),
            channel_id=facts.channel_id,
            details={
                "filename": transcript_filename(
                    facts.channel_id, datetime.fromtimestamp(facts.posted_at, UTC)
                )
            },
        ),
    )


def _attachment_notice(
    facts: MessageFacts, policy: GuildPolicy, user: str, *, blocked: bool
) -> str:
    """The attachment filter's own notice (the guild's text when it set one)."""
    settings = policy.attachments
    warned, blocked_items = classify_attachments(
        [item.filename for item in facts.attachments],
        ignored_extensions=settings.ignored_extensions if settings else (),
        warn_extensions=settings.warn_extensions if settings else (),
    )
    items = blocked_items if blocked else warned
    reported = items[0] if items else {"extension": "", "filename": ""}
    extension, filename = reported["extension"], reported["filename"]
    template = (
        (settings.delete_message if blocked else settings.warn_message)
        if settings
        else None
    )
    if template:
        try:
            return template.format(user=user, extension=extension, filename=filename)
        except (KeyError, IndexError, ValueError):
            pass
    if blocked:
        return (
            f"{user}, your message was removed because the file type ({extension}) "
            "is not allowed. Please use an approved file format."
        )
    return (
        f"{user}, your attachment ({extension}) requires caution. Please ensure "
        "you trust the source of this file."
    )


def _unknown_cause(evaluation: Evaluation) -> str:
    verdicts = evaluation.verdicts
    causes = [
        f"{label}: {verdicts[label].describe()}"
        for label in (SCAM, ABUSE)
        if label in verdicts and not verdicts[label].known
    ]
    if causes:
        return ", ".join(causes)
    return (evaluation.classifier.outcome or "no answer").replace("_", " ")


def _expired_line(provisional: Provisional, evaluation: Evaluation) -> tuple[str, str]:
    """The alert for a hold left without a verdict."""
    user = f"<@{provisional.user_id}>"
    return (
        gw.ENTRY_ALERT,
        f"Provisional hold on {user} expired without a verdict "
        f"({_unknown_cause(evaluation)}); it lapses after 5 minutes. A moderator "
        f"should look: {jump_link(evaluation.facts)}",
    )


def _closing(provisional: Provisional | None, evaluation: Evaluation) -> str:
    resolution = provisional.resolution if provisional is not None else EXPIRED
    if resolution == RELEASED:
        return CLOSED_CLEAN
    if resolution != EXPIRED or evaluation.scam_outcome == "violation":
        return CLOSED_REMOVED
    return CLOSED_UNKNOWN


def history_note(user_id: str, history: AuthorHistory) -> str:
    """scam-banner's history line, from the record instead of a model."""
    actions = sorted(history.actions, key=lambda a: a.created_at, reverse=True)
    if not actions:
        return f"History: no earlier moderation actions on record for <@{user_id}>."
    shown = []
    for action in actions[:HISTORY_LINES]:
        reason = (action.reason or "no reason").replace("@", "@​")
        reason = " ".join(reason.split())[:200]
        shown.append(f"{action.created_at:%Y-%m-%d} {action.action_type}: {reason}")
    more = len(actions) - len(shown)
    tail = f" (+{more} more)" if more > 0 else ""
    return (
        f"History: {len(actions)} earlier action(s) on record for <@{user_id}>: "
        + "; ".join(shown)
        + tail
    )
