"""From rule hits to one decision: strongest sanction per target, cleanup once.

* **Staff**: a target the staff-protection rule refuses gets nothing; the
  refusal is kept (``staff_excluded``). An unknown standing refuses, as in
  every actor today.
* **Strongest wins per target**: ban > kick > timeout > warn, a longer
  timeout over a shorter one. Reasons from every rule are kept.
* **Cleanup once**: each message is listed once, whichever rules asked.
* **Notices** are not sanctions: a notice rides along, deduplicated.
* **Provisional timeout**: kept apart from the sanctions. Its resolution says
  what the verdict did with it.

Before the decision is made, :func:`enforceable` takes out what the guild's
policy (#241) does not let the coordinator do: a rule in ``evaluate`` keeps
its finding and loses its effects, and a sanction the policy does not allow
is dropped (a timeout past the policy's longest is shortened to it).
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from smarter_dev.bot.moderation_coordinator.model import Decision
from smarter_dev.bot.moderation_coordinator.model import Provisional
from smarter_dev.bot.moderation_coordinator.model import RuleHit
from smarter_dev.bot.moderation_coordinator.model import TargetDecision
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web.moderation_gateway import types as gw

if TYPE_CHECKING:
    from smarter_dev.bot.moderation_coordinator.policy import GuildPolicy

#: How long the provisional timeout holds: scam-banner's hold, ample for a
#: verdict due within the 10 s deadline. Without a verdict it simply runs out.
PROVISIONAL_SECONDS = 300

#: How a finding the policy does not act on reads on the record.
EVALUATE_ONLY = " (evaluate only: not acted on)"
NOT_ALLOWED = " ({kind} not allowed by the policy)"
#: The policy's name for each sanction the gateway knows.
_POLICY_SANCTION = {
    gw.WARN: mp.WARN,
    gw.TIMEOUT: mp.TIMEOUT,
    gw.BAN: mp.BAN,
    gw.KICK: "kick",
}

UPGRADED = "upgraded"
RELEASED = "released"
#: The verdict landed (a scam) but this message's own decision carries no
#: longer sanction: its copy claimed the post's sanction, or it is a warn.
#: The hold stays under the incident's sanction or runs out; it did not
#: "expire without a verdict" (#333).
DECIDED = "decided"
EXPIRED = "expired"


def enforceable(hits: Sequence[RuleHit], policy: GuildPolicy) -> list[RuleHit]:
    """What of ``hits`` the guild's policy lets the coordinator carry out.

    Only a guild the coordinator acts for, under a policy, is changed: the
    shadow records what each rule found, and a guild without a policy acts
    as it always has."""
    if not policy.acting or policy.revision is None:
        return list(hits)
    max_timeout = policy.document.sanctions.max_timeout_seconds
    may_delete = policy.allows(mp.DELETE)
    kept: list[RuleHit] = []
    for hit in hits:
        if not policy.enforces(hit.rule):
            kept.append(
                replace(
                    hit,
                    reason=hit.reason + EVALUATE_ONLY,
                    sanction=None,
                    cleanup=(),
                    notice=None,
                    high_signal=False,
                )
            )
            continue
        sanction = hit.sanction
        reason = hit.reason
        if sanction is not None:
            name = _POLICY_SANCTION.get(sanction.kind, sanction.kind)
            if not policy.allows(name):
                reason += NOT_ALLOWED.format(kind=name)
                sanction = None
            elif (
                sanction.kind == gw.TIMEOUT
                and (sanction.duration_seconds or 0) > max_timeout
            ):
                sanction = replace(sanction, duration_seconds=max_timeout)
        cleanup = hit.cleanup
        if cleanup and not may_delete:
            reason += NOT_ALLOWED.format(kind=mp.DELETE)
            cleanup = ()
        kept.append(replace(hit, reason=reason, sanction=sanction, cleanup=cleanup))
    return kept


def decide(
    hits: Sequence[RuleHit],
    *,
    refusals: Mapping[str, str | None],
    provisional: Provisional | None = None,
    scam_outcome: str | None = None,
    review: Sequence[str] = (),
) -> Decision:
    """The one decision. ``refusals`` maps every target to its staff refusal.

    ``scam_outcome`` is the scam check's verdict (``violation``, ``clean``,
    ``unknown``) and settles the provisional timeout.
    """
    decision = Decision(review=list(review))
    for hit in hits:
        decision.rules_fired.append(f"{hit.rule}: {hit.reason}")
        refusal = refusals.get(hit.target_user_id, "standing not checked")
        if refusal:
            decision.staff_excluded[hit.target_user_id] = refusal
            continue
        target = decision.targets.get(hit.target_user_id)
        if target is None:
            target = TargetDecision(user_id=hit.target_user_id, name=hit.target_name)
            decision.targets[hit.target_user_id] = target
        target.rules.append(hit.rule)
        target.reasons.append(hit.reason)
        if hit.notice:
            target.notices.append(hit.notice)
        if hit.sanction is not None and hit.sanction.stronger_than(target.sanction):
            target.sanction = hit.sanction
            target.model_justified = hit.model_justified
        elif (
            hit.sanction is not None
            and target.sanction is not None
            and not hit.model_justified
            and hit.sanction.rank == target.sanction.rank
        ):
            # An equally strong deterministic reason stands on its own.
            target.model_justified = False
        seen = {(item.channel_id, item.message_id) for item in target.cleanup}
        for item in hit.cleanup:
            if (item.channel_id, item.message_id) not in seen:
                target.cleanup.append(item)
                seen.add((item.channel_id, item.message_id))
    # A message is cleaned up once, by whichever target posted it first here.
    claimed: set[str] = set()
    for target in decision.targets.values():
        kept = []
        for item in target.cleanup:
            if item.message_id not in claimed:
                kept.append(item)
                claimed.add(item.message_id)
        target.cleanup = kept
    if provisional is not None and not refusals.get(provisional.user_id):
        decision.provisional = _resolve_provisional(
            provisional, decision.targets.get(provisional.user_id), scam_outcome
        )
    return decision


def _resolve_provisional(
    provisional: Provisional, target: TargetDecision | None, scam_outcome: str | None
) -> Provisional:
    sanction = target.sanction if target is not None else None
    if sanction is not None and (
        sanction.kind in (gw.BAN, gw.KICK)
        or (
            sanction.kind == gw.TIMEOUT
            and (sanction.duration_seconds or 0) > provisional.duration_seconds
        )
    ):
        return replace(provisional, resolution=UPGRADED)
    if scam_outcome == "clean":
        return replace(provisional, resolution=RELEASED)
    if scam_outcome == "violation":
        return replace(provisional, resolution=DECIDED)
    return replace(provisional, resolution=EXPIRED)
