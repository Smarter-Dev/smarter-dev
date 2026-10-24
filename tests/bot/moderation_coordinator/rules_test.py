"""The pure rules and the one decision, on plain data."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from smarter_dev.bot.moderation_coordinator import rules
from smarter_dev.bot.moderation_coordinator.decide import decide
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import ANSWERED
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import Attachment
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import Cleanup
from smarter_dev.bot.moderation_coordinator.model import LabelVerdict
from smarter_dev.bot.moderation_coordinator.model import PastAction
from smarter_dev.bot.moderation_coordinator.model import Provisional
from smarter_dev.bot.moderation_coordinator.model import RecentPost
from smarter_dev.bot.moderation_coordinator.model import RuleHit
from smarter_dev.bot.moderation_coordinator.model import Sanction
from smarter_dev.bot.moderation_coordinator.model import Verdicts
from smarter_dev.bot.moderation_coordinator.model import settle
from smarter_dev.bot.moderation_coordinator.policy import SCAM_BANNER_REFERENCE_SHA256
from smarter_dev.bot.moderation_coordinator.policy import AttachmentPolicy
from smarter_dev.bot.moderation_coordinator.policy import script_sha256
from smarter_dev.bot.spam_engine import SpamDecision
from smarter_dev.web.moderation_gateway import types as gw
from tests.bot.moderation_coordinator.replay_cases import NOW
from tests.bot.moderation_coordinator.replay_cases import OTHER
from tests.bot.moderation_coordinator.replay_cases import Member
from tests.bot.moderation_coordinator.replay_cases import say

DAY = 86400
REGULAR = Member("erin", 4, NOW - timedelta(days=900), NOW - timedelta(days=800))
NEWBIE = Member("newbie", 8, NOW - timedelta(days=3), NOW - timedelta(hours=2))
SETTLED = AuthorHistory(is_first_message=False, days_since_last_message=0)


def verdicts(**answers) -> Verdicts:
    return Verdicts({k: settle(k, *v) for k, v in answers.items()})


# -- the scam-banner reference ------------------------------------------------


def test_the_scam_gate_is_pinned_to_the_reference_script():
    """The scam gate is a port of this file. Changing the script without
    porting the change (and updating the pin) fails here."""
    repo = Path(__file__).resolve().parents[3]
    script = (repo / "scripts/handler_scripts/scam-banner.monty").read_text()
    assert script_sha256(script) == SCAM_BANNER_REFERENCE_SHA256


# -- spam engine, attachments, edit-ping, bump channel -----------------------


def test_spam_engine_mute_warn_and_delete_map_to_hits():
    facts = say(REGULAR, "x", NOW)
    mute = rules.spam_engine_hits(
        facts,
        SpamDecision("mute", ("posting scam links",), True),
        mute_duration_seconds=DAY,
    )
    assert mute[0].sanction == Sanction(gw.TIMEOUT, DAY)
    assert mute[0].cleanup == (Cleanup(facts.channel_id, facts.message_id),)
    warn = rules.spam_engine_hits(
        facts,
        SpamDecision("warn", ("mentioning everyone",), True),
        mute_duration_seconds=DAY,
    )
    assert warn[0].sanction == Sanction(gw.WARN)
    assert warn[0].notice == "spam_warning" and warn[0].cleanup
    assert (
        rules.spam_engine_hits(
            facts, SpamDecision("none", (), False), mute_duration_seconds=DAY
        )
        == []
    )


def test_attachment_tiers():
    policy = AttachmentPolicy(ignored_extensions=(".png",), warn_extensions=(".pdf",))

    def files(*names):
        return say(REGULAR, "", NOW, attachments=tuple(Attachment(n) for n in names))

    assert rules.attachment_hits(files("a.png"), policy) == []
    warned = rules.attachment_hits(files("a.png", "b.pdf"), policy)
    assert warned[0].notice == "attachment_warned" and not warned[0].cleanup
    blocked = rules.attachment_hits(files("b.pdf", "c.exe"), policy)
    assert blocked[0].notice == "attachment_blocked" and blocked[0].cleanup
    assert blocked[0].reason == "blocked attachment type .exe"  # never the name
    # #255: after the last dot is still the member's text, unless it looks
    # like a file type.
    odd = rules.attachment_hits(files("x.call me at 555-0100"), policy)
    assert odd[0].reason == "blocked attachment type (unusual extension)"
    bare = rules.attachment_hits(files("README"), policy)
    assert bare[0].reason == "blocked attachment type (no extension)"


def test_edit_ping_only_on_edits_and_case_sensitive_like_the_handler():
    assert rules.edit_ping_hits(say(REGULAR, "hi @everyone", NOW)) == []
    hit = rules.edit_ping_hits(say(REGULAR, "hi @everyone", NOW, is_edit=True))
    assert hit[0].cleanup and hit[0].sanction is None
    assert rules.edit_ping_hits(say(REGULAR, "hi @Everyone", NOW, is_edit=True)) == []


@pytest.mark.parametrize(
    "text", ["mail me@here.com", "@hereby resign", "`@everyone` is a ping"]
)
def test_an_edit_with_an_email_a_longer_word_or_code_is_not_an_edit_ping(text):
    """Round 3, item 4: the edit-ping read a substring, so an edit to
    ``me@here.com`` was removed as a server-wide mention."""
    assert rules.edit_ping_hits(say(REGULAR, text, NOW, is_edit=True)) == []


def test_bump_channel_chatter_is_cleanup_only():
    chatter = say(REGULAR, "bump pls", NOW, channel="B")
    hit = rules.bump_channel_hits(chatter, frozenset({"B"}))
    assert hit[0].cleanup and hit[0].sanction is None
    assert rules.bump_channel_hits(say(REGULAR, "x", NOW), frozenset({"B"})) == []


# -- high signal ---------------------------------------------------------------


def _metrics(**flags):
    from smarter_dev.bot.spam_engine import SpamMetrics

    values = dict.fromkeys(
        (
            "message_rate",
            "channel_spread",
            "duplicate_message",
            "mass_mention",
            "nitro_scam",
            "scam_link",
            "current_message_mass_mention",
            "current_message_nitro_keyword",
        ),
        False,
    )
    values.update(flags)
    return SpamMetrics(**values)


def test_the_high_signal_set():
    plain = say(REGULAR, "see https://example.com", NOW)
    assert rules.high_signal_reasons(plain, metrics=_metrics()) == ()
    assert rules.high_signal_reasons(plain, metrics=_metrics(scam_link=True)) == (
        "scam link",
    )
    assert rules.high_signal_reasons(plain, metrics=_metrics(nitro_scam=True)) == (
        "mass mention with nitro bait",
    )
    ping_link = say(REGULAR, "@here look https://example.com", NOW)
    assert "mass mention with a link" in rules.high_signal_reasons(
        ping_link, metrics=_metrics()
    )
    # A mass mention alone is the spam engine's warn, not a scam event.
    ping = say(REGULAR, "@here standup in 5", NOW)
    assert rules.high_signal_reasons(ping, metrics=_metrics(mass_mention=True)) == ()
    # Without the spam engine only scam-banner's suspect counts.
    assert rules.high_signal_reasons(
        ping_link, metrics=None, suspect=("four or more images",)
    ) == ("four or more images",)


# -- scam gate -----------------------------------------------------------------


def _review(
    member, content, *, history=SETTLED, recent=(), high_signal=(), attachments=()
):
    facts = say(member, content, NOW, attachments=attachments)
    return rules.scam_gate_review(
        facts,
        key="k",
        history=history,
        recent=recent,
        high_signal=high_signal,
        now=NOW,
    ), facts


def test_scam_gate_needs_content_and_a_reason_to_look():
    assert _review(REGULAR, "nice weather")[0] is None  # no content gate
    assert (
        _review(REGULAR, "docs at https://docs.python.org")[0] is None
    )  # settled author
    assert (
        _review(NEWBIE, "docs at https://docs.python.org")[0] is not None
    )  # joined < 1 day
    first = AuthorHistory(is_first_message=True, days_since_last_message=None)
    assert _review(REGULAR, "my wallet", history=first)[0] is not None
    silent = AuthorHistory(is_first_message=False, days_since_last_message=45)
    assert _review(REGULAR, "free nitro", history=silent)[0] is not None
    moderated = AuthorHistory(
        is_first_message=False,
        days_since_last_message=0,
        actions=(PastAction("timeout", NOW - timedelta(days=10)),),
    )
    review, _ = _review(REGULAR, "airdrop", history=moderated)
    assert review is not None and review.repeat
    # A warning is a day of risk only (Zech, Oct 24).
    warned = AuthorHistory(
        is_first_message=False,
        days_since_last_message=0,
        actions=(PastAction("warn", NOW - timedelta(days=10)),),
    )
    assert _review(REGULAR, "airdrop", history=warned)[0] is None
    images = tuple(Attachment(f"{i}.png", "image/png") for i in range(4))
    review, _ = _review(REGULAR, "", attachments=images)
    assert review.suspect == ("four or more images",)
    # An image the uploader's client left unlabelled still counts.
    unlabelled = tuple(Attachment(f"{i}.png") for i in range(3)) + (
        Attachment("blob", width=640, height=480),
    )
    review, _ = _review(REGULAR, "", attachments=unlabelled)
    assert review.suspect == ("four or more images",)
    # High signal alone opens a review of an established account.
    assert (
        _review(REGULAR, "https://x.example", high_signal=("scam link",))[0] is not None
    )


def test_a_cross_channel_copy_within_ten_seconds_is_a_burst():
    copy = RecentPost(OTHER, "999", NOW.timestamp() - 4, "k")
    review, _ = _review(REGULAR, "https://x.example", recent=(copy,))
    assert review.suspect == ("same post in several channels within 10 s",)
    assert [p.message_id for p in review.copies] == ["999"]
    late = RecentPost(OTHER, "998", NOW.timestamp() - 30, "k")
    assert _review(REGULAR, "https://x.example", recent=(late,))[0] is None


@pytest.mark.parametrize(
    ("member", "high_signal", "history", "expected"),
    [
        (NEWBIE, (), SETTLED, (gw.BAN, None)),
        (REGULAR, ("scam link",), SETTLED, (gw.TIMEOUT, DAY)),
        (
            REGULAR,
            (),
            AuthorHistory(False, 0, (PastAction("timeout", NOW - timedelta(days=3)),)),
            (gw.TIMEOUT, DAY),
        ),
        (REGULAR, (), AuthorHistory(True, None), (gw.WARN, None)),
    ],
)
def test_a_confirmed_scam_ladder(member, high_signal, history, expected):
    review, facts = _review(
        member, "free nitro https://x.example", history=history, high_signal=high_signal
    )
    hits, outcome, _ = review.resolve(verdicts(scam=(True, 0.9), abuse=(False, 0.9)))
    assert outcome == "violation"
    assert (hits[0].sanction.kind, hits[0].sanction.duration_seconds) == expected
    assert hits[0].model_justified
    assert hits[0].cleanup[0].message_id == facts.message_id


def test_clean_and_unknown_verdicts_propose_nothing():
    review, _ = _review(NEWBIE, "https://x.example")
    assert review.resolve(verdicts(scam=(False, 0.9), abuse=(False, 0.8)))[:2] == (
        [],
        "clean",
    )
    hits, outcome, lines = review.resolve(
        verdicts(scam=(True, 0.5), abuse=(False, 0.9))
    )
    assert (hits, outcome) == ([], "unknown")
    assert "too unsure" in lines[0]
    hits, outcome, _ = review.resolve(Verdicts({SCAM: LabelVerdict(SCAM, "timeout")}))
    assert (hits, outcome) == ([], "unknown")


# -- decide --------------------------------------------------------------------


def test_strongest_per_target_cleanup_once_and_staff_excluded():
    a, b = "A", "B"
    hits = [
        RuleHit("spam_engine", "flood", a, Sanction(gw.WARN), (Cleanup("C", "1"),)),
        RuleHit(
            "scam_gate",
            "scam",
            a,
            Sanction(gw.TIMEOUT, 600),
            (Cleanup("C", "1"), Cleanup("C", "2")),
            model_justified=True,
        ),
        RuleHit(
            "spam_ring", "ring", a, Sanction(gw.TIMEOUT, 7 * DAY), model_justified=True
        ),
        RuleHit("spam_engine", "ping", b, Sanction(gw.WARN), (Cleanup("C", "3"),)),
    ]
    decision = decide(hits, refusals={a: None, b: "holds a staff role"})
    target = decision.targets[a]
    assert target.sanction == Sanction(gw.TIMEOUT, 7 * DAY)
    assert [c.message_id for c in target.cleanup] == ["1", "2"]
    assert set(target.rules) == {"spam_engine", "scam_gate", "spam_ring"}
    assert decision.staff_excluded == {b: "holds a staff role"}
    assert b not in decision.targets
    assert decision.strongest == gw.TIMEOUT
    assert decision.acted_on == [a]


def test_an_unchecked_target_is_never_acted_on():
    decision = decide([RuleHit("x", "y", "A", Sanction(gw.BAN))], refusals={})
    assert decision.targets == {}
    assert "A" in decision.staff_excluded


def test_a_deterministic_reason_of_equal_strength_is_not_model_only():
    hits = [
        RuleHit(
            "scam_gate", "scam", "A", Sanction(gw.TIMEOUT, DAY), model_justified=True
        ),
        RuleHit("spam_engine", "scam link", "A", Sanction(gw.TIMEOUT, DAY)),
    ]
    assert decide(hits, refusals={"A": None}).targets["A"].model_justified is False


@pytest.mark.parametrize(
    ("sanction", "outcome", "resolution"),
    [
        (Sanction(gw.BAN), "violation", "upgraded"),
        (Sanction(gw.TIMEOUT, DAY), "violation", "upgraded"),
        (None, "clean", "released"),
        (None, "unknown", "expired"),
        # A verdict landed: the hold is the incident's to resolve (#333).
        (Sanction(gw.WARN), "violation", "decided"),
        (None, "violation", "decided"),
    ],
)
def test_provisional_resolution(sanction, outcome, resolution):
    hits = [RuleHit("scam_gate", "s", "A", sanction)] if sanction else []
    decision = decide(
        hits,
        refusals={"A": None},
        provisional=Provisional("A", 300, ("scam link",)),
        scam_outcome=outcome,
    )
    assert decision.provisional.resolution == resolution


def test_no_provisional_for_a_protected_author():
    decision = decide(
        [],
        refusals={"A": "holds a staff role"},
        provisional=Provisional("A", 300, ("x",)),
    )
    assert decision.provisional is None


def test_settle_bounds():
    assert settle("x", True, 0.7).outcome == ANSWERED
    assert settle("x", True, 0.69).outcome == "low_confidence"
    assert settle("x", None, 0.9).outcome == "invalid_output"
    assert settle("x", False, None).outcome == "low_confidence"
    assert not settle(ABUSE, True, 0.2).yes


@pytest.mark.parametrize(
    "text",
    [
        "@everyone logger.info(msg) is broken",
        "@here self.store.get(key) returns None",
        "@everyone check github.com",
        "@here foo.bar(1)",
    ],
)
def test_a_mass_mention_next_to_code_or_a_bare_domain_is_not_high_signal(text):
    """#310 item 7: a bare domain or a dotted name is not a link for the
    mention+link check, so a staff-style @everyone with code is not held."""
    message = say(REGULAR, text, NOW)
    assert "mass mention with a link" not in rules.high_signal_reasons(
        message, metrics=_metrics()
    )


@pytest.mark.parametrize(
    "text",
    [
        "@everyone join discord.gg/x",
        "@here https://scam.ru/claim",
        "@everyone www.x.io",
    ],
)
def test_a_mass_mention_with_a_scheme_link_or_invite_is_high_signal(text):
    message = say(REGULAR, text, NOW)
    assert "mass mention with a link" in rules.high_signal_reasons(
        message, metrics=_metrics()
    )


@pytest.mark.parametrize(
    "text",
    [
        "logger.info(msg) is broken",
        "call self.store.get(key)",
        "`self.store`",
        "foo.bar(1)",
        "the app.run() call",
    ],
)
def test_code_snippets_do_not_open_the_scam_gate(text):
    assert not rules.scam_gate_content(say(REGULAR, text, NOW))


@pytest.mark.parametrize(
    "text",
    ["discord.gg/x", "claim it scam.ru/claim", "go to scam.ru", "github.com is up"],
)
def test_a_known_tld_before_a_slash_space_or_the_end_opens_the_scam_gate(text):
    assert rules.scam_gate_content(say(REGULAR, text, NOW))


# -- the re-review of a326953a (#310 items 5 and 6) ----------------------------


@pytest.mark.parametrize("text", ["a@everyone.com", "mail me@here.com", "@hereby"])
def test_an_email_or_a_longer_word_is_not_a_mass_mention(text):
    """Re-review item 5: these were removed and left the author high risk."""
    assert rules.mass_mention_hits(say(REGULAR, text, NOW)) == []


@pytest.mark.parametrize("text", ["hey @everyone", "@here.", "(@everyone)"])
def test_a_mass_mention_as_a_word_still_counts(text):
    assert rules.mass_mention_hits(say(REGULAR, text, NOW)) != []


def test_a_mass_mention_in_code_with_a_docs_link_is_not_high_signal():
    """Re-review item 6: the mention+link check honours the code exemption."""
    in_code = say(
        REGULAR, "`@everyone` is escaped, see https://docs.python.org/3/", NOW
    )
    assert "mass mention with a link" not in rules.high_signal_reasons(
        in_code, metrics=_metrics()
    )
    plain = say(REGULAR, "@everyone see https://docs.python.org/3/", NOW)
    assert "mass mention with a link" in rules.high_signal_reasons(
        plain, metrics=_metrics()
    )


def test_an_action_after_the_message_is_no_earlier_action():
    """Review item 2: a warning recorded after the message (its own early
    mass-mention warning) is not an earlier one: neither a repeat nor a risk."""
    history = AuthorHistory(
        is_first_message=False,
        days_since_last_message=0,
        actions=(PastAction(gw.WARN, NOW + timedelta(seconds=1)),),
    )
    assert rules.actions_within(history, rules.RECENT_ACTION_SECONDS, NOW) == []
    assert rules.recent_moderation(history, NOW) == []
