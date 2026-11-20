"""A newer member's same post in two channels is rated promotional spam (#407).

Oct 27 2026: dannieloficial joined three days earlier, had never posted, and
pasted the same promotion into two channels 18 s apart. There was no link and
no risky word, so only the spam engine's "please stop repeating" warning
fired. A copy in a second channel from a newer member now asks Jev for a
``promotional_spam`` rating: a confident yes removes every copy and times
the member out for an hour; anything else keeps the warning and says why.

Every action that does not @mention the member in the channel tells them
by DM (Zech, Oct 27).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import timedelta

from smarter_dev.bot.moderation_coordinator import act as act_module
from smarter_dev.bot.moderation_coordinator import rules
from smarter_dev.bot.moderation_coordinator.classifier import PROMO_POLICY
from smarter_dev.bot.moderation_coordinator.classifier import ClassifierRequest
from smarter_dev.bot.moderation_coordinator.classifier import jev_request
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import ANSWERED
from smarter_dev.bot.moderation_coordinator.model import BAD_FAITH
from smarter_dev.bot.moderation_coordinator.model import LOW_CONFIDENCE
from smarter_dev.bot.moderation_coordinator.model import PROMOTIONAL_SPAM
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import Attachment
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import LabelVerdict
from smarter_dev.bot.moderation_coordinator.model import RecentPost
from smarter_dev.bot.moderation_coordinator.model import Verdicts
from smarter_dev.bot.moderation_coordinator.model import percent
from smarter_dev.bot.moderation_coordinator.replay import ReplayCase
from smarter_dev.bot.moderation_coordinator.replay import ReplayReads
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web.moderation_gateway import types as gw
from tests.bot.moderation_coordinator.act_test import FIRST
from tests.bot.moderation_coordinator.act_test import REPORTS
from tests.bot.moderation_coordinator.act_test import _flood
from tests.bot.moderation_coordinator.act_test import acting
from tests.bot.moderation_coordinator.act_test import harness
from tests.bot.moderation_coordinator.policy_revision_test import document
from tests.bot.moderation_coordinator.policy_revision_test import on_revision
from tests.bot.moderation_coordinator.policy_revision_test import policy_from
from tests.bot.moderation_coordinator.replay_cases import GENERAL
from tests.bot.moderation_coordinator.replay_cases import NOW
from tests.bot.moderation_coordinator.replay_cases import OTHER
from tests.bot.moderation_coordinator.replay_cases import Member
from tests.bot.moderation_coordinator.replay_cases import policy
from tests.bot.moderation_coordinator.replay_cases import say
from tests.bot.moderation_coordinator.replay_cases import scam_1749

DANNIE = Member(
    "dannieloficial", 407, NOW - timedelta(days=40), NOW - timedelta(days=3)
)
REGULAR = Member("helper", 408, NOW - timedelta(days=900), NOW - timedelta(days=600))
#: Joined after ``member_activity`` began recording (2026-07-02), not this week.
LATECOMER = Member(
    "latecomer", 409, NOW - timedelta(days=900), NOW - timedelta(days=30)
)
PROMO = (
    "🔥 Grow your channel fast! Real followers and views, packages from $5. "
    "Message me on Instagram @dannie.growth for prices"
)
QUESTION = (
    "why does my `for i in range(len(xs))` loop skip the last item? "
    "I'm calling xs.pop() inside it"
)
#: A regular: a first message long before today.
REGULAR_HISTORY = AuthorHistory(
    is_first_message=False,
    days_since_last_message=0,
    first_message_at=NOW - timedelta(days=500),
)


def _two_channels(member: Member, text: str):
    """The incident's shape: two channels, 18 s apart."""
    return [
        say(member, text, NOW),
        say(member, text, NOW + timedelta(seconds=18), channel=OTHER),
    ]


def _case(member: Member, posts, history, answers=None):
    return ReplayCase(
        name="promo burst",
        policy=acting(policy(ring=None)),
        messages=posts,
        answers=answers or {},
        reads=ReplayReads(histories={member.id: history}),
    )


def _long_timeouts(h, user_id: str) -> list:
    return [
        call
        for call in h.port.calls
        if call[0] == "timeout" and call[1] == user_id and call[2] - h.clock[0] > 600
    ]


async def test_the_dannieloficial_burst_is_a_timeout_both_copies_gone_and_a_report(
    caplog,
):
    posts = _two_channels(DANNIE, PROMO)
    h = harness(_case(DANNIE, posts, FIRST, {PROMOTIONAL_SPAM: (True, 0.94)}))

    with caplog.at_level(logging.INFO):
        await h.judge(*posts)

    # Jev was asked once, on the copy, with the author's record.
    assert len(h.calls.jev) == 1 and h.calls.haiku == []
    asked = h.calls.jev[0]
    assert asked["fields"] == [PROMOTIONAL_SPAM]
    assert "joined 3 days ago" in asked["material"]
    assert "no messages before this burst" in asked["material"]
    assert "2 copies in 2 channels within 18 s" in asked["material"]
    # Both copies are removed and the member is timed out for an hour.
    assert {p.message_id for p in posts} <= h.deleted()
    assert h.port.count("timeout", DANNIE.id) == 1
    row = h.recorder.rows[-1]
    assert row.decision["targets"][DANNIE.id]["sanction"] == {
        "kind": gw.TIMEOUT,
        "duration_seconds": 3600,
    }
    # The report gives the rating as a percentage, with the evidence.
    report = h.port.latest_report_text(REPORTS)
    assert "Jev: 94% promotional spam" in report
    assert "0.94" not in report
    assert "joined 3 days ago" in report
    # The member is told by DM: what, that it was removed, how long, appeal.
    [(user, dm)] = h.messenger.dms
    assert user == DANNIE.id
    assert "Grow your channel fast!" in dm
    assert "was removed, and you are timed out for 1 hour" in dm
    assert "Jev: 94% promotional spam" in dm
    assert "appeal" in dm
    # One INFO line for the decision: user, rule, score, action.
    [line] = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Coordinator decision")
        and PROMOTIONAL_SPAM in r.getMessage()
    ]
    assert DANNIE.id in line and "scam_gate" in line
    assert "promotional_spam=yes, confidence 94%" in line
    assert "action=timeout 3600s" in line


async def test_an_established_member_reposting_a_question_is_only_warned():
    posts = _two_channels(REGULAR, QUESTION)
    h = harness(
        _case(REGULAR, posts, REGULAR_HISTORY, {PROMOTIONAL_SPAM: (True, 0.99)})
    )

    await h.judge(*posts)

    # No rating asked, nothing timed out or removed beyond the engine's own.
    assert h.calls.total == 0
    assert _long_timeouts(h, REGULAR.id) == []
    assert posts[0].message_id not in h.deleted()
    # The spam engine's warning @mentions them in the channel: no DM too.
    warnings = [text for _, text in h.messenger.posts if f"<@{REGULAR.id}>" in text]
    assert warnings
    assert h.messenger.dms == []


async def test_a_long_standing_member_with_no_activity_row_is_only_warned():
    """#408: member_activity began on 2026-07-02 and was never backfilled, so
    a member who joined 600 days ago and has not posted since has no row.
    That is not "never posted": no rating, only the engine's warning."""
    posts = _two_channels(REGULAR, PROMO)
    h = harness(_case(REGULAR, posts, FIRST, {PROMOTIONAL_SPAM: (True, 0.99)}))

    await h.judge(*posts)

    assert h.calls.total == 0
    assert _long_timeouts(h, REGULAR.id) == []
    assert posts[0].message_id not in h.deleted()
    assert any(f"<@{REGULAR.id}>" in text for _, text in h.messenger.posts)


async def test_a_spam_warning_is_told_by_dm_when_channel_notices_are_off():
    """#408: with channel notices off the warning is posted nowhere, so with
    member DMs on it goes by DM."""
    posts = _two_channels(REGULAR, QUESTION)
    case = _case(REGULAR, posts, REGULAR_HISTORY)
    document = case.policy.document
    quiet = replace(
        case.policy,
        document=document.model_copy(
            update={
                "destinations": document.destinations.model_copy(
                    update={"channel_notices": False}
                )
            }
        ),
    )
    h = harness(replace(case, policy=quiet))

    await h.judge(*posts)

    assert [text for _, text in h.messenger.posts if f"<@{REGULAR.id}>" in text] == []
    [(user, dm)] = h.messenger.dms
    assert user == REGULAR.id
    assert dm.startswith("You have received a warning.")
    assert "**Reason:** Repeating the same message" in dm


async def test_a_removal_the_channel_notice_mentions_sends_no_dm():
    """A blocked attachment is removed and its notice @mentions the member
    in the channel: that is the telling, so no DM as well."""
    post = say(
        REGULAR,
        "here is the tool",
        NOW,
        attachments=(
            Attachment(
                "tool.exe",
                "application/octet-stream",
                90_000,
                "https://cdn.discordapp.com/a/1/tool.exe",
            ),
        ),
    )
    h = harness(_case(REGULAR, [post], REGULAR_HISTORY))

    await h.judge(post)

    assert post.message_id in h.deleted()
    assert any(f"<@{REGULAR.id}>" in text for _, text in h.messenger.posts)
    assert h.messenger.dms == []


async def test_a_not_spam_rating_keeps_the_warning_and_says_so():
    posts = _two_channels(DANNIE, PROMO)
    h = harness(_case(DANNIE, posts, FIRST, {PROMOTIONAL_SPAM: (False, 0.88)}))

    await h.judge(*posts)

    assert len(h.calls.jev) == 1
    assert _long_timeouts(h, DANNIE.id) == []
    assert posts[0].message_id not in h.deleted()
    report = h.port.latest_report_text(REPORTS)
    assert "warned, Jev said not spam (88% sure)" in report
    # The warning names them in the channel; nothing by DM.
    assert any(f"<@{DANNIE.id}>" in text for _, text in h.messenger.posts)
    assert h.messenger.dms == []


async def test_an_unsure_yes_is_not_acted_on():
    posts = _two_channels(DANNIE, PROMO)
    h = harness(_case(DANNIE, posts, FIRST, {PROMOTIONAL_SPAM: (True, 0.55)}))

    await h.judge(*posts)

    assert _long_timeouts(h, DANNIE.id) == []
    report = h.port.latest_report_text(REPORTS)
    assert "Jev said spam but was too unsure (55% sure)" in report


async def test_a_spam_mute_tells_the_member_by_dm():
    """A delete + timeout nobody is @mentioned about: told by DM."""
    spammer = Member("raider", 81, NOW - timedelta(days=400), NOW - timedelta(days=300))
    first = say(spammer, "buy now", NOW)
    case = ReplayCase(
        name="flood",
        policy=acting(policy(ring=None, scam_gate=False)),
        messages=[first],
        reads=ReplayReads(histories={spammer.id: REGULAR_HISTORY}),
    )
    h = harness(case)
    h.port.add_member(spammer.id)

    await _flood(h, spammer)
    await h.shadow.drain()

    assert h.port.count("timeout", spammer.id) == 1
    dms = [text for user, text in h.messenger.dms if user == spammer.id]
    assert len(dms) == 1
    assert "and you are timed out for 1 day" in dms[0]
    assert "appeal" in dms[0]


async def test_a_ban_is_told_by_dm_before_the_ban():
    case, scammer = scam_1749(account_age_days=2)
    case = replace(case, policy=acting(case.policy))
    h = harness(case)
    sent_at: list[int] = []
    dm = h.messenger.dm

    async def recording(user_id, content):
        sent_at.append(len(h.port.calls))
        return await dm(user_id, content)

    h.messenger.dm = recording

    await h.judge(case.messages[0])

    kinds = [call[0] for call in h.port.calls]
    ban_at = kinds.index("ban")
    bans = [
        text
        for (_, text), at in zip(h.messenger.dms, sent_at, strict=True)
        if "you are banned" in text and at <= ban_at
    ]
    assert len(bans) == 1
    assert "**Smarter Dev**" in bans[0] and "appeal" in bans[0]


# -- the rule's parts ----------------------------------------------------------


def _recent(facts, *, channel: str, seconds_before: float) -> RecentPost:
    return RecentPost(
        channel_id=channel,
        message_id="1",
        posted_at=facts.posted_at - seconds_before,
        content_hash="h",
        digest="d",
    )


def _review(member: Member, history, *, now_offset: float = 0.0, **over):
    facts = say(member, PROMO, NOW)
    copies = over.pop("copies", (_recent(facts, channel=OTHER, seconds_before=18),))
    return rules.promo_review(
        facts,
        key="k",
        copies=copies,
        history=history,
        now=NOW + timedelta(seconds=now_offset),
        timeout_seconds=3600,
        **over,
    )


def test_a_newer_member_is_one_who_joined_this_week_or_never_posted_before():
    joined_this_week = _review(DANNIE, REGULAR_HISTORY)
    assert joined_this_week is not None
    assert joined_this_week.newer == ("joined 3 days ago",)
    silent = _review(LATECOMER, FIRST)
    assert silent is not None
    assert silent.newer == ("no messages before this burst",)
    assert _review(REGULAR, REGULAR_HISTORY) is None


def test_no_activity_row_counts_only_for_members_who_joined_since_tracking():
    """#408: no row (or a row this burst made) means "never posted" only for
    a member who joined on or after rules.ACTIVITY_TRACKED_SINCE."""
    assert _review(REGULAR, FIRST) is None
    unknown_join = replace(REGULAR, joined=None)
    assert _review(unknown_join, FIRST) is None
    on_the_day = replace(REGULAR, joined=rules.ACTIVITY_TRACKED_SINCE)
    assert _review(on_the_day, FIRST).newer == ("no messages before this burst",)
    day_before = replace(
        REGULAR, joined=rules.ACTIVITY_TRACKED_SINCE - timedelta(seconds=1)
    )
    assert _review(day_before, FIRST) is None


def test_the_burst_itself_is_not_an_earlier_message():
    """In production the first copy is in member_activity by the time the
    second is judged: a first message at the burst's start is still none."""
    burst_start = NOW - timedelta(seconds=18)
    during = AuthorHistory(
        is_first_message=False,
        days_since_last_message=None,
        first_message_at=burst_start + timedelta(seconds=1),
    )
    assert _review(LATECOMER, during).newer == ("no messages before this burst",)
    # Joined before tracking began: a row this burst made says nothing.
    assert _review(REGULAR, during) is None
    before = replace(during, first_message_at=burst_start - timedelta(minutes=5))
    assert _review(LATECOMER, before) is None
    # Posted before, when is not on record: not newer on that count.
    unrecorded = AuthorHistory(is_first_message=False, days_since_last_message=3)
    assert _review(REGULAR, unrecorded) is None


def test_one_channel_or_a_short_line_is_no_promo_check():
    facts = say(DANNIE, PROMO, NOW)
    same_channel = (_recent(facts, channel=GENERAL, seconds_before=5),)
    assert _review(DANNIE, FIRST, copies=same_channel) is None
    short = say(DANNIE, "thanks!!", NOW)
    assert (
        rules.promo_review(
            short,
            key="k",
            copies=(_recent(short, channel=OTHER, seconds_before=5),),
            history=FIRST,
            now=NOW,
            timeout_seconds=3600,
        )
        is None
    )


def _verdict(answer: bool, confidence: float, outcome: str = ANSWERED) -> Verdicts:
    return Verdicts(
        {
            PROMOTIONAL_SPAM: LabelVerdict(
                label=PROMOTIONAL_SPAM,
                answer=answer,
                confidence=confidence,
                outcome=outcome,
            )
        }
    )


def test_a_confident_yes_times_out_and_removes_every_copy():
    review = _review(DANNIE, FIRST)
    hits, lines = review.resolve(_verdict(True, 0.94))
    [hit] = hits
    assert hit.sanction.kind == gw.TIMEOUT and hit.sanction.duration_seconds == 3600
    assert {c.message_id for c in hit.cleanup} == {review.facts.message_id, "1"}
    [line] = lines
    assert line.startswith(rules.PROMO_LINE)
    assert "Jev: 94% promotional spam" in line and "timed out" in line


def test_a_copy_is_removed_but_never_sanctioned_again():
    review = replace(_review(DANNIE, FIRST), copy=True)
    [hit], lines = review.resolve(_verdict(True, 0.94))
    assert hit.sanction is None
    assert hit.notice == "copy"
    assert lines == []


def test_a_no_or_an_unsure_yes_leaves_the_warning():
    review = _review(DANNIE, FIRST)
    hits, [line] = review.resolve(_verdict(False, 0.88))
    assert hits == []
    assert "Jev said not spam (88% sure)" in line
    hits, [line] = review.resolve(_verdict(True, 0.5, LOW_CONFIDENCE))
    assert hits == []
    assert "Jev said spam but was too unsure (50% sure)" in line


def test_scores_are_whole_percentages():
    assert percent(0.94) == "94%"
    assert percent(0.875) == "88%"
    assert percent(1) == "100%"
    verdict = _verdict(True, 0.7)[PROMOTIONAL_SPAM]
    assert "70%" in verdict.describe() and "0.7" not in verdict.describe()


def test_duration_text():
    assert rules.duration_text(3600) == "1 hour"
    assert rules.duration_text(86400) == "1 day"
    assert rules.duration_text(3 * 86400) == "3 days"
    assert rules.duration_text(300) == "5 minutes"


def test_jev_is_asked_the_promo_question_alone_with_the_authors_record():
    facts = say(DANNIE, PROMO, NOW)
    request = ClassifierRequest(
        guild_id=facts.guild_id,
        channel_id=facts.channel_id,
        author_id=facts.author_id,
        labels=frozenset({PROMOTIONAL_SPAM}),
        content=facts.content,
        promo_context=("joined 3 days ago", "no messages before this burst"),
    )
    output, material, instructions, fields = jev_request(request)
    assert fields == {PROMOTIONAL_SPAM: PROMOTIONAL_SPAM}
    assert set(output.model_fields) == {PROMOTIONAL_SPAM}
    assert material.startswith("MESSAGE:\n") and PROMO in material
    assert "- joined 3 days ago" in material
    assert "promot" in instructions.lower()


def test_the_action_dm_quotes_the_post_and_says_how_to_appeal():
    facts = say(DANNIE, "buy ```now``` " + "x" * 400, NOW)
    text = act_module.action_dm(
        "Smarter Dev", facts, "removed, and you are timed out for 1 hour", "spam"
    )
    assert text.startswith(
        f"Your message in <#{facts.channel_id}> on **Smarter Dev** was removed, "
        "and you are timed out for 1 hour."
    )
    assert "```" not in text
    assert "**Reason:** spam" in text
    assert text.endswith("message a moderator of the server to appeal.")
    assert "x" * 400 not in text


def test_a_hold_dm_explains_the_hold():
    from smarter_dev.bot.moderation_coordinator.model import Provisional

    facts = say(DANNIE, PROMO, NOW)
    text = act_module.hold_dm(
        "Smarter Dev",
        facts,
        Provisional(user_id=DANNIE.id, duration_seconds=300, reasons=("scam link",)),
    )
    assert "being checked" in text and "5 minutes" in text


# -- #478: a confident yes from a brand-new account is a ban ---------------------

#: Nov 20 2026, 13:08 UTC: an 18-hour-old account that joined an hour before
#: pasted this into two channels 38 s apart and got a 1 h timeout.
JEFF = Member("jeffmarcus", 478, NOW - timedelta(hours=18), NOW - timedelta(hours=1))
JEFF_POST = (
    "I'll help the first 10 people to earn $50k within 72 hours, pay me 10% of "
    "your profit once you receive it. DM me on TELEGRAM @Jeffmarcus33"
)


def _jeff_posts():
    return [
        say(JEFF, JEFF_POST, NOW),
        say(JEFF, JEFF_POST, NOW + timedelta(seconds=38), channel=OTHER),
    ]


async def test_the_jeff_burst_from_an_18_hour_old_account_is_a_ban():
    posts = _jeff_posts()
    h = harness(
        _case(
            JEFF,
            posts,
            FIRST,
            {PROMOTIONAL_SPAM: (True, 0.98), BAD_FAITH: (True, 0.95)},
        )
    )

    await h.judge(*posts)

    assert {p.message_id for p in posts} <= h.deleted()
    assert h.port.count("ban", JEFF.id) == 1
    assert h.port.count("timeout", JEFF.id) == 0
    row = h.recorder.rows[-1]
    assert row.decision["targets"][JEFF.id]["sanction"]["kind"] == gw.BAN
    report = h.port.latest_report_text(REPORTS)
    assert (
        "98% promotional spam; bad faith 95%; banned and every copy removed" in report
    )
    assert "account under a week old" in report
    assert "timed out" not in report
    # One Haiku request asked promo and intent together; Jev was not used.
    assert h.calls.jev == []
    asked = h.calls.haiku[-1]
    assert '"promotional_spam"' in asked and f'"{BAD_FAITH}"' in asked
    # The member's DM says why, without the classifier's intent wording.
    dm = " ".join(text for _, text in h.messenger.dms)
    assert "bad faith" not in dm and "95%" not in dm and "intent" not in dm


async def test_an_account_over_a_week_old_keeps_the_one_hour_timeout():
    older = replace(JEFF, created=NOW - timedelta(days=8))
    # Even a bad-faith verdict: the account is past the ban age.
    posts = [
        say(older, JEFF_POST, NOW),
        say(older, JEFF_POST, NOW + timedelta(seconds=38), channel=OTHER),
    ]
    h = harness(
        _case(
            older,
            posts,
            FIRST,
            {PROMOTIONAL_SPAM: (True, 0.98), BAD_FAITH: (True, 0.95)},
        )
    )

    await h.judge(*posts)

    assert {p.message_id for p in posts} <= h.deleted()
    assert h.port.count("ban", older.id) == 0
    assert h.port.count("timeout", older.id) == 1
    assert "timed out and every copy removed" in h.port.latest_report_text(REPORTS)


NEWCOMER_POST = (
    "Check out my open-source CLI tool for renaming files, feedback welcome!"
)


async def test_a_good_faith_newcomer_cross_post_is_a_timeout_not_a_ban():
    posts = [
        say(JEFF, NEWCOMER_POST, NOW),
        say(JEFF, NEWCOMER_POST, NOW + timedelta(seconds=20), channel=OTHER),
    ]
    h = harness(
        _case(
            JEFF, posts, FIRST, {PROMOTIONAL_SPAM: (True, 0.9), BAD_FAITH: (False, 0.9)}
        )
    )

    await h.judge(*posts)

    assert {p.message_id for p in posts} <= h.deleted()
    assert h.port.count("ban", JEFF.id) == 0
    assert h.port.count("timeout", JEFF.id) == 1
    report = h.port.latest_report_text(REPORTS)
    assert "good faith 90%; timed out and every copy removed" in report


async def test_an_unknown_intent_verdict_never_bans():
    posts = _jeff_posts()
    h = harness(
        _case(
            JEFF, posts, FIRST, {PROMOTIONAL_SPAM: (True, 0.98), BAD_FAITH: (True, 0.4)}
        )
    )

    await h.judge(*posts)

    assert h.port.count("ban", JEFF.id) == 0
    assert h.port.count("timeout", JEFF.id) == 1
    assert "intent unclear" in h.port.latest_report_text(REPORTS)


def _intent(answer: bool, confidence: float, outcome: str = ANSWERED) -> Verdicts:
    return _verdict(True, 0.98).merged(
        {
            BAD_FAITH: LabelVerdict(
                label=BAD_FAITH, answer=answer, confidence=confidence, outcome=outcome
            )
        }
    )


def test_the_ban_needs_bad_faith_and_follows_the_ladders_age():
    review = _review(JEFF, FIRST, ban_account_age=timedelta(days=30))
    assert review.asks_intent
    [hit], [line] = review.resolve(_intent(True, 0.95))
    assert hit.sanction.kind == gw.BAN
    assert "account under 30 days old" in hit.reason and "banned" in line
    # The intent rating is the report's; the member's DM never carries it.
    assert "bad faith 95%" in line and "bad faith" not in hit.reason
    # Good faith, unsure, or never asked: a timeout.
    for verdicts in (
        _intent(False, 0.9),
        _intent(True, 0.4, LOW_CONFIDENCE),
        _verdict(True, 0.98),
    ):
        [hit], _ = review.resolve(verdicts)
        assert hit.sanction.kind == gw.TIMEOUT
    # The policy's age is the line: 18 h is no longer under a 0-day setting,
    # and an account past it is not asked.
    off = _review(JEFF, FIRST, ban_account_age=timedelta(0))
    assert not off.asks_intent
    [hit], _ = off.resolve(_intent(True, 0.95))
    assert hit.sanction.kind == gw.TIMEOUT
    # A copy is removed only; not confident: only a warning.
    copy = replace(review, copy=True)
    [hit], lines = copy.resolve(_intent(True, 0.95))
    assert hit.sanction is None and lines == [] and len(hit.cleanup) == 2
    assert review.resolve(_verdict(True, 0.5, LOW_CONFIDENCE))[0] == []


FREELANCER_POST = "Freelance developer available for hire, DM me for my rates!"


async def test_a_good_faith_freelancer_advert_is_a_timeout_not_a_ban():
    posts = [
        say(JEFF, FREELANCER_POST, NOW),
        say(JEFF, FREELANCER_POST, NOW + timedelta(seconds=20), channel=OTHER),
    ]
    h = harness(
        _case(
            JEFF, posts, FIRST, {PROMOTIONAL_SPAM: (True, 0.9), BAD_FAITH: (False, 0.9)}
        )
    )

    await h.judge(*posts)

    assert h.port.count("ban", JEFF.id) == 0
    assert h.port.count("timeout", JEFF.id) == 1
    dm = " ".join(text for _, text in h.messenger.dms)
    assert "good faith" not in dm.replace("in good faith", "") and "intent" not in dm
    assert "good faith 90%" not in dm


async def test_a_policy_that_allows_no_ban_gets_a_timeout_and_says_so():
    posts = _jeff_posts()
    case = _case(
        JEFF, posts, FIRST, {PROMOTIONAL_SPAM: (True, 0.98), BAD_FAITH: (True, 0.95)}
    )
    no_ban = policy_from(document(sanctions__allowed=[mp.WARN, mp.TIMEOUT, mp.DELETE]))
    h = harness(replace(case, policy=no_ban))
    on_revision(h, {"revision": 1, "policy": no_ban})

    await h.judge(*posts)

    assert h.port.count("ban", JEFF.id) == 0
    assert h.port.count("timeout", JEFF.id) == 1
    report = h.port.latest_report_text(REPORTS)
    assert "timed out and every copy removed" in report and "banned" not in report
    assert all(f'"{BAD_FAITH}"' not in call for call in h.calls.haiku)


async def test_scam_no_on_the_first_message_then_promo_and_bad_faith_is_one_ban():
    posts = _jeff_posts()
    h = harness(
        _case(
            JEFF,
            posts,
            FIRST,
            {
                SCAM: (False, 0.9),
                ABUSE: (False, 0.9),
                PROMOTIONAL_SPAM: (True, 0.98),
                BAD_FAITH: (True, 0.95),
            },
        )
    )

    await h.judge(*posts)

    assert h.port.count("ban", JEFF.id) == 1
    assert h.port.count("timeout", JEFF.id) == 0
    assert {p.message_id for p in posts} <= h.deleted()
    assert h.calls.jev == []


async def test_scam_yes_on_the_first_message_bans_once_and_never_asks_promo():
    posts = _jeff_posts()
    h = harness(
        _case(
            JEFF,
            posts,
            FIRST,
            {
                SCAM: (True, 0.97),
                ABUSE: (False, 0.9),
                PROMOTIONAL_SPAM: (True, 0.98),
                BAD_FAITH: (True, 0.95),
            },
        )
    )

    await h.judge(*posts)

    assert h.port.count("ban", JEFF.id) == 1
    assert h.port.count("timeout", JEFF.id) == 0
    assert {p.message_id for p in posts} <= h.deleted()
    assert "promotional spam" not in h.port.latest_report_text(REPORTS)


async def test_haiku_is_given_the_promo_policy_when_it_rates_promo():
    posts = [
        say(JEFF, NEWCOMER_POST, NOW),
        say(JEFF, NEWCOMER_POST, NOW + timedelta(seconds=20), channel=OTHER),
    ]
    h = harness(
        _case(
            JEFF, posts, FIRST, {PROMOTIONAL_SPAM: (True, 0.9), BAD_FAITH: (False, 0.9)}
        )
    )

    await h.judge(*posts)

    asked = [c for c in h.calls.haiku if '"promotional_spam"' in c]
    assert asked and all(PROMO_POLICY in c for c in asked)
    assert "a project someone asks for feedback on" in asked[0]
    # Not asked about promo: no policy text.
    assert all(PROMO_POLICY not in c for c in h.calls.haiku if c not in asked)


def test_the_haiku_prompt_counts_the_summary_question_not_the_policy_line():
    from smarter_dev.bot.moderation_coordinator import classifier

    def request(summarize: bool) -> ClassifierRequest:
        return ClassifierRequest(
            guild_id="1",
            channel_id="2",
            author_id="3",
            labels=frozenset({PROMOTIONAL_SPAM, BAD_FAITH, SCAM, ABUSE}),
            content="x" * 500,
            ring_context=tuple(f"line {i:04d} " + "y" * 8 for i in range(3000)),
            promo_context=("a line",),
            summarize=summarize,
        )

    plain = classifier.haiku_prompt(request(False))
    summed = classifier.haiku_prompt(request(True))
    summary_line = '- "summary": ' + classifier._SUMMARY_TEXT
    # The summary question's bytes come out of the context's share, so asking
    # for one neither lengthens nor shortens the prompt (to a context line).
    assert abs(len(summed.encode()) - len(plain.encode())) <= 24  # one line
    assert summary_line in summed and classifier.PROMO_POLICY in summed
    assert summed.index(summary_line) < summed.index(classifier.PROMO_POLICY)
