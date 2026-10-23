"""What the coordinator must hold to once it acts (#240, from #255's review
and #301): bounded waits, no stalls behind a ring channel, bare invites."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from smarter_dev.bot.moderation_coordinator import coordinator as coordinator_module
from smarter_dev.bot.moderation_coordinator.classifier import ClassifierBudget
from smarter_dev.bot.moderation_coordinator.classifier import ModerationClassifier
from smarter_dev.bot.moderation_coordinator.coordinator import STAFF_CHECK_TIMED_OUT
from smarter_dev.bot.moderation_coordinator.coordinator import Coordinator
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import PastAction
from smarter_dev.bot.moderation_coordinator.replay import ModelCalls
from smarter_dev.bot.moderation_coordinator.replay import ReplayCase
from smarter_dev.bot.moderation_coordinator.replay import ReplayReads
from smarter_dev.bot.moderation_coordinator.replay import replay
from smarter_dev.bot.moderation_coordinator.replay import scripted_haiku
from smarter_dev.bot.moderation_coordinator.replay import scripted_jev
from smarter_dev.bot.moderation_coordinator.rules import scam_gate_content
from smarter_dev.web.moderation_gateway import types as gw
from tests.bot.moderation_coordinator.replay_cases import ESTABLISHED
from tests.bot.moderation_coordinator.replay_cases import GENERAL
from tests.bot.moderation_coordinator.replay_cases import NOW
from tests.bot.moderation_coordinator.replay_cases import OTHER
from tests.bot.moderation_coordinator.replay_cases import THIRD
from tests.bot.moderation_coordinator.replay_cases import Member
from tests.bot.moderation_coordinator.replay_cases import policy
from tests.bot.moderation_coordinator.replay_cases import say
from tests.bot.moderation_coordinator.replay_cases import scam_burst
from tests.bot.moderation_coordinator.replay_cases import staged_ring

NEWBIE = Member("newbie", 21, NOW - timedelta(days=3), NOW - timedelta(hours=2))
FIRST = AuthorHistory(is_first_message=True, days_since_last_message=None)


def _coordinator(reads, answers=None, calls=None) -> Coordinator:
    calls = calls if calls is not None else ModelCalls()
    return Coordinator(
        reads,
        ModerationClassifier(
            budget=ClassifierBudget(per_guild_day=1000),
            haiku_model=scripted_haiku(answers or {}, calls),
            jev_model=scripted_jev(answers or {}, calls),
        ),
    )


class _Hanging(ReplayReads):
    """Reads that never answer the staff check or the history."""

    hang_staff: bool = False
    hang_history: bool = False

    async def staff_refusal(self, guild_id, user_id, *, is_bot, extra_role_ids):
        if self.hang_staff:
            await asyncio.Event().wait()
        return await super().staff_refusal(
            guild_id, user_id, is_bot=is_bot, extra_role_ids=extra_role_ids
        )

    async def author_history(self, guild_id, user_id, before):
        if self.hang_history:
            await asyncio.Event().wait()
        return await super().author_history(guild_id, user_id, before)


# -- deadlines ---------------------------------------------------------------


async def test_an_unanswered_staff_check_refuses(monkeypatch):
    monkeypatch.setattr(coordinator_module, "STAFF_CHECK_SECONDS", 0.05)
    reads = _Hanging()
    reads.hang_staff = True
    spam = [
        say(NEWBIE, "@everyone free nitro https://dlscord.gift/x", NOW),
    ]
    evaluation = await _coordinator(reads).evaluate(spam[0], policy(ring=None))

    assert evaluation.decision.targets == {}
    assert evaluation.decision.staff_excluded == {NEWBIE.id: STAFF_CHECK_TIMED_OUT}


async def test_an_unread_history_is_judged_as_none_and_said(monkeypatch):
    monkeypatch.setattr(coordinator_module, "HISTORY_READ_SECONDS", 0.05)
    reads = _Hanging()
    reads.hang_history = True
    post = say(NEWBIE, "@everyone free nitro https://dlscord.gift/x", NOW)
    evaluation = await _coordinator(
        reads, {SCAM: (True, 0.95), ABUSE: (False, 0.9)}
    ).evaluate(post, policy(ring=None))

    # Still judged: a high-signal post needs no history to be reviewed.
    assert evaluation.scam_outcome == "violation"
    assert evaluation.decision.targets[NEWBIE.id].sanction.kind == gw.BAN
    assert any("could not be read" in line for line in evaluation.decision.review)


# -- the ring channel --------------------------------------------------------


async def test_a_busy_ring_channel_holds_no_slot_and_stalls_nobody(monkeypatch):
    """#255 item 3: with one slot, a message waiting on a ring channel's lock
    must not stop another channel's message from being decided."""
    case, m = staged_ring()
    coordinator = _coordinator(case.reads)
    slot = asyncio.Semaphore(1)
    # The ad's sweep holds the channel's lock.
    lock = coordinator.rings.channel(m["ad"].guild_id, m["ad"].channel_id)
    await lock.__aenter__()
    try:
        waiter = asyncio.create_task(
            coordinator.evaluate(m["cheer"], case.policy, slot=slot)
        )
        await asyncio.sleep(0.02)
        assert not waiter.done()
        elsewhere = say(NEWBIE, "hello", NOW, channel=OTHER)
        other = await asyncio.wait_for(
            coordinator.evaluate(elsewhere, case.policy, slot=slot), 1.0
        )
        assert other.decision.targets == {}
    finally:
        await lock.__aexit__(None, None, None)
    await asyncio.wait_for(waiter, 1.0)
    assert slot._value == 1  # every slot given back


async def test_the_ring_lock_wait_is_bounded(monkeypatch):
    monkeypatch.setattr(coordinator_module, "RING_LOCK_SECONDS", 0.05)
    case, m = staged_ring()
    coordinator = _coordinator(case.reads)
    lock = coordinator.rings.channel(m["ad"].guild_id, m["ad"].channel_id)
    await lock.__aenter__()
    try:
        evaluation = await asyncio.wait_for(
            coordinator.evaluate(m["ad"], case.policy), 1.0
        )
    finally:
        await lock.__aexit__(None, None, None)
    assert any(
        "Ring check" in line and "skipped" in line
        for line in evaluation.decision.review
    )
    assert not coordinator.rings.busy(m["ad"].guild_id, m["ad"].channel_id)


# -- bare invites (#301) -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "join us discord.gg/abcd1234",
        "discord.com/invite/abcd1234",
        "free stuff at steamgift.ru",
        "claim at nitro-drop.ru/claim",
    ],
)
def test_links_without_a_scheme_open_the_scam_gate(text):
    member = Member("x", 30, NOW - timedelta(days=900), NOW - timedelta(days=800))
    assert scam_gate_content(say(member, text, NOW))


@pytest.mark.parametrize(
    "text", ["mail me at a@b.com", "use node.js please", "version 1.2.3", "e.g. this"]
)
def test_ordinary_text_with_dots_is_not_a_link(text):
    member = Member("x", 31, NOW - timedelta(days=900), NOW - timedelta(days=800))
    assert not scam_gate_content(say(member, text, NOW))


async def test_ferryloys_eight_bare_invites_are_judged_once_and_all_removed():
    """#301: ferryloy posted a bare ``discord.gg/…`` invite eight times across
    channels in 14 s on their first day; scam-banner saw plain text. Here the
    first post is a new member's link (reviewed), the rest are its copies,
    burst across channels: one request, every post removed, and the account
    is new enough for a ban."""
    ferryloy = Member(
        "ferryloy", 41, NOW - timedelta(days=3), NOW - timedelta(seconds=30)
    )
    invite = "come hang out discord.gg/fr33n1tro"
    channels = [GENERAL, OTHER, THIRD]
    posts = [
        say(ferryloy, invite, NOW + timedelta(seconds=2 * i), channel=channels[i % 3])
        for i in range(8)
    ]
    case = ReplayCase(
        name="ferryloy",
        policy=policy(ring=None),
        messages=posts,
        answers={SCAM: (True, 0.92), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={ferryloy.id: FIRST}),
    )
    result = await replay(case)

    assert result.calls.total == 1
    cleaned = {
        item.message_id
        for evaluation in result.evaluations
        for target in evaluation.decision.targets.values()
        for item in target.cleanup
    }
    assert cleaned == {post.message_id for post in posts}
    first = result.evaluations[0]
    assert first.scam_outcome == "violation"
    assert first.decision.targets[ferryloy.id].sanction.kind == gw.BAN


async def test_an_established_members_copy_of_a_clean_post_costs_nothing():
    """Only a post that was judged has copies to reuse a verdict for: a
    regular who pastes the same harmless link twice is never sent to the
    model and nothing is removed (the spam engine's repeat warning aside)."""
    regular = Member("reg", 42, NOW - timedelta(days=900), NOW - timedelta(days=800))
    link = "notes: docs.python.org/3/library/asyncio.html"
    posts = [say(regular, link, NOW), say(regular, link, NOW + timedelta(seconds=5))]
    case = ReplayCase(
        name="clean copies",
        policy=policy(ring=None),
        messages=posts,
        answers={SCAM: (False, 0.95), ABUSE: (False, 0.95)},
        reads=ReplayReads(histories={regular.id: ESTABLISHED}),
    )
    result = await replay(case)
    assert result.calls.total == 0  # not risky, not suspect: never reviewed
    assert all(e.scam_outcome is None for e in result.evaluations)
    assert all(
        not target.cleanup
        for e in result.evaluations
        for target in e.decision.targets.values()
    )


async def test_the_provisional_hook_runs_before_the_classifier():
    events = []
    calls = ModelCalls()
    haiku = scripted_haiku({SCAM: (True, 0.95), ABUSE: (False, 0.9)}, calls)
    answer = haiku.function

    def recording(messages, info):
        events.append("classifier")
        return answer(messages, info)

    haiku.function = recording
    coordinator = Coordinator(
        ReplayReads(histories={NEWBIE.id: FIRST}),
        ModerationClassifier(
            budget=ClassifierBudget(per_guild_day=100), haiku_model=haiku
        ),
    )

    async def hold(facts, provisional, key):
        events.append(("hold", provisional.duration_seconds, key))

    post = say(NEWBIE, "@everyone free nitro https://dlscord.gift/x", NOW)
    evaluation = await coordinator.evaluate(
        post, replace(policy(ring=None)), on_provisional=hold
    )
    assert events[0] == ("hold", 300, evaluation.key)
    assert events[1] == "classifier"


# -- the 4+ image hold is for high-risk authors only (Zech, Oct 23) ------------


def _screenshots(member: Member):
    from smarter_dev.bot.moderation_coordinator.model import Attachment

    images = tuple(
        Attachment(
            f"shot{i}.png", "image/png", 20_000 + i, f"https://cdn.example/{i}.png"
        )
        for i in range(5)
    )
    return say(member, "here's my setup", NOW, attachments=images)


async def _image_post(history: AuthorHistory, *, joined_hours_ago: float = 24 * 400):
    member = Member(
        "shooter",
        50,
        NOW - timedelta(days=900),
        NOW - timedelta(hours=joined_hours_ago),
    )
    post = _screenshots(member)
    case = ReplayCase(
        name="five screenshots",
        policy=policy(ring=None),
        messages=[post],
        answers={SCAM: (False, 0.9), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={member.id: history}),
    )
    result = await replay(case)
    return result.evaluations[0], result


async def test_an_active_established_members_screenshots_are_reviewed_not_held():
    evaluation, result = await _image_post(ESTABLISHED)
    assert result.calls.total == 1  # still the image-aware review
    assert evaluation.scam_outcome == "clean"
    assert evaluation.decision.provisional is None
    assert evaluation.decision.targets == {}  # released silently: nothing at all


async def test_the_same_screenshots_from_a_member_silent_31_days_are_held():
    evaluation, _ = await _image_post(
        AuthorHistory(is_first_message=False, days_since_last_message=31)
    )
    assert evaluation.decision.provisional is not None
    assert evaluation.decision.provisional.resolution == "released"


async def test_the_same_screenshots_from_a_new_joiner_are_held():
    evaluation, _ = await _image_post(ESTABLISHED, joined_hours_ago=2)
    assert evaluation.decision.provisional is not None


async def test_the_same_screenshots_from_a_member_warned_10_days_ago_are_held():
    from datetime import UTC
    from datetime import datetime

    from smarter_dev.bot.moderation_coordinator.model import PastAction

    warned = AuthorHistory(
        is_first_message=False,
        days_since_last_message=0,
        actions=(
            PastAction(
                "warn",
                datetime.fromtimestamp(NOW.timestamp(), UTC) - timedelta(days=10),
            ),
        ),
    )
    evaluation, _ = await _image_post(warned)
    assert evaluation.decision.provisional is not None


async def test_a_burst_still_holds_an_established_member():
    """Zech kept the burst hold for everyone."""
    member = Member("burst", 51, NOW - timedelta(days=900), NOW - timedelta(days=800))
    lure = "check this out https://example.org/x"
    posts = [
        say(member, lure, NOW),
        say(member, lure, NOW + timedelta(seconds=2), channel=OTHER),
    ]
    case = ReplayCase(
        name="burst",
        policy=policy(ring=None),
        messages=posts,
        answers={SCAM: (False, 0.9), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={member.id: ESTABLISHED}),
    )
    result = await replay(case)
    assert result.evaluations[1].decision.provisional is not None


# -- one random image for an established member's post (Zech, Oct 23 22:00) ----


def _five_images(member: Member):
    from smarter_dev.bot.moderation_coordinator.model import Attachment

    return say(
        member,
        "my desk setup",
        NOW,
        attachments=tuple(
            Attachment(
                f"shot{i}.png",
                "image/png",
                20_000,
                f"https://cdn.discordapp.com/attachments/1/2/shot{i}.png",
            )
            for i in range(5)
        ),
    )


def _image_haiku(calls: ModelCalls):
    """Haiku that answers scam=yes only when a shown image carries SCAM."""
    import json

    from pydantic_ai.messages import BinaryContent
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.messages import TextPart
    from pydantic_ai.models.function import FunctionModel

    def respond(messages, info):
        parts = list(messages[-1].parts[-1].content)
        images = [p for p in parts if isinstance(p, BinaryContent)]
        calls.haiku.append(parts[0])
        calls.haiku_images.append(images)
        scam = any(image.data.endswith(b"SCAM") for image in images)
        body = {
            "participants": {},
            "scam": {"answer": scam, "confidence": 0.95},
            "abuse": {"answer": False, "confidence": 0.95},
        }
        return ModelResponse(parts=[TextPart(json.dumps(body))])

    return FunctionModel(respond, model_name="claude-haiku-5-5")


async def _sampled(
    history: AuthorHistory, *, pick: int = 3, scam_in: str | None = None
):
    from smarter_dev.bot.moderation_coordinator.replay import REPLAY_PNG

    member = Member("desk", 70, NOW - timedelta(days=900), NOW - timedelta(days=800))
    post = _five_images(member)

    async def fetch(url, limit):
        if scam_in and url.endswith(scam_in):
            return REPLAY_PNG + b"SCAM"
        return REPLAY_PNG

    case = ReplayCase(
        name="sampled",
        policy=policy(ring=None),
        messages=[post],
        reads=ReplayReads(histories={member.id: history}),
        haiku=lambda answers, calls, by_text: _image_haiku(calls),
        fetch_image=fetch,
        choose_images=lambda items, count: [items[pick]][:count],
    )
    result = await replay(case)
    return result, member, post


async def test_an_established_members_five_images_are_checked_on_the_chosen_one():
    result, _, _ = await _sampled(ESTABLISHED, pick=3)
    [shown] = result.calls.haiku_images
    assert len(shown) == 1
    prompt = result.calls.haiku[0]
    assert "shot3.png [shown as image 1]" in prompt
    assert prompt.count("[not shown: not sampled]") == 4
    record = result.evaluations[0].classifier.as_record()["images"]
    assert record["sampled"] is True and record["unseen"] == 0
    # A "no" on a sample stays a no: clean, nothing done.
    assert result.evaluations[0].scam_outcome == "clean"


async def test_a_high_risk_members_five_images_show_the_first_four():
    result, _, _ = await _sampled(FIRST, pick=3)
    [shown] = result.calls.haiku_images
    assert len(shown) == 4
    assert result.evaluations[0].classifier.as_record()["images"]["sampled"] is False


async def test_a_scam_in_the_chosen_image_is_caught_and_cleaned_up():
    result, member, post = await _sampled(ESTABLISHED, pick=3, scam_in="shot3.png")
    evaluation = result.evaluations[0]
    assert evaluation.scam_outcome == "violation"
    target = evaluation.decision.targets[member.id]
    assert target.sanction.kind == gw.WARN
    assert [item.message_id for item in target.cleanup] == [post.message_id]
    # The control: the same scam in an image not chosen is not seen.
    missed, _, _ = await _sampled(ESTABLISHED, pick=0, scam_in="shot3.png")
    assert missed.evaluations[0].scam_outcome == "clean"


# -- copies after the review of #240 (#310 items 2 and 3) ---------------------


async def test_a_burst_copy_of_a_sampled_image_post_is_held_and_asked_on_every_image():
    """#310 item 2: an established member's 4+ image post is checked on one
    sampled image, unheld. The same post in another channel 2 s later is a
    burst the first check never saw: it is held and asked again on the first
    four images, which finds the scam the sample missed; both posts go."""
    from smarter_dev.bot.moderation_coordinator.replay import REPLAY_PNG

    member = Member("desk", 71, NOW - timedelta(days=900), NOW - timedelta(days=800))
    first = _five_images(member)
    burst = replace(
        first,
        message_id=str(int(first.message_id) + 1),
        channel_id=OTHER,
        posted_at=first.posted_at + 2,
    )

    async def fetch(url, limit):
        return REPLAY_PNG + b"SCAM" if url.endswith("shot0.png") else REPLAY_PNG

    case = ReplayCase(
        name="burst copy of a sampled post",
        policy=policy(ring=None),
        messages=[first, burst],
        reads=ReplayReads(histories={member.id: ESTABLISHED}),
        haiku=lambda answers, calls, by_text: _image_haiku(calls),
        fetch_image=fetch,
        choose_images=lambda items, count: [items[3]][:count],
    )
    result = await replay(case)

    sampled, full = result.calls.haiku_images
    assert len(sampled) == 1 and len(full) == 4
    original, copy = result.evaluations
    assert original.scam_outcome == "clean"
    assert original.decision.provisional is None
    assert copy.decision.provisional is not None  # held while asked again
    assert copy.scam_outcome == "violation"
    target = copy.decision.targets[member.id]
    assert target.sanction.kind == gw.TIMEOUT  # a held high-signal scam
    assert {item.message_id for item in target.cleanup} == {
        first.message_id,
        burst.message_id,
    }


async def test_a_copy_with_no_new_reason_reuses_the_verdict_without_asking():
    """The control for item 2: a third copy in yet another channel brings the
    same burst reason the second was judged under, so it asks nothing."""
    case, victim, posts = scam_burst()
    case = replace(case, answers={SCAM: (False, 0.95), ABUSE: (False, 0.95)})
    result = await replay(case)
    assert result.calls.total == 1
    assert result.evaluations[2].decision.provisional is None


async def test_a_copy_of_a_confirmed_scam_is_removed_but_not_punished_again():
    """#310 item 3: the copy reused the violation verdict and went through
    the sanction again: a second warn, or with a recent warn on record, a
    second 24 h timeout. A copy is removed only (by the scam gate)."""
    warned = AuthorHistory(
        is_first_message=False,
        days_since_last_message=0,
        actions=(
            PastAction(
                "warn",
                datetime.fromtimestamp(NOW.timestamp(), UTC) - timedelta(days=10),
            ),
        ),
    )
    member = Member("again", 72, NOW - timedelta(days=300), NOW - timedelta(days=200))
    lure = "grab the free crypto airdrop https://airdrop-claim.example/now"
    posts = [say(member, lure, NOW), say(member, lure, NOW + timedelta(seconds=19))]
    case = ReplayCase(
        name="copy of a confirmed scam",
        policy=policy(ring=None),
        messages=posts,
        answers={SCAM: (True, 0.9), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={member.id: warned}),
    )
    result = await replay(case)

    assert result.calls.total == 1
    original, copy = (e.decision.targets[member.id] for e in result.evaluations)
    assert original.sanction.kind == gw.TIMEOUT  # the repeat
    # No second repeat timeout and no scam notice; what is left is the spam
    # engine's own duplicate-message warning, which legacy gives too.
    assert copy.sanction.kind == gw.WARN
    assert copy.notices == ["spam_warning"]
    assert posts[1].message_id in {item.message_id for item in copy.cleanup}


# -- a removed @everyone makes the author high risk for a day (#310 item 10b) --


def _pinged(hours_ago: float) -> AuthorHistory:
    return AuthorHistory(
        is_first_message=False,
        days_since_last_message=0,
        pinged_everyone_at=datetime.fromtimestamp(NOW.timestamp(), UTC)
        - timedelta(hours=hours_ago),
    )


async def test_four_images_an_hour_after_a_removed_ping_are_held_on_every_image():
    result, _, _ = await _sampled(_pinged(1), pick=3)
    [shown] = result.calls.haiku_images
    assert len(shown) == 4
    evaluation = result.evaluations[0]
    assert evaluation.decision.provisional is not None
    assert evaluation.classifier.as_record()["images"]["sampled"] is False


async def test_the_same_post_25_hours_later_is_sampled_and_not_held():
    result, _, _ = await _sampled(_pinged(25), pick=3)
    [shown] = result.calls.haiku_images
    assert len(shown) == 1
    evaluation = result.evaluations[0]
    assert evaluation.decision.provisional is None
    assert evaluation.classifier.as_record()["images"]["sampled"] is True
