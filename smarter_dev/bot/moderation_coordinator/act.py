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
  and the report lines: the provisional outcome with its cause, the review
  lines, and a history note drawn from the record (no model call).
* **A failed evaluation after a hold** (:meth:`Executor.evaluation_failed`):
  a line for moderators and a closing notice.

**The fence.** Each step reads the guild's ownership again first and acts only
while the coordinator still owns the guild at the generation the decision was
made under. A rollback that lands mid-decision stops it; what already reached
Discord stays and is reconciled by the gateway, never replayed.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from uuid import UUID

from smarter_dev.bot.attachment_filter import classify_attachments
from smarter_dev.bot.moderation_coordinator import rules
from smarter_dev.bot.moderation_coordinator.coordinator import Evaluation
from smarter_dev.bot.moderation_coordinator.decide import EXPIRED
from smarter_dev.bot.moderation_coordinator.decide import RELEASED
from smarter_dev.bot.moderation_coordinator.decide import UPGRADED
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import NOTICE_ATTACHMENT_BLOCKED
from smarter_dev.bot.moderation_coordinator.model import NOTICE_ATTACHMENT_WARNED
from smarter_dev.bot.moderation_coordinator.model import NOTICE_EDIT_PING
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
from smarter_dev.bot.spam_engine import analyze_message_content
from smarter_dev.bot.spam_engine import format_scam_log_entry
from smarter_dev.bot.spam_engine import format_warning_message
from smarter_dev.bot.spam_ring import transcript_filename
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
SECURE_ACCOUNT_TEXT = (
    "Your scam-like post suggests your account may be compromised. Please "
    "secure your account before posting again."
)
HOLD_REASON = "Held while a possible scam is reviewed"
EDIT_PING_TEXT = (
    "<@{user}> please don't edit a server-wide mention into a message after "
    "posting it — that edit has been removed. Ask a moderator if you need to "
    "reach everyone."
)
HISTORY_LINES = 5


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
    ) -> None:
        self._gateway_for = gateway_for
        self.messenger = messenger
        self._ownership = ownership
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
        if not policy.acting:
            return False
        try:
            current = await self._ownership(policy.guild_id)
        except Exception:  # noqa: BLE001 — fail closed
            log_exception(
                logger,
                "Coordinator could not read guild %s's ownership; not acting",
                policy.guild_id,
                level=logging.WARNING,
            )
            return False
        if current.coordinator_acts and current.generation == policy.generation:
            return True
        logger.warning(
            "Coordinator not acting in guild %s: owner is now %s, generation %d "
            "(decided under %d)",
            policy.guild_id,
            current.owner,
            current.generation,
            policy.generation,
        )
        return False

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

        The message counts on the hold before the gateway is asked: a copy
        decided while the post's hold is still being placed finds it
        pending, and does not release it. It counts even when placing fails:
        its decision still comes, and is then one of the hold's (#310 round
        3, item 2)."""
        hold_key = (facts.guild_id, facts.author_id, key)
        try:
            if not await self.still_owner(policy):
                return
            gateway = self._gateway_for(policy)
            hold = self._holds.get(hold_key)
            if hold is None:
                hold = _Hold(facts.channel_id, None, False)
                self._holds[hold_key] = hold
            hold.pending.add(facts.message_id)
            result = await gateway.act(
                self._trigger(
                    facts,
                    reason="held while under review: " + ", ".join(provisional.reasons),
                ),
                [
                    Proposal(
                        kind=gw.TIMEOUT,
                        target_user_id=facts.author_id,
                        target_username=facts.author_name or None,
                        reason=HOLD_REASON,
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
                    " | ".join(text for _, text in lines),
                )
            return None, False
        if not await self.still_owner(policy):
            return None, False

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
            reason=_reason(decision.targets.values()) or "coordinator decision",
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
        scam_text = (
            SECURE_ACCOUNT_TEXT
            if NOTICE_SECURE_ACCOUNT in notices
            else SCAM_REMOVED_TEXT
            if NOTICE_SCAM_REMOVED in notices
            else None
        )
        if target.sanction is not None and target.sanction.kind == gw.WARN:
            if scam_text is not None:
                notice = (
                    f"⚠️ <@{target.user_id}> has been warned.\n**Reason:** {scam_text}"
                )
                dm = (
                    "You have received a warning.\n\n"
                    f"**Reason:** {scam_text}\n\n"
                    "Please review the server rules. Continued violations may "
                    "result in further action (timeout, kick, or ban)."
                )
            else:
                notice = format_warning_message(target.user_id, _spam_reasons(target))
                dm = None
            await self._warn(gateway, trigger, target, facts, notice, dm)
        elif scam_text is not None and target.sanction is not None:
            # A timeout (high-signal or repeat): the member is told by DM,
            # once it took effect (never after a staff refusal or a reversal).
            applied = any(
                outcome.kind == target.sanction.kind
                and outcome.target_user_id == target.user_id
                and outcome.applied
                for outcome in result.outcomes
            )
            if (
                applied
                and result.incident_id is not None
                and await gateway.claim_member_message(
                    result.incident_id,
                    kind=gw.MESSAGE_DM,
                    target_user_id=target.user_id,
                )
            ):
                await self.messenger.dm(target.user_id, scam_text)

        user = f"<@{target.user_id}>"
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
    ) -> None:
        """As the spam engine and the handlers warn: one row, one notice per
        incident (a warn the incident already covers still tells the member
        once), and for a scam a best-effort DM."""
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
            if not send:
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
            await self.messenger.dm(target.user_id, dm)

    # ------------------------------------------------------------------
    # Report lines
    # ------------------------------------------------------------------

    def _report_lines(self, evaluation: Evaluation) -> list[tuple[str, str]]:
        decision = evaluation.to_carry_out
        facts = evaluation.facts
        lines: list[tuple[str, str]] = []
        provisional = decision.provisional
        if provisional is not None:
            lines.append(_provisional_line(provisional, evaluation))
        lines.extend((gw.ENTRY_NOTE, line) for line in decision.review)
        scam_fired = any(
            line.startswith(rules.SCAM_GATE) for line in decision.rules_fired
        )
        if evaluation.history is not None and (scam_fired or provisional is not None):
            lines.append(
                (gw.ENTRY_NOTE, history_note(facts.author_id, evaluation.history))
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


def _reason(targets) -> str:
    return "; ".join(dict.fromkeys(reason for t in targets for reason in t.reasons))


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


def _provisional_line(
    provisional: Provisional, evaluation: Evaluation
) -> tuple[str, str]:
    user = f"<@{provisional.user_id}>"
    if provisional.resolution == UPGRADED:
        target = evaluation.to_carry_out.targets.get(provisional.user_id)
        sanction = target.sanction if target is not None else None
        what = (
            "a ban"
            if sanction is not None and sanction.kind == gw.BAN
            else "a 24-hour timeout"
            if sanction is not None and (sanction.duration_seconds or 0) >= 86400
            else "a longer sanction"
        )
        return (
            gw.ENTRY_NOTE,
            f"Provisional hold on {user} upgraded to {what}.",
        )
    if provisional.resolution == RELEASED:
        return (
            gw.ENTRY_NOTE,
            f"Provisional hold on {user} released: the scam check found nothing.",
        )
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
    if resolution == UPGRADED or evaluation.scam_outcome == "violation":
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
