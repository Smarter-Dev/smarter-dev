"""The replay harness over recorded incidents and established-member negatives.

Each case runs through a fresh coordinator with scripted models that count
their requests (``smarter_dev.bot.moderation_coordinator.replay``). What is
checked is the one decision per message and the cost of reaching it: at most
one classifier request per message revision, and none at all when no rule
needs a label.
"""

from __future__ import annotations

import asyncio

from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import TIMEOUT
from smarter_dev.bot.moderation_coordinator.model import UNSEEN_IMAGES
from smarter_dev.bot.moderation_coordinator.replay import replay
from smarter_dev.web.moderation_gateway import types as gw
from tests.bot.moderation_coordinator.replay_cases import ALICE
from tests.bot.moderation_coordinator.replay_cases import BOB
from tests.bot.moderation_coordinator.replay_cases import CAROL
from tests.bot.moderation_coordinator.replay_cases import DAVE
from tests.bot.moderation_coordinator.replay_cases import NED
from tests.bot.moderation_coordinator.replay_cases import SAM
from tests.bot.moderation_coordinator.replay_cases import established_negatives
from tests.bot.moderation_coordinator.replay_cases import images_2030
from tests.bot.moderation_coordinator.replay_cases import scam_1749
from tests.bot.moderation_coordinator.replay_cases import scam_burst
from tests.bot.moderation_coordinator.replay_cases import staged_ring

DAY = 86400


# ---------------------------------------------------------------------------
# 17:49
# ---------------------------------------------------------------------------


async def test_1749_two_day_old_account_is_one_ban_after_one_request():
    case, scammer = scam_1749(account_age_days=2)
    result = await replay(case)

    evaluation = result.evaluations[0]
    decision = evaluation.decision
    target = decision.targets[scammer.id]
    assert target.sanction.kind == gw.BAN
    assert [c.message_id for c in target.cleanup] == [evaluation.facts.message_id]
    # Every reason that fired is kept on the one decision.
    assert {"spam_engine", "scam_gate"} <= set(target.rules)
    assert list(decision.targets) == [scammer.id]
    # Held while judged, then upgraded to the ban.
    assert decision.provisional.resolution == "upgraded"
    assert "scam link" in decision.provisional.reasons
    # The scam labels and the ring's ad question went in one Haiku request.
    assert result.calls.total == 1
    assert len(result.calls.haiku) == 1
    assert evaluation.classifier.backend == "haiku"
    assert set(evaluation.labels_wanted) == {"scam", "abuse", "event_promotion"}


async def test_1749_older_compromised_account_is_a_timeout_plus_cleanup():
    case, scammer = scam_1749(account_age_days=30)
    result = await replay(case)

    decision = result.evaluations[0].decision
    target = decision.targets[scammer.id]
    assert target.sanction.kind == gw.TIMEOUT
    assert target.sanction.duration_seconds == DAY
    assert target.cleanup
    assert "secure_account" in target.notices
    assert decision.provisional.resolution == "upgraded"
    # Not a new member, so no ring question: only the scam labels.
    assert set(result.evaluations[0].labels_wanted) == {"scam", "abuse"}
    assert result.calls.total == 1


async def test_1749_with_no_verdict_keeps_only_what_the_rules_justify():
    """Haiku times out: the spam engine's own mute and delete still stand and
    replace the hold; nothing only the model would justify (the ban) is added."""

    def slow(answers, calls, by_text=None):
        from pydantic_ai.models.function import FunctionModel

        async def respond(messages, info):
            calls.haiku.append("timed out")
            await asyncio.sleep(5)

        return FunctionModel(respond)

    case, scammer = scam_1749(account_age_days=2)
    case.haiku = slow
    case.deadline = 0.05
    result = await replay(case)

    evaluation = result.evaluations[0]
    target = evaluation.decision.targets[scammer.id]
    assert target.sanction.kind == gw.TIMEOUT  # the spam engine's 24 h mute
    assert target.sanction.duration_seconds == DAY
    assert target.model_justified is False
    assert evaluation.scam_outcome == "unknown"
    assert evaluation.decision.provisional.resolution == "upgraded"
    assert all(v.outcome == TIMEOUT for v in evaluation.verdicts.values())
    assert any("came back unknown" in line for line in evaluation.decision.review)
    assert result.calls.total == 1  # and never a second try


async def test_a_burst_is_judged_once_and_every_copy_cleaned_up():
    case, victim, posts = scam_burst()
    result = await replay(case)

    first, second, third = (result.by_message(p.message_id) for p in posts)
    # A lone post with an ordinary link from a regular is not reviewed.
    assert first.decision.strongest is None
    assert first.labels_wanted == []
    # The copy in a second channel makes it a burst: held, judged once.
    assert second.decision.provisional is not None
    assert second.decision.targets[victim.id].sanction.kind == gw.TIMEOUT
    assert {c.message_id for c in second.decision.targets[victim.id].cleanup} == {
        posts[0].message_id,
        posts[1].message_id,
    }
    # The third copy reuses that verdict instead of asking again.
    assert third.reused_labels == ["abuse", "scam"]
    assert third.classifier.requests == 0
    assert {c.message_id for c in third.decision.targets[victim.id].cleanup} == {
        p.message_id for p in posts
    }
    assert result.calls.total == 1


# ---------------------------------------------------------------------------
# The staged ring
# ---------------------------------------------------------------------------


async def test_the_staged_ring_is_found_with_jev_alone():
    case, m = staged_ring()
    result = await replay(case)

    ad = result.by_message(m["ad"].message_id)
    acted = {
        user_id: target.sanction
        for user_id, target in ad.decision.targets.items()
        if target.sanction is not None
    }
    assert set(acted) == {CAROL.id, ALICE.id, BOB.id}
    assert all(
        s.kind == gw.TIMEOUT and s.duration_seconds == 7 * DAY for s in acted.values()
    )
    cleaned = {c.message_id for t in ad.decision.targets.values() for c in t.cleanup}
    assert cleaned == {m["q1"].message_id, m["q2"].message_id, m["ad"].message_id}
    # The newcomer with a real question and the established member are spared.
    assert NED.id not in ad.decision.targets
    assert m["chat"].author_id not in ad.decision.targets
    # The ad and its participant questions were one Jev request.
    assert ad.classifier.backend == "jev"
    assert ad.classifier.requests == 1
    assert result.calls.haiku == []


async def test_ring_followups_known_participant_free_newcomer_one_request():
    case, m = staged_ring()
    result = await replay(case)

    cheer = result.by_message(m["cheer"].message_id)
    assert cheer.decision.targets[ALICE.id].sanction.kind == gw.TIMEOUT
    assert cheer.classifier.requests == 0  # already a participant: no model

    late = result.by_message(m["late"].message_id)
    assert late.decision.targets[DAVE.id].sanction.kind == gw.TIMEOUT
    assert late.labels_wanted == [f"participant:{DAVE.id}"]
    assert late.classifier.requests == 1

    staff = result.by_message(m["staff"].message_id)
    assert staff.decision.strongest is None
    assert SAM.id not in staff.decision.targets


async def test_every_ring_message_costs_at_most_one_request():
    case, _ = staged_ring()
    result = await replay(case)

    assert all(e.classifier.requests <= 1 for e in result.evaluations)
    # q1, q2, the ad and the late follow-up asked; nothing else did.
    assert sum(e.classifier.requests for e in result.evaluations) == 4
    assert len(result.calls.jev) == 4


# ---------------------------------------------------------------------------
# Established members
# ---------------------------------------------------------------------------


async def test_established_members_are_never_actioned():
    case, messages = established_negatives()
    result = await replay(case)

    assert [e.decision.strongest for e in result.evaluations] == [None] * len(messages)
    # Only the image dump needed a check (scam-banner's 4+ images); it came
    # back clean. An established member is not held while it is asked
    # (Zech, Oct 23: the image hold is for high-risk authors only).
    asked = [e for e in result.evaluations if e.classifier.requests]
    assert [e.facts.message_id for e in asked] == [messages[2].message_id]
    assert asked[0].decision.provisional is None
    # The moderator's @here is the spam engine's, refused on staff grounds.
    mod = result.by_message(messages[3].message_id)
    assert mod.decision.staff_excluded
    assert mod.classifier.requests == 0


# ---------------------------------------------------------------------------
# 20:30 Oct 23: four posts of four images
# ---------------------------------------------------------------------------


async def test_2030_every_image_is_shown_and_a_clean_answer_ends_it():
    case, _, posts = images_2030()
    result = await replay(case)

    first = result.by_message(posts[0].message_id)
    assert first.classifier.backend == "haiku"
    assert first.scam_outcome == "clean"
    # An account of two years: reviewed, not held (Zech, Oct 23).
    assert first.decision.provisional is None
    # Not high risk and not held: each post is checked on one image chosen
    # at random (Zech, Oct 23); the other three are named, not unseen.
    assert [len(images) for images in result.calls.haiku_images] == [1] * len(
        result.calls.haiku
    )
    record = first.classifier.as_record()["images"]
    assert (record["sent"], record["not_sampled"], record["sampled"]) == (1, 3, True)
    assert record["unseen"] == 0


async def test_2030_one_image_not_loaded_is_unknown_and_said():
    from smarter_dev.bot.moderation_coordinator.replay import replay_image

    async def one_fails(url, limit):
        if url.endswith("/p0_0.png"):  # the one the replay samples
            raise ConnectionError("cdn refused")
        return await replay_image(url, limit)

    case, _, posts = images_2030(fetch_image=one_fails)
    result = await replay(case)

    first = result.by_message(posts[0].message_id)
    assert first.verdicts[SCAM].outcome == UNSEEN_IMAGES
    assert first.scam_outcome == "unknown"
    assert first.decision.provisional is None  # not a high-risk author
    assert first.decision.strongest is None
    assert any("came back unknown" in line for line in first.decision.review)


async def test_a_ring_ad_with_an_image_is_judged_by_haiku_with_the_image():
    """Jev cannot see images: the ring's questions on an ad with one go to
    Haiku, image included, and reach the same decision."""
    from dataclasses import replace

    from smarter_dev.bot.moderation_coordinator.model import Attachment

    case, m = staged_ring()
    flyer = Attachment(
        "flyer.png", "image/png", 90_000, "https://cdn.discordapp.com/a/1/flyer.png"
    )
    ad = replace(m["ad"], attachments=(flyer,))
    case.messages = [ad if f is m["ad"] else f for f in case.messages]
    result = await replay(case)

    judged = result.by_message(ad.message_id)
    assert judged.scam_outcome is None  # no scam check: the ring's questions only
    assert judged.classifier.backend == "haiku"
    assert [len(images) for images in result.calls.haiku_images] == [1]
    acted = {
        user_id
        for user_id, target in judged.decision.targets.items()
        if target.sanction is not None
    }
    assert acted == {CAROL.id, ALICE.id, BOB.id}
