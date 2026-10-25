"""The coordinator's pipeline: who is asked about, and what it costs."""

from __future__ import annotations

from datetime import timedelta

from smarter_dev.bot.moderation_coordinator.classifier import PER_AUTHOR
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import BUDGET
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.replay import ReplayCase
from smarter_dev.bot.moderation_coordinator.replay import ReplayReads
from smarter_dev.bot.moderation_coordinator.replay import replay
from smarter_dev.web.moderation_gateway import types as gw
from tests.bot.moderation_coordinator.replay_cases import NOW
from tests.bot.moderation_coordinator.replay_cases import SCAM_TEXT
from tests.bot.moderation_coordinator.replay_cases import Member
from tests.bot.moderation_coordinator.replay_cases import policy
from tests.bot.moderation_coordinator.replay_cases import say

NEWBIE = Member("newbie", 8, NOW - timedelta(days=3), NOW - timedelta(hours=2))
FIRST = AuthorHistory(is_first_message=True, days_since_last_message=None)


async def test_a_protected_author_is_never_sent_to_the_model():
    case = ReplayCase(
        name="staff scam-looking post",
        policy=policy(),
        messages=[say(NEWBIE, SCAM_TEXT, NOW)],
        answers={SCAM: (True, 0.99)},
        reads=ReplayReads(staff={NEWBIE.id: "holds a staff permission"}),
    )
    result = await replay(case)

    evaluation = result.evaluations[0]
    assert result.calls.total == 0
    assert evaluation.decision.targets == {}
    assert evaluation.decision.staff_excluded == {NEWBIE.id: "holds a staff permission"}
    assert evaluation.decision.provisional is None


async def test_past_the_author_budget_labels_are_unknown_and_rules_still_apply():
    """A new member posting a sixth link in ten minutes (the cap is 5,
    #376): no sixth request. The scam check is unknown (budget); nothing
    model-only is decided."""
    posts = [
        say(NEWBIE, f"check https://site{i}.example", NOW + timedelta(seconds=30 * i))
        for i in range(PER_AUTHOR + 1)
    ]
    case = ReplayCase(
        name="budget",
        policy=policy(ring=None),
        messages=posts,
        answers={SCAM: (True, 0.95), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={NEWBIE.id: FIRST}),
    )
    result = await replay(case)

    assert [e.classifier.requests for e in result.evaluations] == [1] * PER_AUTHOR + [0]
    third = result.evaluations[-1]
    assert third.classifier.outcome == BUDGET
    assert third.classifier.refused_by == "author"
    assert third.verdicts[SCAM].outcome == BUDGET
    assert third.decision.strongest is None
    assert result.calls.total == PER_AUTHOR == 5


async def test_an_off_policy_rule_never_runs():
    """A guild with the scam gate and ring off: a scam-looking first post
    from a new member costs nothing; the spam engine alone decides."""
    case = ReplayCase(
        name="rules off",
        policy=policy(scam_gate=False, ring=None),
        messages=[say(NEWBIE, SCAM_TEXT, NOW)],
        answers={SCAM: (True, 0.99)},
        reads=ReplayReads(histories={NEWBIE.id: FIRST}),
    )
    result = await replay(case)

    evaluation = result.evaluations[0]
    assert result.calls.total == 0
    assert evaluation.decision.targets[NEWBIE.id].sanction.kind == gw.TIMEOUT
    assert evaluation.decision.provisional is None


async def test_attachments_are_only_sent_with_the_scam_question():
    from smarter_dev.bot.moderation_coordinator.model import Attachment

    images = tuple(
        Attachment(f"{i}.png", "image/png", 10, f"https://cdn.example/{i}.png")
        for i in range(4)
    )
    case = ReplayCase(
        name="images",
        policy=policy(ring=None),
        messages=[say(NEWBIE, "", NOW, attachments=images)],
        answers={SCAM: (False, 0.9), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={NEWBIE.id: FIRST}),
    )
    result = await replay(case)
    assert "https://cdn.example/3.png" in result.calls.haiku[0]


async def test_a_ring_message_arriving_mid_sweep_waits_and_is_a_followup():
    """As the spam ring's channel lock does: the cheer that lands while the ad
    is being judged is judged after it, as a follow-up, not as a new ad."""
    import asyncio

    from smarter_dev.bot.moderation_coordinator.classifier import ClassifierBudget
    from smarter_dev.bot.moderation_coordinator.classifier import ModerationClassifier
    from smarter_dev.bot.moderation_coordinator.coordinator import Coordinator
    from smarter_dev.bot.moderation_coordinator.replay import ModelCalls
    from smarter_dev.bot.moderation_coordinator.replay import scripted_jev
    from tests.bot.moderation_coordinator.replay_cases import ALICE
    from tests.bot.moderation_coordinator.replay_cases import staged_ring

    case, m = staged_ring()
    calls = ModelCalls()
    jev = scripted_jev(case.answers, calls, case.answers_by_text)
    answer = jev.function

    async def slow(messages, info):
        await asyncio.sleep(0.05)
        return answer(messages, info)

    jev.function = slow
    coordinator = Coordinator(
        case.reads,
        ModerationClassifier(budget=ClassifierBudget(per_guild_day=100), jev_model=jev),
    )
    case.reads.now = m["cheer"].posted_at
    cheer = say(ALICE, "so hyped", NOW + timedelta(seconds=1))
    ad, follow = await asyncio.gather(
        coordinator.evaluate(m["ad"], case.policy),
        coordinator.evaluate(cheer, case.policy),
    )

    assert ad.decision.targets[ALICE.id].sanction.kind == gw.TIMEOUT
    assert follow.decision.targets[ALICE.id].sanction.kind == gw.TIMEOUT
    assert follow.classifier.requests == 0  # a known participant: no model
    assert len(calls.jev) == 1


async def test_a_followups_scam_check_judges_the_followup_not_the_ad():
    """A ring follow-up that also needs a scam check: the MESSAGE is the new
    post; the ring's participant question is about the AD, in its own block."""
    from tests.bot.moderation_coordinator.replay_cases import DAVE
    from tests.bot.moderation_coordinator.replay_cases import staged_ring

    case, m = staged_ring()
    lure = say(
        DAVE, "FREE NITRO claim https://dlscord.gift/x", NOW + timedelta(minutes=5)
    )
    case.messages = [m["ad"], lure]
    case.answers = {**case.answers, SCAM: (True, 0.95), ABUSE: (False, 0.9)}
    case.reads.histories = {DAVE.id: FIRST}
    result = await replay(case)

    follow = result.by_message(lure.message_id)
    assert set(follow.labels_wanted) == {"scam", "abuse", f"participant:{DAVE.id}"}
    [prompt] = result.calls.haiku
    message = prompt.split("MESSAGE:\n", 1)[1].split("\nAD (", 1)[0]
    assert "dlscord.gift" in message and "HackNova" not in message
    assert "HackNova" in prompt.split("\nAD (", 1)[1]
    assert follow.decision.targets[DAVE.id].sanction.kind == gw.BAN  # 2-day account


async def test_a_burst_sees_the_first_mute_while_the_model_is_still_out():
    """The spam engine's mute is armed before the scam check waits, so the
    author's next message in the burst is not muted again (as legacy)."""
    import asyncio

    from pydantic_ai.models.function import FunctionModel

    from smarter_dev.bot.moderation_coordinator.classifier import ClassifierBudget
    from smarter_dev.bot.moderation_coordinator.classifier import ModerationClassifier
    from smarter_dev.bot.moderation_coordinator.coordinator import Coordinator
    from smarter_dev.bot.moderation_coordinator.replay import ModelCalls
    from smarter_dev.bot.moderation_coordinator.replay import scripted_haiku

    answers = {SCAM: (True, 0.95), ABUSE: (False, 0.9)}
    scripted = scripted_haiku(answers, ModelCalls())

    async def slow(messages, info):
        await asyncio.sleep(0.05)
        return scripted.function(messages, info)

    coordinator = Coordinator(
        ReplayReads(histories={NEWBIE.id: FIRST}),
        ModerationClassifier(
            budget=ClassifierBudget(per_guild_day=100), haiku_model=FunctionModel(slow)
        ),
    )
    first = say(NEWBIE, SCAM_TEXT, NOW)
    second = say(
        NEWBIE,
        "@everyone last chance https://dlscord.gift/y",
        NOW + timedelta(seconds=1),
    )
    one, two = await asyncio.gather(
        coordinator.evaluate(first, policy(ring=None)),
        coordinator.evaluate(second, policy(ring=None)),
    )
    assert "spam_engine" in one.decision.targets[NEWBIE.id].rules
    assert not any(rule.startswith("spam_engine") for rule in two.decision.rules_fired)


async def test_the_live_handlers_channel_scope_is_kept():
    case = ReplayCase(
        name="scoped scam gate",
        policy=policy(ring=None, scam_gate_channel_ids=frozenset({"elsewhere"})),
        messages=[say(NEWBIE, "my wallet https://x.example", NOW)],
        answers={SCAM: (True, 0.95)},
        reads=ReplayReads(histories={NEWBIE.id: FIRST}),
    )
    result = await replay(case)
    assert result.calls.total == 0


async def test_a_raid_past_the_budget_reads_no_ring_history():
    """#255: 120 new members each post a link in one channel within a minute.
    The guild's minute budget takes 20 requests; the other 100 would be
    refused, so their sweeps never read the channel's history (Discord's rate
    limit is shared with the live actors)."""
    from tests.bot.moderation_coordinator.replay_cases import GENERAL

    raiders = [
        Member(f"raider{i}", 500 + i, NOW - timedelta(days=3), NOW - timedelta(hours=1))
        for i in range(120)
    ]
    reads = ReplayReads(windows={GENERAL: ([], {})})
    case = ReplayCase(
        name="raid",
        policy=policy(),
        messages=[
            say(
                raider,
                f"join us https://raid{i}.example",
                NOW + timedelta(seconds=i / 2),
            )
            for i, raider in enumerate(raiders)
        ],
        answers={SCAM: (False, 0.9), ABUSE: (False, 0.9)},
        reads=reads,
    )
    result = await replay(case)

    requests = sum(e.classifier.requests for e in result.evaluations)
    refused = [e for e in result.evaluations if e.classifier.outcome == BUDGET]
    assert requests == 20
    assert len(refused) == 100
    assert reads.window_reads == requests
