"""#241: the coordinator under a versioned policy.

Live-shaped messages through the same harness as ``act_test`` (the real
gateway over a fake Discord port), for a guild whose rules come from a policy
document instead of the old tables: the same actions as before the policy at
its imported values, nothing destructive when the policy allows nothing or
only evaluates, and a revision that moves between the decision and the
dispatch is honoured before anything is sent.
"""

from __future__ import annotations

from dataclasses import replace

from smarter_dev.bot.moderation_coordinator import rules
from smarter_dev.bot.moderation_coordinator.decide import EVALUATE_ONLY
from smarter_dev.bot.moderation_coordinator.decide import enforceable
from smarter_dev.bot.moderation_coordinator.model import RuleHit
from smarter_dev.bot.moderation_coordinator.model import Sanction
from smarter_dev.bot.moderation_coordinator.ownership import COORDINATOR
from smarter_dev.bot.moderation_coordinator.ownership import RULES
from smarter_dev.bot.moderation_coordinator.ownership import Ownership
from smarter_dev.bot.moderation_coordinator.policy import from_document
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web.models import DEFAULT_SCAM_LINK_DOMAINS
from smarter_dev.web.moderation_gateway import types as gw
from tests.bot.moderation_coordinator.act_test import REPORTS
from tests.bot.moderation_coordinator.act_test import entries
from tests.bot.moderation_coordinator.act_test import harness
from tests.bot.moderation_coordinator.replay_cases import BUMP
from tests.bot.moderation_coordinator.replay_cases import GUILD
from tests.bot.moderation_coordinator.replay_cases import scam_1749

DESTRUCTIVE = {"timeout", "delete", "purge", "ban", "kick"}


def document(**over) -> mp.PolicyDocument:
    """smarter.dev's policy as the import writes it from the live config."""
    raw = mp.default_document().as_json()
    for rule in mp.RULES:
        raw["rules"][rule]["mode"] = mp.ENFORCE
    raw["rules"]["spam_engine"]["scam_link_domains"] = list(DEFAULT_SCAM_LINK_DOMAINS)
    raw["rules"]["attachment_filter"].update(
        ignored_extensions=[".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4"],
        warn_extensions=[".txt", ".pdf", ".py"],
    )
    raw["rules"]["bump_channel"]["channels"] = [BUMP]
    raw["destinations"]["report_channel_id"] = REPORTS
    for path, value in over.items():
        node = raw
        *parents, leaf = path.split("__")
        for part in parents:
            node = node[part]
        node[leaf] = value
    return mp.validate(raw)


def owned(revision: int = 1) -> Ownership:
    return Ownership(GUILD, COORDINATOR, 1, frozenset(RULES), policy_revision=revision)


def policy_from(doc: mp.PolicyDocument, revision: int = 1):
    return from_document(GUILD, doc, revision=revision, ownership=owned(revision))


def on_revision(h, current: dict) -> None:
    """The executor reads ownership and the policy as the bot does: the
    revision ``current`` names, and that revision's policy."""

    async def ownership(_guild_id):
        return owned(current["revision"])

    async def policies(_guild_id):
        return current["policy"]

    h.executor._ownership = ownership
    h.executor._policies = policies


def destructive(h) -> list[str]:
    return [call[0] for call in h.port.calls if call[0] in DESTRUCTIVE]


async def test_the_imported_policy_acts_on_the_1749_scam_as_the_old_tables_did():
    case, scammer = scam_1749(account_age_days=2)
    case = replace(case, policy=policy_from(document()))
    h = harness(case)
    on_revision(h, {"revision": 1, "policy": case.policy})

    await h.judge(case.messages[0])

    assert [call[0] for call in h.port.calls] == ["timeout", "delete", "ban"]
    assert h.port.calls[2][:2] == ("ban", scammer.id)
    assert {p["channel_id"] for p in h.port.posts} == {REPORTS}


async def test_no_allowed_sanction_means_no_destructive_call():
    """An empty allowlist is empty: the scam is judged and reported, and
    Discord is asked for nothing."""
    case, _ = scam_1749(account_age_days=2)
    case = replace(case, policy=policy_from(document(sanctions__allowed=[])))
    h = harness(case)
    on_revision(h, {"revision": 1, "policy": case.policy})

    await h.judge(case.messages[0])

    assert destructive(h) == []
    [row] = h.recorder.rows
    fired = row.rules
    assert any("ban not allowed by the policy" in line for line in fired)
    assert any("timeout not allowed by the policy" in line for line in fired)


async def test_rules_in_evaluate_are_recorded_and_never_acted_on():
    doc = document(
        **{f"rules__{rule}__mode": mp.EVALUATE for rule in mp.RULES},
    )
    case, _ = scam_1749(account_age_days=2)
    case = replace(case, policy=policy_from(doc))
    h = harness(case)
    on_revision(h, {"revision": 1, "policy": case.policy})

    await h.judge(case.messages[0])

    assert h.port.calls == []
    assert h.messenger.posts == [] and h.messenger.dms == []
    [row] = h.recorder.rows
    fired = row.rules
    assert fired and all(EVALUATE_ONLY in line for line in fired)
    assert row.policy["revision"] == 1 and row.policy["enforced"] == []


async def test_enforcement_switched_off_guild_wide_acts_on_nothing():
    case, _ = scam_1749(account_age_days=2)
    case = replace(case, policy=policy_from(document(controls__enforce=False)))
    h = harness(case)
    on_revision(h, {"revision": 1, "policy": case.policy})

    await h.judge(case.messages[0])

    assert h.port.calls == []


async def test_a_revision_saved_before_dispatch_is_honoured():
    """Decided under revision 1 (bans allowed); revision 2, which allows no
    ban, is current when the ban is due. The spam mute and removal revision
    2 still allows go out; the ban does not, and the report says why."""
    case, scammer = scam_1749(account_age_days=2)
    case = replace(case, policy=policy_from(document()))
    h = harness(case)
    stricter = policy_from(
        document(sanctions__allowed=[mp.WARN, mp.TIMEOUT, mp.DELETE]), revision=2
    )
    on_revision(h, {"revision": 2, "policy": stricter})

    await h.judge(case.messages[0])

    assert [call[0] for call in h.port.calls] == ["timeout", "delete"]
    notes = await entries(gw.ENTRY_NOTE)
    assert any(
        f"Not carried out for <@{scammer.id}>" in note
        and "revision 2" in note
        and "ban no longer allowed" in note
        for note in notes
    )


async def test_a_rule_switched_to_evaluate_before_dispatch_stops_its_action():
    case, _ = scam_1749(account_age_days=2)
    case = replace(case, policy=policy_from(document()))
    h = harness(case)
    doc = document(
        rules__spam_engine__mode=mp.EVALUATE, rules__scam_gate__mode=mp.EVALUATE
    )
    on_revision(h, {"revision": 2, "policy": policy_from(doc, revision=2)})

    await h.judge(case.messages[0])

    # The mass-mention removal is still enforced; the spam mute and the ban
    # (and the hold the scam gate would place) are not.
    assert "ban" not in destructive(h) and "timeout" not in destructive(h)


async def test_a_policy_that_cannot_be_read_before_dispatch_acts_on_nothing():
    case, _ = scam_1749(account_age_days=2)
    case = replace(case, policy=policy_from(document()))
    h = harness(case)

    async def ownership(_guild_id):
        return owned(2)

    async def broken(_guild_id):
        raise RuntimeError("database down")

    h.executor._ownership = ownership
    h.executor._policies = broken

    await h.judge(case.messages[0])

    assert destructive(h) == []


async def test_no_dms_and_no_channel_notices_when_the_policy_says_so():
    case, _ = scam_1749(account_age_days=30)
    doc = document(destinations__member_dms=False, destinations__channel_notices=False)
    case = replace(case, policy=policy_from(doc))
    h = harness(case)
    on_revision(h, {"revision": 1, "policy": case.policy})

    await h.judge(case.messages[0])

    assert h.messenger.dms == []
    assert [c for c, _ in h.messenger.posts if c != REPORTS] == []


# -- the gate itself ------------------------------------------------------------


def _hit(rule: str, sanction: Sanction | None, cleanup=()):
    return RuleHit(
        rule=rule,
        reason="why",
        target_user_id="5001",
        sanction=sanction,
        cleanup=cleanup,
        notice="n",
    )


def test_a_guild_without_a_policy_or_in_shadow_is_not_gated():
    hit = _hit(rules.SPAM_ENGINE, Sanction(gw.BAN))
    no_policy = replace(policy_from(document()), revision=None)
    shadow = replace(policy_from(document(sanctions__allowed=[])), owner="legacy")
    assert enforceable([hit], no_policy) == [hit]
    assert enforceable([hit], shadow) == [hit]


def test_the_gate_drops_what_is_not_allowed_and_shortens_long_timeouts():
    from smarter_dev.bot.moderation_coordinator.model import Cleanup

    gated = policy_from(
        document(
            sanctions__allowed=[mp.TIMEOUT],
            sanctions__max_timeout_seconds=3600,
            sanctions__scam_timeout_seconds=3600,
        )
    )
    cleanup = (Cleanup("7001", "8001"),)
    ban, timeout = enforceable(
        [
            _hit(rules.SPAM_ENGINE, Sanction(gw.BAN), cleanup),
            _hit(rules.SPAM_RING, Sanction(gw.TIMEOUT, duration_seconds=86400)),
        ],
        gated,
    )
    assert ban.sanction is None and ban.cleanup == ()
    assert "ban not allowed" in ban.reason and "delete not allowed" in ban.reason
    assert timeout.sanction.duration_seconds == 3600


def test_provisional_signals_the_policy_does_not_list_hold_nobody():
    reasons = (rules.HIGH_SIGNAL_SCAM_LINK, rules.HIGH_SIGNAL_BURST)
    assert rules.holding_signals(reasons, [mp.SIGNAL_BURST]) == (
        rules.HIGH_SIGNAL_BURST,
    )
    assert rules.holding_signals(reasons, []) == ()


def test_the_policys_risk_windows_decide_who_is_high_risk():
    from datetime import timedelta

    from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
    from smarter_dev.bot.moderation_coordinator.model import PastAction
    from tests.bot.moderation_coordinator.replay_cases import ESTABLISHED
    from tests.bot.moderation_coordinator.replay_cases import NOW
    from tests.bot.moderation_coordinator.replay_cases import Member
    from tests.bot.moderation_coordinator.replay_cases import say

    member = Member("w", 77, NOW - timedelta(days=400), NOW - timedelta(days=300))
    warned_2_days_ago = replace(
        ESTABLISHED,
        actions=(PastAction(action_type="warn", created_at=NOW - timedelta(days=2)),),
    )
    facts = say(member, "hi", NOW)
    default = rules.RiskRules.from_policy(mp.default_document())
    assert rules.scam_risky_author(facts, warned_2_days_ago, NOW, default) == ()
    longer = rules.RiskRules.from_policy(
        document(risk__windows={"warn": 3 * 86400, "timeout": 0, "kick": 0, "ban": 0})
    )
    assert rules.scam_risky_author(facts, warned_2_days_ago, NOW, longer) == (
        "moderated recently",
    )
    first = AuthorHistory(is_first_message=True, days_since_last_message=None)
    off = rules.RiskRules.from_policy(document(risk__first_message=False))
    assert "first message" in rules.scam_risky_author(facts, first, NOW, default)
    assert rules.scam_risky_author(facts, first, NOW, off) == ()
