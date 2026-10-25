"""One member's scam burst is one incident, one sanction, no blind copies (#376).

Incident 6ebbd00d / d37d0554 (Oct 25 2026, 14:07Z): a member posted two
different scam posts (A and B, two images each) in #introductions, then each
again in #general, four messages in 1.9 s. A and B were checked; their
copies arrived while those checks were out, each asked for itself, met the
author's budget (2 per 10 minutes) and read "expired without a verdict, a
moderator should look". The two posts opened two incidents and warned and
DMed the member twice.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from smarter_dev.bot.moderation_coordinator import act as act_module
from smarter_dev.bot.moderation_coordinator.model import Attachment
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import PastAction
from smarter_dev.bot.moderation_coordinator.model import Sanction
from smarter_dev.bot.moderation_coordinator.replay import ReplayCase
from smarter_dev.bot.moderation_coordinator.replay import ReplayReads
from smarter_dev.bot.moderation_coordinator.state import ScamBursts
from smarter_dev.bot.moderation_coordinator.state import ScamSanctions
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.gateway import default_session_factory
from tests.bot.moderation_coordinator.act_test import REPORTS
from tests.bot.moderation_coordinator.act_test import _incident_ids
from tests.bot.moderation_coordinator.act_test import _until
from tests.bot.moderation_coordinator.act_test import acting
from tests.bot.moderation_coordinator.act_test import entries
from tests.bot.moderation_coordinator.act_test import harness
from tests.bot.moderation_coordinator.replay_cases import GENERAL
from tests.bot.moderation_coordinator.replay_cases import NOW
from tests.bot.moderation_coordinator.replay_cases import OTHER
from tests.bot.moderation_coordinator.replay_cases import Member
from tests.bot.moderation_coordinator.replay_cases import policy
from tests.bot.moderation_coordinator.replay_cases import say

INTRODUCTIONS = OTHER
SCAMMER = Member("d1sc0n3ct", 376, NOW - timedelta(days=400), NOW - timedelta(days=300))
BYSTANDER = Member("second", 377, NOW - timedelta(days=400), NOW - timedelta(days=300))
NEW_ACCOUNT = Member("fresh", 378, NOW - timedelta(days=3), NOW - timedelta(hours=2))
#: Checked but not held, as the member was: back after 60 days' silence.
RETURNING = AuthorHistory(is_first_message=False, days_since_last_message=60)
#: The four real timings, seconds after the first post (14:07:23.668).
AT = {"A": 0.0, "B": 0.389, "C": 1.677, "D": 1.880}


def _files(post: str) -> tuple[Attachment, ...]:
    return tuple(
        Attachment(
            f"{post}_{i}.png",
            "image/png",
            300_000 + i,
            f"https://cdn.discordapp.com/attachments/1/2/{post}_{i}.png",
        )
        for i in range(2)
    )


def _burst(member: Member = SCAMMER, offset: float = 0.0):
    """A, B in #introductions; C (B again) and D (A again) in #general."""
    text = {"A": "free nitro, claim it here", "B": "airdrop live, claim now"}
    same = {"A": "A", "B": "B", "C": "B", "D": "A"}
    channel = {"A": INTRODUCTIONS, "B": INTRODUCTIONS, "C": GENERAL, "D": GENERAL}
    return {
        name: say(
            member,
            text[same[name]],
            NOW + timedelta(seconds=offset + AT[name]),
            channel=channel[name],
            attachments=_files(f"{member.salt}{same[name]}"),
        )
        for name in "ABCD"
    }


def _scam_haiku(release: asyncio.Event):
    """Haiku calling every post a scam (0.97), once ``release`` is set: the
    checks of A and B are still out when C and D arrive."""

    def build(answers, calls, by_text):
        async def respond(messages, info):
            content = messages[-1].parts[-1].content
            calls.haiku.append(content if isinstance(content, str) else content[0])
            await release.wait()
            body = {
                "participants": {},
                "scam": {"answer": True, "confidence": 0.97},
                "abuse": {"answer": False, "confidence": 0.97},
            }
            return ModelResponse(parts=[TextPart(json.dumps(body))])

        return FunctionModel(respond, model_name="claude-haiku-5-5")

    return build


def _case(posts, release, *, histories=None):
    return ReplayCase(
        name="#376 burst",
        policy=acting(policy(ring=None, spam=None)),
        messages=list(posts),
        haiku=_scam_haiku(release),
        reads=ReplayReads(histories=histories or {SCAMMER.id: RETURNING}),
    )


async def _run_burst(h, posts, release, *, per_author: int = 2):
    """Submit A and B, wait until both checks are out, then C and D, and
    release the answers once C and D wait on them (held, as in prod)."""
    h.shadow.coordinator.classifier.budget.per_author = per_author
    for name in "AB":
        h.clock[0] = posts[name].posted_at
        h.shadow.submit(posts[name])
    assert await _until(lambda: len(h.calls.haiku) == 2)
    for name in "CD":
        h.clock[0] = posts[name].posted_at
        h.shadow.submit(posts[name])

    def copies_held() -> bool:
        pending = set().union(*(hold.pending for hold in h.executor._holds.values()))
        return {posts["C"].message_id, posts["D"].message_id} <= pending

    assert await _until(copies_held)
    await asyncio.sleep(0.05)  # C and D are past their holds, waiting
    release.set()
    await h.shadow.drain()


async def _sanctions(user_id: str) -> list[tuple[str, int | None]]:
    async with default_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    ModerationAction.action_type, ModerationAction.duration_seconds
                ).where(
                    ModerationAction.target_user_id == user_id,
                    ModerationAction.action_type.in_(("warn", "timeout", "ban")),
                )
            )
        ).all()
    return sorted((row[0], row[1]) for row in rows)


async def test_the_1407_burst_is_one_incident_one_sanction_and_no_blind_copies():
    release = asyncio.Event()
    posts = _burst()
    h = harness(_case(posts.values(), release))

    await _run_burst(h, posts, release)

    rows = {row.message_id: row for row in h.recorder.rows}
    by_name = {name: rows[posts[name].message_id] for name in "ABCD"}
    # Only A and B were asked; the copies reused their answers, nothing was
    # refused by the author's budget.
    assert h.calls.total == 2
    assert all(row.classifier["refused_by"] is None for row in rows.values())
    for name in "CD":
        assert by_name[name].classifier["requests"] == 0
        assert by_name[name].classifier["reused_labels"] == ["abuse", "scam"]
        assert by_name[name].classifier["scam_outcome"] == "violation"
        assert by_name[name].provisional["resolution"] != "expired"
    assert {p.message_id for p in posts.values()} <= h.deleted()

    # One incident, one report, one sanction, one DM.
    assert len(await _incident_ids()) == 1
    assert len([p for p in h.port.posts if p["channel_id"] == REPORTS]) == 1
    assert len(h.messenger.dms) == 1
    sanctions = await _sanctions(SCAMMER.id)
    # The burst is a high-signal scam from an established account: the
    # policy's one-hour high-signal timeout (Zech, Oct 25).
    assert [s for s in sanctions if s[0] != gw.TIMEOUT or s[1] > 300] == [
        (gw.TIMEOUT, 3600)
    ]
    report = h.port.latest_report_text(REPORTS)
    print(report)
    assert "No verdict" not in report
    assert "moderator should look" not in report
    assert "came back unknown" not in report
    assert "has been warned" not in "".join(text for _, text in h.messenger.posts)


WARNED_LATELY = replace(
    RETURNING, actions=(PastAction("warn", NOW - timedelta(days=3), "spam"),)
)


@pytest.mark.parametrize(
    ("member", "history", "sanction", "secure_account_dm"),
    [
        # Zech, Oct 25: most hacked accounts spam in one burst and never
        # again, so an established account's burst is an hour.
        (SCAMMER, RETURNING, (gw.TIMEOUT, 3600), True),
        # A warning in the last 30 days still makes it a repeat: a day.
        (SCAMMER, WARNED_LATELY, (gw.TIMEOUT, 86400), True),
        # An account under a week old is still banned, with no DM.
        (NEW_ACCOUNT, RETURNING, (gw.BAN, None), False),
    ],
    ids=["established", "warned-lately", "new-account"],
)
async def test_a_confirmed_burst_is_an_hour_a_repeat_a_day_a_new_account_a_ban(
    member, history, sanction, secure_account_dm
):
    release = asyncio.Event()
    posts = _burst(member)
    h = harness(_case(posts.values(), release, histories={member.id: history}))

    await _run_burst(h, posts, release)

    sanctions = await _sanctions(member.id)
    assert [s for s in sanctions if s[0] != gw.TIMEOUT or s[1] > 300] == [sanction]
    assert len(await _incident_ids()) == 1
    dms = [text for user, text in h.messenger.dms if user == member.id]
    if secure_account_dm:
        assert len(dms) == 1 and act_module.SECURE_ACCOUNT_STEPS in dms[0]
    else:
        assert dms == []


def _old_escalation(h):
    """The copies ask again, as before #376 (any new high-signal reason)."""
    from smarter_dev.bot.moderation_coordinator import state

    cache = h.shadow.coordinator.verdict_cache

    def escalates(guild_id, author_id, key, reasons, now=None):
        entry = cache._entries.get((guild_id, author_id, key))
        return entry is not None and state._adds_to(entry, reasons)

    cache.escalates = escalates


async def test_without_the_fix_the_copies_spend_the_budget_and_split_the_burst():
    """Control: the copies asking for themselves again, with no fallback to
    their post's answer and no burst grouping, reproduce the incident: two
    budget refusals, two "expired" alerts, two incidents, two DMs."""
    release = asyncio.Event()
    posts = _burst()
    h = harness(_case(posts.values(), release))
    _old_escalation(h)
    h.shadow.coordinator.verdict_cache.asked = lambda *args: None
    joined = iter(range(100))
    h.shadow.coordinator.bursts.join = lambda *args: f"own-{next(joined)}"

    await _run_burst(h, posts, release)

    rows = {row.message_id: row for row in h.recorder.rows}
    refused = [m for m, row in rows.items() if row.classifier["refused_by"]]
    assert sorted(refused) == sorted([posts["C"].message_id, posts["D"].message_id])
    assert len(await _incident_ids()) == 2
    assert len(h.messenger.dms) == 2
    alerts = await entries(gw.ENTRY_ALERT)
    assert len([a for a in alerts if "No verdict from the scam check" in a]) == 2


async def test_an_escalated_copy_refused_by_the_budget_falls_back_to_its_posts_answer():
    """Control for the fallback alone: copies that do ask again (and meet
    the budget) are judged on their post's answer, not left "expired"."""
    release = asyncio.Event()
    posts = _burst()
    h = harness(_case(posts.values(), release))
    _old_escalation(h)

    await _run_burst(h, posts, release)

    rows = {row.message_id: row for row in h.recorder.rows}
    for name in "CD":
        row = rows[posts[name].message_id]
        assert row.classifier["refused_by"] == "author"
        assert row.classifier["scam_outcome"] == "violation"
        assert row.classifier["reused_labels"] == ["abuse", "scam"]
    assert not [a for a in await entries(gw.ENTRY_ALERT) if "No verdict" in a]
    assert len(await _incident_ids()) == 1
    assert len(h.messenger.dms) == 1


async def test_two_members_in_the_same_burst_are_two_incidents_and_two_sanctions():
    release = asyncio.Event()
    mine = _burst()
    theirs = _burst(BYSTANDER, offset=0.1)
    h = harness(
        _case(
            [*mine.values(), *theirs.values()],
            release,
            histories={SCAMMER.id: RETURNING, BYSTANDER.id: RETURNING},
        )
    )
    h.shadow.coordinator.classifier.budget.per_author = 2
    for posts in (mine, theirs):
        for name in "AB":
            h.shadow.submit(posts[name])
    assert await _until(lambda: len(h.calls.haiku) == 4)
    for posts in (mine, theirs):
        for name in "CD":
            h.shadow.submit(posts[name])
    await asyncio.sleep(0.2)
    release.set()
    await h.shadow.drain()

    assert h.calls.total == 4
    assert len(await _incident_ids()) == 2
    for member in (SCAMMER, BYSTANDER):
        sanctions = await _sanctions(member.id)
        assert [s for s in sanctions if s[1] != 300] == [(gw.TIMEOUT, 3600)]
    assert sorted(user for user, _ in h.messenger.dms) == sorted(
        [SCAMMER.id, BYSTANDER.id]
    )


async def test_a_stronger_ladder_sanction_in_the_burst_still_applies():
    """A post of the burst decided alone is a warn. The burst's next post,
    judged once the member's record carries a recent warning (a repeat), is a
    24 h timeout: stronger, so it applies on top, in the same incident."""
    release = asyncio.Event()
    release.set()
    posts = _burst()
    case = _case([posts["A"], posts["B"]], release)
    h = harness(case)

    await h.judge(posts["A"])
    case.reads.histories[SCAMMER.id] = replace(
        RETURNING,
        actions=(PastAction("warn", NOW - timedelta(days=3), "spam"),),
    )
    await h.judge(posts["B"])

    assert await _sanctions(SCAMMER.id) == [(gw.TIMEOUT, 86400), (gw.WARN, None)]
    assert len(await _incident_ids()) == 1


async def test_an_equal_sanction_for_a_second_post_of_the_burst_is_not_given():
    """Two different posts of one burst, each decided alone as a warn: one
    warning. Control for the burst key: a post past the burst's gap is a
    new burst and is warned again."""
    release = asyncio.Event()
    release.set()
    posts = _burst()
    later = say(
        SCAMMER,
        "another giveaway, claim",
        NOW + timedelta(seconds=400),
        channel=GENERAL,
        attachments=_files("late"),
    )
    h = harness(_case([posts["A"], posts["B"], later], release))

    await h.judge(posts["A"], posts["B"])
    assert await _sanctions(SCAMMER.id) == [(gw.WARN, None)]
    assert len(await _incident_ids()) == 1

    await h.judge(later)
    assert await _sanctions(SCAMMER.id) == [(gw.WARN, None), (gw.WARN, None)]
    assert len(await _incident_ids()) == 2


def test_the_burst_key_keeps_the_strongest_and_ends_with_its_burst():
    sanctions = ScamSanctions()
    bursts = ScamBursts()
    t = time.time()
    anchor = bursts.join("g", "u", "m1", t)
    assert bursts.join("g", "u", "m2", t + 1) == anchor
    assert bursts.join("g", "other", "m3", t + 1) == "m3"
    warn, day = Sanction(gw.WARN), Sanction(gw.TIMEOUT, duration_seconds=86400)
    assert sanctions.claim("g", "u", "post-a", warn, t, burst=anchor)
    assert not sanctions.claim("g", "u", "post-b", warn, t + 1, burst=anchor)
    assert sanctions.claim("g", "u", "post-b", day, t + 2, burst=anchor)
    later = bursts.join("g", "u", "m4", t + 500)
    assert later == "m4"
    assert sanctions.claim("g", "u", "post-c", warn, t + 500, burst=later)


def test_a_member_posting_every_minute_starts_a_new_burst_every_two_minutes():
    # Each post is within 120 s of the one before, so a gap measured from the
    # last post would keep all six in the first burst (and its incident and
    # sanction) for good. Measured from the burst's first post, they split.
    bursts = ScamBursts()
    t = time.time()
    anchors = [
        bursts.join("g", "u", f"m{minute}", t + 60 * minute) for minute in range(6)
    ]
    assert anchors == ["m0", "m0", "m0", "m3", "m3", "m3"]


async def test_a_blind_post_of_a_confirmed_burst_asks_no_moderator():
    """A third, different post of the burst (four images: held) meets the
    spent budget after the burst was confirmed and actioned: the report says
    its hold stays and lapses, and asks nobody to look. Control: the same
    blind post with nothing confirmed before it still alerts."""
    release = asyncio.Event()
    release.set()
    posts = _burst()
    third = say(
        SCAMMER,
        "look at these",
        NOW + timedelta(seconds=3),
        channel=GENERAL,
        attachments=tuple(_files("x1") + _files("x2")),
    )
    h = harness(_case([posts["A"], posts["B"], third], release))
    h.shadow.coordinator.classifier.budget.per_author = 2

    await h.judge(posts["A"], posts["B"], third)

    row = next(r for r in h.recorder.rows if r.message_id == third.message_id)
    assert row.classifier["refused_by"] == "author"
    assert row.provisional["resolution"] == "expired"
    report = h.port.latest_report_text(REPORTS)
    assert "the rest of their burst was confirmed as a scam and actioned" in report
    assert "moderator should look" not in report
    assert "Scam check incomplete" not in report
    assert len(await _incident_ids()) == 1


async def test_a_blind_post_with_nothing_confirmed_still_alerts():
    release = asyncio.Event()
    release.set()
    third = say(
        SCAMMER,
        "look at these",
        NOW,
        channel=GENERAL,
        attachments=tuple(_files("x1") + _files("x2")),
    )
    h = harness(_case([third], release))
    h.shadow.coordinator.classifier.budget.per_author = 0

    await h.judge(third)

    report = h.port.latest_report_text(REPORTS)
    assert "A moderator should look" in report
    assert "Scam check incomplete" in report


def test_the_facts_carry_the_display_name_the_member_had():
    from types import SimpleNamespace

    from smarter_dev.bot.moderation_coordinator.shadow import facts_from_message

    def facts(nickname, global_name):
        author = SimpleNamespace(
            id=1328112859804336178,
            username="d1sc0n3ct",
            global_name=global_name,
            is_bot=False,
        )
        message = SimpleNamespace(
            id=1, author=author, content="hi", attachments=(), webhook_id=None
        )
        member = SimpleNamespace(nickname=nickname, joined_at=None)
        return facts_from_message(
            guild_id=1,
            channel_id=2,
            message=message,
            member=member,
            is_edit=False,
            now=0.0,
        )

    assert facts("Nick", "Global").author_display_name == "Nick"
    assert facts(None, "Global").author_display_name == "Global"
    assert facts(None, None).author_display_name is None
    assert facts(None, None).author_name == "d1sc0n3ct"
