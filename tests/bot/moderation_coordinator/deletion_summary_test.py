"""The report says what was deleted (#378).

Every deletion in a reported incident gets a one-line description of the
message and its attachments, written by Haiku from what the bot saw before
the delete: from the classifier's own request when there was one, otherwise
from one separate request off the hot path. Copies of one post share one
description. The cases run live-shaped messages through the shadow and the
executor over the real gateway (``act_test.harness``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic_ai.messages import BinaryContent
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.moderation_coordinator import classifier as clf
from smarter_dev.bot.moderation_coordinator import summary as summary_module
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import Attachment
from smarter_dev.bot.moderation_coordinator.replay import ReplayCase
from smarter_dev.bot.moderation_coordinator.replay import ReplayReads
from smarter_dev.bot.moderation_coordinator.replay import scripted_haiku
from smarter_dev.bot.moderation_coordinator.summary import DeletionSummaries
from smarter_dev.bot.moderation_coordinator.summary import DeletionSummarizer
from smarter_dev.bot.moderation_coordinator.summary import SummaryBudget
from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.moderation_gateway import report as report_mod
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.gateway import default_session_factory
from tests.bot.moderation_coordinator.act_test import FIRST
from tests.bot.moderation_coordinator.act_test import PINGER
from tests.bot.moderation_coordinator.act_test import REPORTS
from tests.bot.moderation_coordinator.act_test import _incident_ids
from tests.bot.moderation_coordinator.act_test import _until
from tests.bot.moderation_coordinator.act_test import acting
from tests.bot.moderation_coordinator.act_test import harness
from tests.bot.moderation_coordinator.burst_test import INTRODUCTIONS
from tests.bot.moderation_coordinator.burst_test import RETURNING
from tests.bot.moderation_coordinator.burst_test import SCAMMER
from tests.bot.moderation_coordinator.burst_test import _burst
from tests.bot.moderation_coordinator.burst_test import _run_burst
from tests.bot.moderation_coordinator.replay_cases import GENERAL
from tests.bot.moderation_coordinator.replay_cases import NOW
from tests.bot.moderation_coordinator.replay_cases import THIRD
from tests.bot.moderation_coordinator.replay_cases import policy
from tests.bot.moderation_coordinator.replay_cases import say

CLASSIFIER_LINE = "Crypto prize lure: text links to prize-drop[.]example"
SEPARATE_LINE = "Server-wide ping asking members to check a message; no attachments"
SEPARATE_PREFIX = "Describe this deleted message"


@dataclass
class Haiku:
    """One stand-in for the classifier's Haiku, which the deletion summaries
    share: classifier requests answer as scripted (with a ``summary`` when
    asked for one); separate summary requests are counted on their own."""

    classifier_line: str | None = CLASSIFIER_LINE
    separate_line: str = SEPARATE_LINE
    #: The separate request answers only once this is set.
    release: asyncio.Event | None = None
    #: The classifier's request answers only once this is set.
    classifier_release: asyncio.Event | None = None
    #: The separate request raises this instead of answering.
    fail: BaseException | None = None
    #: The separate request takes this long.
    delay: float = 0.0
    separate: list[str] = field(default_factory=list)
    separate_images: list[int] = field(default_factory=list)
    classifier_prompts: list[str] = field(default_factory=list)

    def build(self, answers, calls, by_text):
        scripted = scripted_haiku(answers, calls, by_text)

        async def respond(messages, info):
            content = messages[-1].parts[-1].content
            parts = [content] if isinstance(content, str) else list(content)
            prompt = parts[0]
            if prompt.startswith(SEPARATE_PREFIX):
                self.separate.append(prompt)
                self.separate_images.append(
                    len([p for p in parts if isinstance(p, BinaryContent)])
                )
                if self.release is not None:
                    await self.release.wait()
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.fail is not None:
                    raise self.fail
                return ModelResponse(parts=[TextPart(self.separate_line)])
            self.classifier_prompts.append(prompt)
            reply = scripted.function(messages, info)  # counts the request
            if self.classifier_release is not None:
                await self.classifier_release.wait()
            body = json.loads(reply.parts[0].content)
            if '"summary":' in prompt and self.classifier_line is not None:
                body["summary"] = self.classifier_line
            return ModelResponse(parts=[TextPart(json.dumps(body))])

        return FunctionModel(respond, model_name="claude-haiku-5-5")


def summarized(case: ReplayCase, haiku: Haiku, **summarizer):
    """``act_test.harness`` with the deletion summaries on, as the bot wires
    them: the classifier's own Haiku, the coordinator's book."""
    h = harness(replace(case, haiku=haiku.build))
    coordinator = h.shadow.coordinator
    h.summarizer = DeletionSummarizer(
        model=coordinator.classifier.haiku_model,
        budget=summarizer.pop("budget", None),
        **summarizer,
    )
    h.executor.summaries = DeletionSummaries(coordinator.summaries, h.summarizer)
    return h


def ping_case(*messages, answers=None, **over):
    """The poster's first message: the scam gate asks about a link in it."""
    return ReplayCase(
        name="#378",
        policy=acting(policy(ring=None, **over)),
        messages=list(messages),
        answers=answers or {SCAM: (False, 0.9), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={PINGER.id: FIRST}),
    )


async def summaries() -> list[ModerationIncidentEntry]:
    from sqlalchemy import select

    async with default_session_factory()() as session:
        return list(
            (
                await session.execute(
                    select(ModerationIncidentEntry)
                    .where(ModerationIncidentEntry.kind == gw.ENTRY_DELETED_SUMMARY)
                    .order_by(ModerationIncidentEntry.created_at)
                )
            ).scalars()
        )


def delete_line(report: str) -> str:
    return next(line for line in report.splitlines() if "Deleted " in line)


# -- the classifier's own request ------------------------------------------


SCAM_LINK = "claim your crypto https://prize-drop.example/x"


@pytest.mark.parametrize(
    "text",
    [f"@everyone {SCAM_LINK}", f"free {SCAM_LINK}"],
    ids=["removed-before-the-check", "removed-on-the-verdict"],
)
async def test_a_classifier_judged_deletion_carries_the_summary_from_the_same_call(
    text,
):
    """The scam check's request asks for the description too; the line is
    written from its answer and no second request is made. With a mass
    mention the message is removed before the check is even asked: its line
    waits for the check's answer instead of asking for itself."""
    haiku = Haiku()
    post = say(PINGER, text, NOW)
    h = summarized(
        ping_case(post, answers={SCAM: (True, 0.95), ABUSE: (False, 0.9)}), haiku
    )
    await h.judge(post)
    await h.shadow.drain()

    assert post.message_id in h.deleted()
    assert len(haiku.classifier_prompts) == 1
    assert '"summary":' in haiku.classifier_prompts[0]
    assert haiku.separate == [] and h.summarizer.requests == 0
    [entry] = await summaries()
    assert entry.content == CLASSIFIER_LINE
    assert entry.details["status"] == "ok"
    assert entry.details["message_ids"] == [post.message_id]
    assert entry.target_user_id == PINGER.id
    report = h.port.latest_report_text(REPORTS)
    assert CLASSIFIER_LINE in delete_line(report)
    assert f"**Deleted:** {CLASSIFIER_LINE}" in report


# -- a rule-only deletion ---------------------------------------------------


async def test_a_rule_only_deletion_gets_one_background_call_that_nothing_waits_for():
    """A mass mention with nothing for the scam gate: no classifier request.
    The delete, the warning and its DM are done while the description's
    request is still out; the report gains the line when it answers."""
    release = asyncio.Event()
    haiku = Haiku(release=release)
    post = say(PINGER, "@everyone check this", NOW)
    h = summarized(ping_case(post), haiku)
    classifier_budget = dict(h.shadow.coordinator.classifier.budget._days)

    # The decision is carried out in full with the summary still held.
    await asyncio.wait_for(h.judge(post), 10)
    assert post.message_id in h.deleted()
    assert len(h.messenger.dms) == 1  # the warning's DM
    assert await _until(lambda: len(haiku.separate) == 1)
    assert await summaries() == []
    assert "**Deleted:**" not in h.port.latest_report_text(REPORTS)

    release.set()
    await h.shadow.drain()
    assert haiku.classifier_prompts == []
    assert h.summarizer.requests == 1 and len(haiku.separate) == 1
    assert "@everyone check this" in haiku.separate[0]
    [entry] = await summaries()
    assert entry.content == SEPARATE_LINE
    report = h.port.latest_report_text(REPORTS)
    assert SEPARATE_LINE in delete_line(report)
    assert f"**Deleted:** {SEPARATE_LINE}" in report
    # Its own budget: the classifier's was not charged.
    assert h.shadow.coordinator.classifier.budget._days == classifier_budget


async def test_the_separate_call_sees_only_images_loaded_before_the_delete():
    """A message deleted with its images loaded for the scam check: a check
    refused by its budget leaves no description, and the separate request
    shows Haiku those same bytes. Nothing is fetched after the delete."""
    haiku = Haiku()
    shots = tuple(
        Attachment(
            f"shot{i}.png",
            "image/png",
            1000,
            f"https://cdn.discordapp.com/attachments/1/2/shot{i}.png",
        )
        for i in range(6)
    )
    post = say(PINGER, "@everyone look", NOW, attachments=shots)
    fetched: list[str] = []

    async def fetch(url, limit):
        fetched.append(url)
        return b"\x89PNG\r\n\x1a\n" + bytes(16)

    case = replace(ping_case(post), fetch_image=fetch)
    h = summarized(case, haiku)
    h.shadow.coordinator.classifier.budget.per_guild_day = 0  # check refused
    h.shadow.coordinator.classifier.budget.acting_per_guild_day = 0
    await h.judge(post)
    await h.shadow.drain()

    assert post.message_id in h.deleted()
    assert haiku.classifier_prompts == []
    assert len(fetched) == clf.MAX_IMAGES  # loaded once, before the delete
    assert haiku.separate_images == [clf.MAX_IMAGES]
    [entry] = await summaries()
    assert entry.content == SEPARATE_LINE


# -- copies -----------------------------------------------------------------


async def test_a_burst_of_identical_copies_gets_one_summary():
    """#376's burst: posts A and B, each copied to a second channel. Two
    posts, two descriptions (from A's and B's own checks); the copies'
    deletions reuse them, and the report says "×2 in" both channels."""
    release = asyncio.Event()
    haiku = Haiku(classifier_release=release)
    posts = _burst()
    case = ReplayCase(
        name="#378 burst",
        policy=acting(policy(ring=None, spam=None)),
        messages=list(posts.values()),
        answers={SCAM: (True, 0.97), ABUSE: (False, 0.97)},
        reads=ReplayReads(histories={SCAMMER.id: RETURNING}),
    )
    h = summarized(case, haiku)

    await _run_burst(h, posts, release)

    assert len(await _incident_ids()) == 1
    assert len(haiku.classifier_prompts) == 2 and haiku.separate == []
    assert {p.message_id for p in posts.values()} <= h.deleted()
    report = h.port.latest_report_text(REPORTS)
    print(report)
    groups = report_mod.deleted_posts(await _report_facts())
    assert len(groups) == 2
    for group, (post, copy) in zip(
        sorted(groups, key=lambda g: g.message_ids[0]),
        sorted([("A", "D"), ("B", "C")], key=lambda pair: posts[pair[0]].message_id),
        strict=True,
    ):
        assert group.text == CLASSIFIER_LINE
        assert sorted(group.message_ids) == sorted(
            [posts[post].message_id, posts[copy].message_id]
        )
        assert sorted(group.channel_ids) == sorted([INTRODUCTIONS, GENERAL])
    times = f"(×2 in <#{INTRODUCTIONS}>, <#{GENERAL}>)"
    assert report.count(times) == 4  # each post: its delete line and the summary
    deleted_line = next(line for line in report.splitlines() if "**Deleted:**" in line)
    assert deleted_line.count(CLASSIFIER_LINE) == 2


async def test_rule_only_copies_share_one_separate_call():
    """The same @everyone post in three channels: three removals, no
    classifier request, one separate request for all three."""
    haiku = Haiku(delay=0.05)
    posts = [
        say(PINGER, "@everyone check this", NOW + timedelta(seconds=i), channel=channel)
        for i, channel in enumerate((GENERAL, INTRODUCTIONS, THIRD))
    ]
    h = summarized(ping_case(*posts, spam=None), haiku)
    await h.judge(*posts)
    await h.shadow.drain()

    assert {p.message_id for p in posts} <= h.deleted()
    assert h.summarizer.requests == 1 and len(haiku.separate) == 1
    assert len(await summaries()) == 3
    report = h.port.latest_report_text(REPORTS)
    [group] = report_mod.deleted_posts(await _report_facts())
    assert group.text == SEPARATE_LINE and len(group.message_ids) == 3
    assert f"{SEPARATE_LINE} (×3 in <#{GENERAL}>" in report


# -- failure ----------------------------------------------------------------


@pytest.mark.parametrize("failure", ["error", "timeout", "budget"])
async def test_a_summary_that_fails_or_times_out_still_renders_the_action(failure):
    haiku = Haiku(
        fail=RuntimeError("provider down") if failure == "error" else None,
        delay=5.0 if failure == "timeout" else 0.0,
    )
    post = say(PINGER, "@everyone check this", NOW)
    h = summarized(
        ping_case(post),
        haiku,
        deadline=0.2,
        budget=SummaryBudget(per_guild_day=0 if failure == "budget" else 100),
    )
    await h.judge(post)
    await h.shadow.drain()

    assert post.message_id in h.deleted()
    [entry] = await summaries()
    assert entry.content == summary_module.UNAVAILABLE
    assert entry.details["status"] == "unavailable"
    report = h.port.latest_report_text(REPORTS)
    line = delete_line(report)
    assert line.startswith("- <t:") and f"<@{PINGER.id}>" in line
    assert line.endswith("— summary unavailable")
    assert "**Deleted:** summary unavailable" in report
    assert "Warned" in report or "warned" in report
    assert len(haiku.separate) == (0 if failure == "budget" else 1)


# -- safety -----------------------------------------------------------------


RAW = (
    "Fake Steam gift-card giveaway: links to steamcommunnity.ru/gift and "
    "https://evil.example/claim, invite discord.gg/abc123, pings @everyone"
)


@pytest.mark.parametrize("path", ["classifier", "separate"])
async def test_links_in_a_summary_are_defanged(path):
    haiku = Haiku(classifier_line=RAW, separate_line=RAW)
    text = f"free {SCAM_LINK}" if path == "classifier" else "@everyone check this"
    answers = {SCAM: (path == "classifier", 0.95), ABUSE: (False, 0.9)}
    post = say(PINGER, text, NOW)
    h = summarized(ping_case(post, answers=answers), haiku)
    await h.judge(post)
    await h.shadow.drain()

    [entry] = await summaries()
    report = h.port.latest_report_text(REPORTS)
    for shown in (entry.content, report):
        assert "steamcommunnity[.]ru/gift" in shown
        assert "evil[.]example" in shown and "https://evil" not in shown
        assert "discord[.]gg/abc123" in shown and "discord.gg" not in shown
        assert "steamcommunnity.ru" not in shown
        assert "@everyone" not in shown


def test_the_summary_prompts_say_what_a_summary_must_not_repeat():
    """The brief's safety rules, pinned: both prompts tell Haiku to defang
    links as [.] and to name slurs and explicit content, not quote them."""
    from smarter_dev.bot.moderation_coordinator.classifier import ClassifierRequest
    from smarter_dev.bot.moderation_coordinator.classifier import haiku_prompt

    request = ClassifierRequest(
        guild_id="G",
        channel_id="C",
        author_id="A",
        labels=frozenset({SCAM, ABUSE}),
        content="x",
        summarize=True,
    )
    for prompt in (
        haiku_prompt(request),
        summary_module.SUMMARY_INSTRUCTIONS,
    ):
        assert "write each of its dots as [.]" in prompt
        assert "Never repeat a slur, insult or explicit sexual content" in prompt
        assert "a slur aimed at a member, a sexual image" in prompt
    assert '"summary"' not in haiku_prompt(replace(request, summarize=False))


async def test_kept_images_stay_under_the_byte_limit_oldest_dropped_first():
    """A Haiku outage keeps every checked post's images for a separate
    request: the book holds at most its byte limit, drops the oldest posts'
    images first, keeps their descriptions' claims, and never keeps a post
    whose images alone exceed the limit."""
    from smarter_dev.bot.moderation_coordinator.summary import SummaryBook

    assert summary_module.MAX_IMAGE_BYTES == 32 * 1024 * 1024
    book = SummaryBook(max_image_bytes=1000)
    claims = {}
    for n in range(12):
        claims[n], first = book.claim("G", "A", f"post-{n}")
        assert first
        book.keep_images("G", "A", f"post-{n}", {0: b"x" * 150, 1: None})
        assert book.image_bytes <= 1000
    held = [n for n in range(12) if book.images("G", "A", f"post-{n}")]
    assert held == [6, 7, 8, 9, 10, 11]
    assert book.image_bytes == 900
    # The dropped posts' claims still stand: their copies wait on them.
    future, first = book.claim("G", "A", "post-0")
    assert future is claims[0] and not first

    book.keep_images("G", "A", "huge", {0: b"x" * 1001})
    assert book.images("G", "A", "huge") == {}
    assert book.image_bytes == 900

    # A written description frees its post's images.
    book.settle("G", "A", "post-11", claims[11], "a line")
    assert book.images("G", "A", "post-11") == {}
    assert book.image_bytes == 750


# -- the report -------------------------------------------------------------


async def _report_facts() -> report_mod.ReportFacts:
    from sqlalchemy import select

    from smarter_dev.web.models import ModerationIncident
    from smarter_dev.web.moderation_gateway import ModerationGateway

    async with default_session_factory()() as session:
        [incident] = (await session.execute(select(ModerationIncident))).scalars()
        facts, _ = await ModerationGateway(None)._report_facts(session, incident)
    return facts


def _entry(key, text, ids, channels, status="ok"):
    return report_mod.ReportEntry(
        kind=gw.ENTRY_DELETED_SUMMARY,
        source="coordinator",
        rule=None,
        content=text,
        target_user_id="9",
        channel_id=None,
        message_id=None,
        at=1000.0,
        details={
            "key": key,
            "message_ids": ids,
            "channel_ids": channels,
            "status": status,
        },
    )


def test_the_report_groups_copies_and_says_when_none_was_written():
    delete = report_mod.ReportOperation(
        kind=gw.PURGE,
        state=gw.CONFIRMED,
        actor="coordinator",
        target_user_id="9",
        target_username="spammer",
        detail=None,
        channel_id="1",
        message_ids=("m1", "m2"),
        deleted=("m1", "m2"),
        duration_seconds=None,
        expires_at=None,
        at=999.0,
    )
    other = replace(
        delete, kind=gw.DELETE, channel_id="2", message_ids=("m3",), deleted=("m3",)
    )
    lone = replace(delete, kind=gw.DELETE, message_ids=("m4",), deleted=("m4",))
    facts = report_mod.ReportFacts(
        incident_id=uuid4(),
        guild_id="G",
        subject_user_id="9",
        channel_id="1",
        trigger_message_id="m1",
        opened_at=990.0,
        ping_role_id=None,
        operations=(delete, other, lone),
        entries=(
            # The first copy's line failed; a later copy's carries the text.
            _entry("k1", "summary unavailable", ["m1", "m2"], ["1"], "unavailable"),
            _entry("k1", "Gift-card lure", ["m3"], ["2"]),
            _entry("k2", "summary unavailable", ["m4"], ["1"], "unavailable"),
        ),
    )
    text = report_mod.render_report(facts)
    lines = text.splitlines()
    first = next(line for line in lines if "Deleted 2 messages" in line)
    assert first.endswith("— Gift-card lure (×3 in <#1>, <#2>)")
    second = next(line for line in lines if "/m3" in line)
    assert "—" not in second  # said once, on the post's first delete
    lone_line = next(line for line in lines if "/m4" in line)
    assert lone_line.endswith("— summary unavailable")
    assert (
        "**Deleted:** Gift-card lure (×3 in <#1>, <#2>); summary unavailable" in lines
    )
