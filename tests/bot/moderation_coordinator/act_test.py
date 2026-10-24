"""The cutover (#240): the coordinator's decision, carried out.

Each case runs live-shaped messages through :class:`ModerationShadow` with an
:class:`Executor` over the real moderation gateway and a fake Discord port,
for a guild the coordinator owns. What Discord saw is read back from the port;
what moderators saw, from the incident's entries.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from smarter_dev.bot.moderation_coordinator import act as act_module
from smarter_dev.bot.moderation_coordinator import rules
from smarter_dev.bot.moderation_coordinator.act import CLOSED_CLEAN
from smarter_dev.bot.moderation_coordinator.act import CLOSED_REMOVED
from smarter_dev.bot.moderation_coordinator.act import CLOSED_UNKNOWN
from smarter_dev.bot.moderation_coordinator.act import EDIT_PING_TEXT
from smarter_dev.bot.moderation_coordinator.act import INVESTIGATING
from smarter_dev.bot.moderation_coordinator.act import Executor
from smarter_dev.bot.moderation_coordinator.act import history_note
from smarter_dev.bot.moderation_coordinator.classifier import ClassifierBudget
from smarter_dev.bot.moderation_coordinator.classifier import ModerationClassifier
from smarter_dev.bot.moderation_coordinator.coordinator import Coordinator
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import PastAction
from smarter_dev.bot.moderation_coordinator.ownership import COORDINATOR
from smarter_dev.bot.moderation_coordinator.ownership import LEGACY
from smarter_dev.bot.moderation_coordinator.ownership import RULES
from smarter_dev.bot.moderation_coordinator.ownership import Ownership
from smarter_dev.bot.moderation_coordinator.policy import PolicyCache
from smarter_dev.bot.moderation_coordinator.replay import ModelCalls
from smarter_dev.bot.moderation_coordinator.replay import ReplayCase
from smarter_dev.bot.moderation_coordinator.replay import ReplayReads
from smarter_dev.bot.moderation_coordinator.replay import scripted_haiku
from smarter_dev.bot.moderation_coordinator.shadow import ModerationShadow
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationIncidentEntry
from smarter_dev.web.models import ModerationIncidentLink
from smarter_dev.web.moderation_gateway import ModerationGateway
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.gateway import default_session_factory
from tests.bot.moderation_coordinator.replay_cases import ALICE
from tests.bot.moderation_coordinator.replay_cases import BOB
from tests.bot.moderation_coordinator.replay_cases import CAROL
from tests.bot.moderation_coordinator.replay_cases import ESTABLISHED
from tests.bot.moderation_coordinator.replay_cases import GENERAL
from tests.bot.moderation_coordinator.replay_cases import GUILD
from tests.bot.moderation_coordinator.replay_cases import NOW
from tests.bot.moderation_coordinator.replay_cases import OTHER
from tests.bot.moderation_coordinator.replay_cases import THIRD
from tests.bot.moderation_coordinator.replay_cases import Member
from tests.bot.moderation_coordinator.replay_cases import policy
from tests.bot.moderation_coordinator.replay_cases import say
from tests.bot.moderation_coordinator.replay_cases import scam_1749
from tests.bot.moderation_coordinator.replay_cases import scam_burst
from tests.bot.moderation_coordinator.replay_cases import staged_ring
from tests.moderation_fakes import FakeModerationDiscord

REPORTS = "728249959098482829"
FOURTH = "702178352395583501"
FIRST = AuthorHistory(is_first_message=True, days_since_last_message=None)


def acting(case_policy, generation: int = 1):
    return replace(
        case_policy,
        owner=COORDINATOR,
        generation=generation,
        report_channel_id=REPORTS,
        mass_mention=True,
    )


@dataclass
class FakeMessenger:
    posts: list[tuple[str, str]] = field(default_factory=list)
    dms: list[tuple[str, str]] = field(default_factory=list)

    async def post(self, channel_id, content, *, mention=None):
        self.posts.append((str(channel_id), content))
        return True

    async def dm(self, user_id, content):
        self.dms.append((str(user_id), content))
        return True


class Recorder:
    """The shadow table's unique revision, in memory."""

    def __init__(self) -> None:
        self.rows = []
        self._seen: set[tuple[str, str]] = set()

    async def recorded(self, guild_id, message_id, revision) -> bool:
        return (message_id, revision) in self._seen

    async def record(self, row) -> bool:
        key = (row.message_id, row.revision)
        if key in self._seen:
            return False
        self._seen.add(key)
        self.rows.append(row)
        return True


@dataclass
class Harness:
    case: ReplayCase
    port: FakeModerationDiscord
    messenger: FakeMessenger
    shadow: ModerationShadow
    executor: Executor
    calls: ModelCalls
    recorder: Recorder
    owner: dict
    clock: list

    async def judge(self, *messages):
        for facts in messages:
            self.clock[0] = facts.posted_at
            if isinstance(self.case.reads, ReplayReads):
                self.case.reads.now = facts.posted_at
            await self.shadow.judge(facts)

    def deleted(self) -> set[str]:
        return {call[2] for call in self.port.calls if call[0] == "delete"}


def harness(case: ReplayCase, *, extra_messages=(), owner_generation: int = 1):
    port = FakeModerationDiscord(guild_id=GUILD)
    for facts in [*case.messages, *extra_messages]:
        port.add_member(facts.author_id)
        port.post(facts.channel_id, facts.message_id, facts.author_id, facts.content)
    gateway = ModerationGateway(port)
    messenger = FakeMessenger()
    owner = {"owner": COORDINATOR, "generation": owner_generation}

    async def ownership(guild_id):
        return Ownership(
            guild_id, owner["owner"], owner["generation"], frozenset(RULES)
        )

    executor = Executor(lambda _policy: gateway, messenger, ownership=ownership)
    calls = ModelCalls()
    clock = [case.messages[0].posted_at if case.messages else 0.0]
    classifier = ModerationClassifier(
        budget=ClassifierBudget(per_guild_day=10_000),
        haiku_model=case.haiku(case.answers, calls, case.answers_by_text),
        jev_model=case.jev(case.answers, calls, case.answers_by_text),
        deadline=case.deadline,
        clock=lambda: clock[0],
        fetch_image=case.fetch_image,
        choose_images=case.choose_images,
    )

    async def loader(guild_id):
        return case.policy

    recorder = Recorder()
    shadow = ModerationShadow(
        Coordinator(case.reads, classifier),
        guild_ids=frozenset({GUILD}),
        policies=PolicyCache(loader=loader),
        recorder=recorder,
        executor=executor,
    )
    return Harness(
        case, port, messenger, shadow, executor, calls, recorder, owner, clock
    )


async def entries(kind: str | None = None) -> list[str]:
    async with default_session_factory()() as session:
        query = select(ModerationIncidentEntry)
        if kind is not None:
            query = query.where(ModerationIncidentEntry.kind == kind)
        rows = (await session.execute(query)).scalars()
        return [row.rule or row.content or "" for row in rows]


def _slow(model, seconds: float = 0.05):
    answer = model.function

    async def slow(messages, info):
        await asyncio.sleep(seconds)
        return answer(messages, info)

    model.function = slow
    return model


def held_burst(answers=None):
    """A cross-channel burst with the spam engine off: the second post is
    held by the scam gate alone (no spam mute covers the hold first)."""
    case, victim, posts = scam_burst()
    case = replace(case, policy=acting(replace(case.policy, spam=None)))
    if answers is not None:
        case = replace(case, answers=answers)
    return case, victim, posts


# -- the 17:49 scam, acted on ------------------------------------------------


async def test_the_1749_scam_is_muted_and_removed_first_then_banned():
    """The spam engine's mute and removal (mass mention, nitro, scam link)
    are carried out before the classifier is asked (#310 item 4). The
    5-minute hold then finds the member already muted: covered, so no
    "investigating" notice is posted for a message that is already gone."""
    case, scammer = scam_1749(account_age_days=2)
    case = replace(case, policy=acting(case.policy))
    h = harness(case)
    post = case.messages[0]

    await h.judge(post)

    kinds = [call[0] for call in h.port.calls]
    assert kinds == ["timeout", "delete", "ban"]
    assert ("ban", scammer.id) == h.port.calls[kinds.index("ban")][:2]
    assert post.message_id in h.deleted()
    assert h.messenger.posts == []
    notes = await entries(gw.ENTRY_NOTE)
    assert any(
        note.startswith("History: no earlier moderation actions") for note in notes
    )
    # One report, in the moderators' channel.
    assert {p["channel_id"] for p in h.port.posts} == {REPORTS}
    assert h.calls.total == 1


async def test_a_clean_verdict_releases_the_hold_and_closes_its_notice():
    case, victim, posts = scam_burst()
    case = replace(
        case,
        policy=acting(case.policy),
        answers={SCAM: (False, 0.95), ABUSE: (False, 0.95)},
    )
    h = harness(case)

    await h.judge(*posts)

    timeouts = [call for call in h.port.calls if call[0] == "timeout"]
    assert timeouts[0][2] is not None  # held while judged
    assert timeouts[-1] == ("timeout", victim.id, None)  # released
    assert h.deleted() == set()
    assert (posts[1].channel_id, INVESTIGATING) in h.messenger.posts
    assert (posts[1].channel_id, CLOSED_CLEAN) in h.messenger.posts
    assert any("released" in note for note in await entries(gw.ENTRY_NOTE))


async def test_no_verdict_leaves_the_hold_and_alerts_with_the_cause():
    """#302: a hold that just expires is never silent."""
    case, victim, posts = scam_burst()
    case = replace(case, policy=acting(case.policy), answers={})
    h = harness(case)

    await h.judge(*posts)

    assert h.port.members[victim.id] is not None  # still held; it runs out
    alerts = await entries(gw.ENTRY_ALERT)
    assert any(
        "expired without a verdict" in alert and "invalid output" in alert
        for alert in alerts
    )
    assert (posts[1].channel_id, CLOSED_UNKNOWN) in h.messenger.posts


async def test_a_failed_evaluation_after_the_hold_is_reported(monkeypatch):
    case, victim, posts = held_burst()
    h = harness(case)

    async def broken(request):
        raise RuntimeError("model down")

    monkeypatch.setattr(h.shadow.coordinator.classifier, "classify", broken)
    await h.judge(posts[0], posts[1])

    assert [call[0] for call in h.port.calls] == ["timeout"]  # the hold only
    alerts = await entries(gw.ENTRY_ALERT)
    assert any("failed after the hold" in a and "RuntimeError" in a for a in alerts)
    assert h.messenger.posts[-1] == (posts[1].channel_id, CLOSED_UNKNOWN)
    assert {row.error for row in h.recorder.rows} == {"RuntimeError"}


# -- every copy, and only the author's ----------------------------------------


async def test_every_copy_of_a_confirmed_scam_goes_and_only_the_authors():
    """Oct 23 20:30: none of the copies were removed. A copy posted while the
    verdict was being asked goes too; another member who pasted the same text
    keeps theirs (cleanup is the same author's same post only)."""
    case, victim, posts = scam_burst()
    copycat = Member(
        "copycat", 11, NOW - timedelta(days=500), NOW - timedelta(days=400)
    )
    theirs = say(copycat, posts[0].content, NOW + timedelta(seconds=1), channel=FOURTH)
    case = replace(case, policy=acting(case.policy))
    case = replace(
        case,
        haiku=lambda answers, calls, by_text: _slow(
            scripted_haiku(answers, calls, by_text)
        ),
        reads=ReplayReads(histories={victim.id: ESTABLISHED, copycat.id: ESTABLISHED}),
    )
    h = harness(case, extra_messages=[theirs])

    await h.judge(posts[0], theirs)
    # The second copy is being judged when the third arrives.
    second = asyncio.create_task(h.judge(posts[1]))
    # Until the second copy's check is out (its early spam warning goes
    # through the gateway first).
    while not h.shadow.coordinator.verdict_cache._entries:
        await asyncio.sleep(0.005)
    await h.judge(posts[2])
    await second

    assert {p.message_id for p in posts} <= h.deleted()
    assert theirs.message_id not in h.deleted()
    assert h.calls.total == 1
    # The second copy's own decision already named the third (read again
    # after the verdict).
    row = next(r for r in h.recorder.rows if r.message_id == posts[1].message_id)
    cleaned = {m for _, m in row.decision["targets"][victim.id]["cleanup"]}
    assert posts[2].message_id in cleaned


class _FirstThenEstablished(ReplayReads):
    async def author_history(self, guild_id, user_id, before):
        seen = getattr(self, "_seen", 0)
        self._seen = seen + 1
        return FIRST if seen == 0 else ESTABLISHED


async def test_a_later_copy_goes_even_when_its_own_gates_no_longer_fire():
    """#239's 01:05 case: the author's second post 19 s later was no longer a
    first message, so nothing checked it. It reuses the verdict and goes."""
    member = Member(
        "airdrop_al", 12, NOW - timedelta(days=60), NOW - timedelta(days=50)
    )
    lure = "grab the free crypto airdrop https://airdrop-claim.example/now"
    first = say(member, lure, NOW)
    again = say(member, lure, NOW + timedelta(seconds=19))
    case = ReplayCase(
        name="later copy",
        policy=acting(policy(ring=None)),
        messages=[first, again],
        answers={SCAM: (True, 0.9), ABUSE: (False, 0.9)},
        reads=_FirstThenEstablished(),
    )
    h = harness(case)

    await h.judge(first, again)

    assert {first.message_id, again.message_id} <= h.deleted()
    assert h.calls.total == 1
    async with default_session_factory()() as session:
        warns = (
            await session.execute(
                select(ModerationAction).where(ModerationAction.action_type == "warn")
            )
        ).all()
    assert len(warns) == 1  # one warn for the post and its copy


# -- the fence and the record --------------------------------------------------


async def test_a_rollback_mid_decision_stops_it_acting():
    case, _ = scam_1749(account_age_days=2)
    case = replace(case, policy=acting(case.policy, generation=1))
    h = harness(case, owner_generation=2)  # rolled back (and maybe forward)

    await h.judge(case.messages[0])

    assert h.port.calls == []
    assert h.messenger.posts == []
    assert h.recorder.rows[0].strongest_action == gw.BAN  # still recorded


async def test_a_guild_still_owned_by_legacy_is_only_recorded():
    case, _ = scam_1749(account_age_days=2)
    h = harness(case)  # the replay policy: owner legacy
    assert case.policy.owner == LEGACY

    await h.judge(case.messages[0])

    assert h.port.calls == []
    assert h.messenger.posts == []
    assert h.recorder.rows[0].strongest_action == gw.BAN


async def test_a_replayed_revision_is_not_acted_on_twice(monkeypatch):
    """A handover replays events the old pod may have handled: the revision
    is already recorded, so it is not carried out again."""
    case, _ = scam_1749(account_age_days=2)
    case = replace(case, policy=acting(case.policy))
    h = harness(case)
    executed = []
    original = h.executor.execute

    async def spy(evaluation):
        executed.append(evaluation.facts.message_id)
        return await original(evaluation)

    monkeypatch.setattr(h.executor, "execute", spy)
    await h.judge(case.messages[0])
    first_pass = list(executed)
    await h.judge(case.messages[0])

    # The deterministic part and then the rest; nothing on the replay.
    assert first_pass == [case.messages[0].message_id] * 2
    assert executed == first_pass


async def test_accepted_decisions_are_tracked_for_the_handover_drain():
    """#255 item 1: the stopping process drains the decisions it accepted."""
    from smarter_dev.bot import leadership

    case, _ = scam_1749(account_age_days=2)
    case = replace(case, policy=acting(case.policy))
    h = harness(case)
    task = h.shadow.submit(case.messages[0])
    assert task in leadership.tracked()
    await h.shadow.drain()
    assert task not in leadership.tracked()


# -- notices and rings ---------------------------------------------------------


async def test_an_edited_in_mass_mention_is_removed_with_its_notice():
    member = Member("ed", 13, NOW - timedelta(days=400), NOW - timedelta(days=300))
    edit = say(member, "hello @everyone", NOW, is_edit=True)
    case = ReplayCase(name="edit ping", policy=acting(policy()), messages=[edit])
    h = harness(case)

    await h.judge(edit)

    assert h.deleted() == {edit.message_id}
    assert (edit.channel_id, EDIT_PING_TEXT.format(user=member.id)) in h.messenger.posts
    # The handler never reported; the mass-mention rule (#310 item 10) does.
    assert [p["channel_id"] for p in h.port.posts] == [REPORTS]


async def test_a_ring_and_its_followup_are_one_incident_with_a_transcript():
    case, m = staged_ring()
    case = replace(case, policy=acting(case.policy))
    h = harness(case)

    await h.judge(*case.messages)

    timed_out = {call[1] for call in h.port.calls if call[0] == "timeout"}
    assert {ALICE.id, BOB.id, CAROL.id} <= timed_out
    assert m["ad"].message_id in h.deleted()
    assert m["cheer"].message_id in h.deleted()
    async with default_session_factory()() as session:
        aliases = (
            await session.execute(
                select(ModerationIncidentLink.incident_id).where(
                    ModerationIncidentLink.link_key.like("alias:coordinator_ring:%")
                )
            )
        ).scalars()
        assert len(set(aliases)) == 1
    assert await entries(gw.ENTRY_TRANSCRIPT)


def test_the_history_note_comes_from_the_record():
    history = AuthorHistory(
        is_first_message=False,
        days_since_last_message=1,
        actions=tuple(
            PastAction("warn", NOW - timedelta(days=i), reason=f"spam @here {i}")
            for i in range(7)
        ),
    )
    note = history_note("42", history)
    assert note.startswith("History: 7 earlier action(s) on record for <@42>:")
    assert "2026-10-18 warn: spam @​here 0" in note
    assert note.endswith("(+2 more)")
    assert act_module.HISTORY_LINES == 5


async def test_the_spam_engines_scam_link_archive_is_still_posted():
    case, scammer = scam_1749(account_age_days=30)
    spam = replace(case.policy.spam, scam_log_channel_id="999")
    case = replace(case, policy=acting(replace(case.policy, spam=spam)))
    h = harness(case)

    await h.judge(case.messages[0])

    [archive] = [content for channel, content in h.messenger.posts if channel == "999"]
    assert scammer.id in archive
    assert "https://dlscord.gift" not in archive  # defanged


async def test_a_spam_mute_discord_refused_leaves_the_author_counted():
    """The engine arms its re-entrancy guard only once the author is timed
    out; a refused timeout must not let them off for 24 hours."""
    from smarter_dev.web.moderation_gateway.types import EffectRejected

    case, scammer = scam_1749(account_age_days=30)
    case = replace(case, policy=acting(policy(scam_gate=False, ring=None)))
    h = harness(case)
    h.port.fail["set_timeout"] = EffectRejected(403, "hierarchy")
    post = case.messages[0]

    await h.judge(post)

    decided = h.recorder.rows[0].decision["targets"][scammer.id]
    assert decided["sanction"]["kind"] == gw.TIMEOUT
    state = h.shadow.coordinator.spam._states[GUILD]
    assert not state.is_muted(scammer.id, post.posted_at + 1)


async def test_a_revision_decided_as_owner_is_not_compared_with_legacy():
    """After the cutover there is no legacy side: the comparison marks the
    row ``acting`` instead of calling every action stronger than legacy."""
    import time
    from datetime import UTC
    from datetime import datetime

    from smarter_dev.bot.moderation_coordinator.shadow import ACTING
    from smarter_dev.bot.moderation_coordinator.shadow import LegacyComparer
    from smarter_dev.web.models import ModerationShadowDecision

    async with default_session_factory()() as session:
        session.add(
            ModerationShadowDecision(
                guild_id=GUILD,
                channel_id=GENERAL,
                message_id="1",
                revision="create",
                is_edit=False,
                author_id="42",
                message_at=datetime.now(UTC),
                policy={"owner": COORDINATOR, "generation": 1},
                strongest_action=gw.BAN,
                target_ids=["42"],
            )
        )
        await session.commit()
    await LegacyComparer(clock=lambda: time.time() + 300).sweep()
    async with default_session_factory()() as session:
        row = (await session.execute(select(ModerationShadowDecision))).scalar_one()
    assert row.agreement == ACTING


async def test_a_replayed_high_signal_message_is_not_held_again():
    """Review of #240: the duplicate check ran after the hold was placed, so a
    replay re-held a member the first pass had already cleared."""
    case, victim, posts = scam_burst()
    case = replace(
        case,
        policy=acting(case.policy),
        answers={SCAM: (False, 0.95), ABUSE: (False, 0.95)},
    )
    h = harness(case)
    await h.judge(*posts)
    timeouts = h.port.count("timeout")
    notices = list(h.messenger.posts)

    # The successor pod: none of the first pod's memory, the same table.
    from smarter_dev.bot.moderation_coordinator.state import RecentPosts
    from smarter_dev.bot.moderation_coordinator.state import VerdictCache

    h.shadow.coordinator.verdict_cache = VerdictCache()
    h.shadow.coordinator.recent = RecentPosts()
    await h.judge(posts[0], posts[1])

    assert h.port.count("timeout") == timeouts
    assert h.messenger.posts == notices


async def test_a_copy_without_its_own_hold_does_not_close_the_posts_notice():
    """Review of #240: copies share the post's hold; only a decision with a
    provisional outcome closes it, with that outcome's line."""
    case, victim, posts = scam_burst()
    case = replace(
        case,
        policy=acting(case.policy),
        answers={SCAM: (False, 0.95), ABUSE: (False, 0.95)},
    )
    h = harness(case)
    await h.judge(*posts)
    closings = [c for _, c in h.messenger.posts if c.startswith("-# Check")]
    assert closings == [CLOSED_CLEAN]


async def test_review_lines_alone_open_no_report(monkeypatch):
    """An unread history on a message nothing fired on is logged, not reported."""
    from smarter_dev.bot.moderation_coordinator import coordinator as coordinator_module

    monkeypatch.setattr(coordinator_module, "HISTORY_READ_SECONDS", 0.01)

    class Slow(ReplayReads):
        async def author_history(self, guild_id, user_id, before):
            await asyncio.sleep(1)

    member = Member("calm", 60, NOW - timedelta(days=900), NOW - timedelta(days=800))
    post = say(member, "docs at https://docs.python.org/3/", NOW)
    case = ReplayCase(
        name="quiet", policy=acting(policy(ring=None)), messages=[post], reads=Slow()
    )
    h = harness(case)
    await h.judge(post)
    assert h.port.posts == [] and h.port.calls == []
    assert await entries() == []


async def test_the_secure_account_dm_follows_an_applied_timeout():
    case, scammer = scam_1749(account_age_days=30)
    case = replace(case, policy=acting(case.policy))
    h = harness(case)
    await h.judge(case.messages[0])
    assert [user for user, _ in h.messenger.dms] == [scammer.id]


async def test_no_dm_when_the_gateway_did_not_apply_the_timeout():
    """Review of #240: the 'secure your account' DM follows the timeout only
    once it took effect; the gateway's own staff check refusing sends none."""
    case, scammer = scam_1749(account_age_days=30)
    case = replace(case, policy=acting(case.policy))
    h = harness(case)
    h.port.protected[scammer.id] = "holds a staff role"
    await h.judge(case.messages[0])
    assert h.messenger.dms == []


# -- a hold always closes (#310 item 1) ----------------------------------------


async def test_a_raise_while_carrying_out_the_decision_still_closes_the_hold(
    monkeypatch,
):
    """No verdict: the decision has a hold to close and nothing to propose,
    so it opens the incident with ensure_incident. That raising used to leave
    the hold notice open, with no line for moderators."""
    case, victim, posts = scam_burst()
    case = replace(case, policy=acting(case.policy), answers={})
    h = harness(case)
    gateway = h.executor._gateway_for(case.policy)

    original = h.executor.contain

    async def broken(*args, **kwargs):
        raise RuntimeError("database gone")

    async def contain_then_break(*args, **kwargs):
        await original(*args, **kwargs)
        monkeypatch.setattr(gateway, "ensure_incident", broken)

    h.executor.contain = contain_then_break
    await h.judge(*posts)

    assert (posts[1].channel_id, INVESTIGATING) in h.messenger.posts
    assert h.messenger.posts[-1] == (posts[1].channel_id, CLOSED_UNKNOWN)
    alerts = await entries(gw.ENTRY_ALERT)
    assert any("stopped before its decision was carried out" in a for a in alerts)
    assert h.executor._holds == {}


async def test_a_rollback_after_the_hold_still_closes_it():
    """The generation moves between the hold and the decision: nothing more
    is done to the member, but the notice is closed and moderators told."""
    case, victim, posts = held_burst()
    h = harness(case)
    post = posts[1]
    original = h.executor.contain

    async def contain_then_roll_back(*args, **kwargs):
        await original(*args, **kwargs)
        h.owner["generation"] = 2

    h.executor.contain = contain_then_roll_back
    await h.judge(posts[0], post)

    assert [call[0] for call in h.port.calls] == ["timeout"]  # the hold only
    assert h.messenger.posts == [
        (post.channel_id, INVESTIGATING),
        (post.channel_id, CLOSED_UNKNOWN),
    ]
    alerts = await entries(gw.ENTRY_ALERT)
    assert any("stopped before its decision was carried out" in a for a in alerts)
    assert h.executor._holds == {}


async def test_a_second_pod_that_lost_the_record_closes_its_own_notice():
    """#310 item 8: two pods judge the same message in a handover. The one
    whose hold posted the notice may lose the record race; it still closes
    its notice (with its own verdict's line) and carries out nothing."""
    case, victim, posts = held_burst()
    h = harness(case)
    await h.judge(posts[0])
    post = posts[1]
    executed = []
    original_record = h.recorder.record

    async def lose_the_race(row):
        await original_record(row)  # the other pod's row lands first
        return False

    async def spy(evaluation):
        executed.append(evaluation)

    h.recorder.record = lose_the_race
    h.executor.execute = spy
    await h.judge(post)

    assert executed == []
    assert h.messenger.posts == [
        (post.channel_id, INVESTIGATING),
        (post.channel_id, CLOSED_REMOVED),
    ]
    assert h.executor._holds == {}


# -- nothing dropped while the coordinator owns the guild (#310 item 4) --------


async def _flood(h, spammer, count=12):
    start = NOW + timedelta(seconds=30)
    for i in range(count):
        facts = say(spammer, f"buy now {i}", start + timedelta(seconds=i * 0.3))
        h.port.post(facts.channel_id, facts.message_id, facts.author_id, facts.content)
        h.clock[0] = facts.posted_at
        h.case.reads.now = facts.posted_at
        h.shadow.submit(facts)


async def test_a_spam_flood_behind_a_full_queue_still_gets_the_spam_mute(
    monkeypatch,
):
    """#310 item 4: past the queue, a guild the coordinator owns used to drop
    messages, so during a raid nobody acted. They are judged on the
    deterministic rules instead, and the spam mute lands while the queued
    message is still waiting on the classifier."""
    from smarter_dev.bot.moderation_coordinator import shadow as shadow_module

    monkeypatch.setattr(shadow_module, "MAX_PENDING", 1)
    blocker = Member("newbie", 80, NOW - timedelta(days=1), NOW - timedelta(hours=1))
    spammer = Member("raider", 81, NOW - timedelta(days=400), NOW - timedelta(days=300))
    slow_post = say(blocker, "free nitro https://nitro-claim.example/x", NOW)
    case = ReplayCase(
        name="raid",
        policy=acting(policy(ring=None)),
        messages=[slow_post],
        answers={SCAM: (False, 0.9), ABUSE: (False, 0.9)},
        haiku=lambda answers, calls, by_text: _slow(
            scripted_haiku(answers, calls, by_text), 0.5
        ),
        reads=ReplayReads(histories={blocker.id: FIRST}),
    )
    h = harness(case)
    h.port.add_member(spammer.id)

    h.shadow.submit(slow_post)
    await _flood(h, spammer)
    await asyncio.sleep(0.2)  # the slow check is still out
    assert h.port.count("timeout", spammer.id) == 1
    await h.shadow.drain()

    assert h.shadow.dropped == 0
    assert h.shadow.backlogged == 12


async def test_a_full_queue_in_a_shadow_guild_still_drops(monkeypatch):
    """The control: a guild still owned by legacy is only compared, so past
    the queue its messages are dropped and counted, as before."""
    from smarter_dev.bot.moderation_coordinator import shadow as shadow_module

    monkeypatch.setattr(shadow_module, "MAX_PENDING", 1)
    spammer = Member("raider", 82, NOW - timedelta(days=400), NOW - timedelta(days=300))
    first = say(spammer, "hello", NOW)
    case = ReplayCase(name="shadow raid", policy=policy(ring=None), messages=[first])
    h = harness(case)
    await h.shadow.policies.get(GUILD)  # seen before: legacy
    h.shadow.submit(first)
    await _flood(h, spammer, count=5)
    await h.shadow.drain()

    assert h.shadow.dropped == 5
    assert h.port.calls == []


# -- @everyone/@here from members (#310 item 10, Zech Oct 23 22:36) ------------


async def _ping_case(*messages, staff=None, histories=None, answers=None, **over):
    case = ReplayCase(
        name="mass mention",
        policy=acting(policy(ring=None, **over)),
        messages=list(messages),
        answers=answers or {SCAM: (False, 0.9), ABUSE: (False, 0.9)},
        reads=ReplayReads(staff=staff or {}, histories=histories or {}),
    )
    h = harness(case)
    await h.judge(*messages)
    return h


async def _actions() -> list[tuple[str, str]]:
    async with default_session_factory()() as session:
        rows = (
            await session.execute(
                select(ModerationAction.action_type, ModerationAction.reason)
            )
        ).all()
    return [(row[0], row[1]) for row in rows]


PINGER = Member("pinger", 90, NOW - timedelta(days=400), NOW - timedelta(days=300))


async def test_a_members_everyone_is_deleted_logged_and_warned_by_dm():
    """Zech, Oct 24: the removal stays, and the member gets a formal warning
    on mod history, told by DM; no timeout, and no notice in the channel."""
    post = say(PINGER, "@everyone check this", NOW)
    h = await _ping_case(post)

    assert [call[0] for call in h.port.calls] == ["delete"]  # no timeout
    assert post.message_id in h.deleted()
    actions = await _actions()
    assert sorted(kind for kind, _ in actions) == ["delete", "warn"]
    assert (gw.WARN, rules.MASS_MENTION_WARNING) in actions
    assert h.messenger.dms == [
        (PINGER.id, act_module._warning_dm(rules.MASS_MENTION_WARNING))
    ]
    assert not [
        text for channel, text in h.messenger.posts if channel == post.channel_id
    ]
    reports = [p for p in h.port.posts if p["channel_id"] == REPORTS]
    assert len(reports) == 1  # the mod-log line
    assert h.calls.total == 0


async def test_an_edit_that_adds_here_is_deleted():
    edit = say(PINGER, "updated: @here look", NOW, is_edit=True)
    h = await _ping_case(edit, edit_ping=False)  # the new rule on its own
    assert edit.message_id in h.deleted()
    assert sorted(kind for kind, _ in await _actions()) == ["delete", "warn"]


async def test_staff_may_ping_everyone():
    mod = Member("mod", 91, NOW - timedelta(days=900), NOW - timedelta(days=800))
    post = say(mod, "@everyone maintenance tonight", NOW)
    h = await _ping_case(post, staff={mod.id: "holds a staff role"})
    assert h.port.calls == []
    assert h.port.posts == []
    assert h.messenger.dms == [] and await _actions() == []  # no warning


async def test_a_bot_or_a_webhook_pinging_everyone_is_not_warned():
    bot_post = replace(say(PINGER, "@everyone deploy done", NOW), author_is_bot=True)
    hook = replace(
        say(PINGER, "@here new release", NOW + timedelta(seconds=30)), is_webhook=True
    )
    h = await _ping_case(bot_post, hook)
    assert h.deleted() == set()
    assert h.messenger.dms == [] and await _actions() == []


async def test_everyone_in_code_is_left_alone():
    posts = [
        say(PINGER, "the bot replies to `@everyone` literally", NOW),
        say(
            PINGER,
            "```\nif text == '@here':\n    return\n```",
            NOW + timedelta(seconds=40),
        ),
    ]
    h = await _ping_case(*posts)
    assert h.deleted() == set()
    assert await _actions() == []
    assert h.messenger.dms == []  # no warning


async def test_everyone_with_a_scam_link_is_deleted_held_and_reviewed_in_one_report():
    # No nitro bait, so the spam engine stays out: the removal, then the
    # scam gate's hold and verdict.
    post = say(PINGER, "@everyone claim your crypto https://prize-drop.example/x", NOW)
    h = await _ping_case(post, answers={SCAM: (True, 0.95), ABUSE: (False, 0.9)})

    assert post.message_id in h.deleted()
    assert h.calls.total == 1  # reviewed
    row = h.recorder.rows[0]
    assert row.decision["targets"][PINGER.id]["sanction"]["kind"] == gw.TIMEOUT
    timeouts = [call[2] for call in h.port.calls if call[0] == "timeout"]
    assert h.port.calls[0][0] == "delete"  # removed before the hold
    assert len(timeouts) == 2  # the 5-minute hold, then the 24 h timeout
    assert timeouts[1] - timeouts[0] > 3600
    async with default_session_factory()() as session:
        incidents = (
            await session.execute(select(ModerationIncidentLink.incident_id))
        ).scalars()
        assert len(set(incidents)) == 1
    assert len([p for p in h.port.posts if p["channel_id"] == REPORTS]) == 1


# -- risk from earlier actions, by type (Zech, Oct 24) -------------------------


async def _record(*rows: tuple[str, timedelta]) -> None:
    """Moderation actions on PINGER as a moderator records them: each
    ``(action_type, how long before NOW)``."""
    async with default_session_factory()() as session:
        for action_type, ago in rows:
            session.add(
                ModerationAction(
                    guild_id=GUILD,
                    target_user_id=PINGER.id,
                    target_username=PINGER.name,
                    moderator_user_id="1",
                    moderator_username="a mod",
                    action_type=action_type,
                    reason="by hand",
                    source="manual",
                    created_at=NOW - ago,
                )
            )
        await session.commit()


async def _risk(at=NOW) -> tuple[str, ...]:
    """PINGER's risk reasons at ``at``, from the history read back from the
    database (as after a restart); a known member, so only the record
    counts."""
    from smarter_dev.bot.moderation_coordinator.shadow import BotReads

    history = await BotReads(bot=None).author_history(GUILD, PINGER.id, at)
    history = replace(history, is_first_message=False, days_since_last_message=0)
    return rules.scam_risky_author(say(PINGER, "hi", at), history, at)


@pytest.mark.parametrize(
    ("rows", "risky"),
    [
        ([("warn", timedelta(hours=2))], True),
        ([("warn", timedelta(days=2))], False),
        ([("timeout", timedelta(days=20))], True),
        ([("kick", timedelta(days=20))], True),
        ([("ban", timedelta(days=20))], True),
        ([("timeout", timedelta(days=40))], False),
        ([("kick", timedelta(days=40))], False),
        ([("ban", timedelta(days=40))], False),
        # Unbanned since: the ban still counts.
        ([("ban", timedelta(days=10)), ("unban", timedelta(days=9))], True),
        ([("untimeout", timedelta(hours=1)), ("unban", timedelta(hours=1))], False),
    ],
    ids=[
        "warn 2h",
        "warn 2d",
        "timeout 20d",
        "kick 20d",
        "ban 20d",
        "timeout 40d",
        "kick 40d",
        "ban 40d",
        "unbanned, banned 10d",
        "lifts only",
    ],
)
async def test_earlier_actions_make_an_author_risky_by_their_type(rows, risky):
    """A warning leaves the author high risk for 24 h; a timeout, kick or
    ban for 30 days. One reason, keyed on the action's type."""
    await _record(*rows)
    reasons = await _risk()
    assert reasons == ((rules.RECENTLY_MODERATED,) if risky else ())


async def test_a_review_hold_is_no_moderation_action():
    """A post held while it was checked and found clean: its 5-minute hold
    and the hold's release are on record, but neither is an action on the
    member. An hour later the author is not high risk and their history
    lists nothing. (Before, the hold's ``timeout`` row counted as
    "moderated in the last 30 days".)"""
    from datetime import UTC
    from datetime import datetime

    newbie = Member("held", 101, NOW - timedelta(days=30), NOW - timedelta(hours=2))
    post = say(newbie, "my setup", NOW, attachments=_shots(4))
    case = ReplayCase(
        name="clean after a hold",
        policy=acting(policy(ring=None, spam=None)),
        messages=[post],
        answers={SCAM: (False, 0.95), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={newbie.id: FIRST}),
    )
    h = harness(case)
    await h.judge(post)
    timeouts = [call[2] for call in h.port.calls if call[0] == "timeout"]
    assert len(timeouts) == 2 and timeouts[1] is None  # held, then released
    async with default_session_factory()() as session:
        recorded = (
            await session.execute(select(ModerationAction.action_type))
        ).scalars()
        assert "timeout" in set(recorded)  # the hold is on record

    from smarter_dev.bot.moderation_coordinator.shadow import BotReads

    hour_later = datetime.now(UTC) + timedelta(hours=1)
    history = await BotReads(bot=None).author_history(GUILD, newbie.id, hour_later)
    assert history.actions == ()
    history = replace(history, is_first_message=False, days_since_last_message=0)
    facts = say(newbie, "hi", hour_later)
    assert rules.scam_risky_author(facts, history, hour_later) == ()


def test_review_holds_are_told_apart_by_their_operation_not_their_reason():
    from smarter_dev.bot.moderation_coordinator.shadow import is_review_hold

    assert is_review_hold("timeout", {"provisional": True, "until": 1.0})
    # A release, also when it was shortened to another claim (then recorded
    # as a timeout row).
    assert is_review_hold("untimeout", {"until": 1.0})
    assert not is_review_hold("timeout", {"duration_seconds": 86400})
    assert not is_review_hold("timeout", None)
    assert not is_review_hold("warn", {"provisional": True})
    assert not is_review_hold(None, None)  # a moderator's, outside the gateway


async def test_a_hold_that_ends_in_a_timeout_counts_the_timeout():
    """A held post confirmed a scam: the hold is still no action, the 24 h
    timeout it became is one (30 days of risk)."""
    from datetime import UTC
    from datetime import datetime

    from smarter_dev.bot.moderation_coordinator.shadow import BotReads

    newbie = Member("held2", 102, NOW - timedelta(days=30), NOW - timedelta(hours=2))
    post = say(newbie, "my setup", NOW, attachments=_shots(4))
    case = ReplayCase(
        name="scam after a hold",
        policy=acting(policy(ring=None, spam=None)),
        messages=[post],
        answers={SCAM: (True, 0.95), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={newbie.id: FIRST}),
    )
    h = harness(case)
    await h.judge(post)
    later = datetime.now(UTC) + timedelta(days=20)
    history = await BotReads(bot=None).author_history(GUILD, newbie.id, later)
    assert [action.action_type for action in history.actions] == ["timeout"]
    history = replace(history, is_first_message=False, days_since_last_message=0)
    facts = say(newbie, "hi", later)
    assert rules.scam_risky_author(facts, history, later) == (rules.RECENTLY_MODERATED,)


async def test_a_ping_warning_is_a_day_of_risk_not_30_days():
    """The mass-mention warning is a warning like any other: two hours after
    it the author is high risk, two days after it not."""
    from datetime import UTC
    from datetime import datetime

    from smarter_dev.bot.moderation_coordinator.shadow import BotReads

    post = say(PINGER, "@everyone check this", NOW)
    await _ping_case(post)
    async with default_session_factory()() as session:
        warned_at = (
            await session.execute(
                select(ModerationAction.created_at).where(
                    ModerationAction.action_type == gw.WARN
                )
            )
        ).scalar_one()
    warned_at = warned_at if warned_at.tzinfo else warned_at.replace(tzinfo=UTC)
    reads = BotReads(bot=None)
    facts = say(PINGER, "hi", datetime.fromtimestamp(0, UTC))
    for after, risky in ((timedelta(hours=2), True), (timedelta(days=2), False)):
        at = warned_at + after
        history = replace(
            await reads.author_history(GUILD, PINGER.id, at),
            is_first_message=False,
            days_since_last_message=0,
        )
        reasons = rules.scam_risky_author(facts, history, at)
        assert reasons == ((rules.RECENTLY_MODERATED,) if risky else ())


async def test_a_ping_warning_with_closed_dms_is_still_recorded_and_reported():
    """The DM fails (closed DMs): the warning stands on mod history and the
    report says the member was not told; no message in the channel."""
    post = say(PINGER, "@here look", NOW)
    case = ReplayCase(
        name="closed DMs",
        policy=acting(policy(ring=None)),
        messages=[post],
        answers={SCAM: (False, 0.9), ABUSE: (False, 0.9)},
    )
    h = harness(case)

    async def closed(user_id, content):
        h.messenger.dms.append((str(user_id), content))
        return False

    h.messenger.dm = closed
    await h.judge(post)

    assert (gw.WARN, rules.MASS_MENTION_WARNING) in await _actions()
    assert len(h.messenger.dms) == 1  # tried once
    assert not [
        text for channel, text in h.messenger.posts if channel == post.channel_id
    ]
    assert len([p for p in h.port.posts if p["channel_id"] == REPORTS]) == 1
    assert "Could not DM" in h.port.latest_report_text(REPORTS)


async def test_a_burst_of_one_ping_is_warned_once():
    """Copies of one @everyone post in three channels (and a re-send) share
    one incident: one warning, one DM, every copy removed."""
    channels = [GENERAL, OTHER, THIRD]
    posts = [
        replace(
            say(PINGER, "@everyone free stuff", NOW + timedelta(seconds=i)),
            channel_id=channel,
        )
        for i, channel in enumerate(channels)
    ]
    posts.append(say(PINGER, "@everyone   FREE stuff", NOW + timedelta(seconds=40)))
    h = await _ping_case(*posts)

    assert {post.message_id for post in posts} <= h.deleted()
    warns = [reason for kind, reason in await _actions() if kind == gw.WARN]
    assert warns == [rules.MASS_MENTION_WARNING]
    assert len(h.messenger.dms) == 1


async def test_an_exhausted_budget_is_reported_once_a_day():
    """#310 item 5: when the acting ceiling runs out, one WARNING and one
    line in the moderators' channel, not one per message."""
    from smarter_dev.bot.moderation_coordinator.shadow import BUDGET_EXHAUSTED_TEXT

    newbie = Member("newbie", 95, NOW - timedelta(days=1), NOW - timedelta(hours=1))
    posts = [
        say(
            newbie,
            f"free crypto https://claim{i}.example/x",
            NOW + timedelta(seconds=i),
        )
        for i in range(3)
    ]
    case = ReplayCase(
        name="budget",
        policy=acting(policy(ring=None, spam=None)),
        messages=posts,
        answers={SCAM: (False, 0.9), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={newbie.id: FIRST}),
    )
    h = harness(case)
    budget = h.shadow.coordinator.classifier.budget
    budget.acting_per_guild_day = 0
    await h.judge(*posts)

    lines = [text for channel, text in h.messenger.posts if channel == REPORTS]
    assert lines == [BUDGET_EXHAUSTED_TEXT.format(cap=0)]
    assert h.calls.total == 0


async def test_the_spam_mute_lands_before_the_classifier_answers():
    """#310 item 4: spam timeouts used to wait behind the classifier. A new
    member flooding links is reviewed on each post (slow model), and the spam
    engine's mute is carried out while that review is still out."""
    flooder = Member("flooder", 83, NOW - timedelta(days=1), NOW - timedelta(hours=2))
    posts = [
        say(
            flooder, f"look https://site{i}.example/x", NOW + timedelta(seconds=i * 0.3)
        )
        for i in range(12)
    ]
    case = ReplayCase(
        name="flood with links",
        policy=acting(policy(ring=None)),
        messages=posts,
        answers={SCAM: (False, 0.9), ABUSE: (False, 0.9)},
        haiku=lambda answers, calls, by_text: _slow(
            scripted_haiku(answers, calls, by_text), 1.0
        ),
        reads=ReplayReads(histories={flooder.id: FIRST}),
    )
    h = harness(case)
    budget = h.shadow.coordinator.classifier.budget
    budget.per_author = budget.per_guild_minute = 100  # every post is reviewed
    for facts in posts:
        h.clock[0] = facts.posted_at
        h.case.reads.now = facts.posted_at
        h.shadow.submit(facts)
    await asyncio.sleep(0.5)
    assert h.calls.total == 0  # no review has answered yet
    assert h.port.count("timeout", flooder.id) >= 1
    await h.shadow.drain()


# -- the re-review of a326953a (#310) -----------------------------------------


def _copy_in(facts, channel_id: str, seconds: float = 2):
    """The same post in another channel: the same files (so the same content
    hash), served from the copy's own URLs (``/copy/``)."""
    return replace(
        facts,
        message_id=str(int(facts.message_id) + 1),
        channel_id=channel_id,
        posted_at=facts.posted_at + seconds,
        attachments=tuple(
            replace(item, url=item.url.replace("/1/2/", "/1/copy/"))
            for item in facts.attachments
        ),
    )


def _shots(count: int, prefix: str = "shot"):
    from smarter_dev.bot.moderation_coordinator.model import Attachment

    return tuple(
        Attachment(
            f"{prefix}{i}.png",
            "image/png",
            20_000,
            f"https://cdn.discordapp.com/attachments/1/2/{prefix}{i}.png",
        )
        for i in range(count)
    )


async def test_an_early_deleted_message_still_shows_the_scam_check_its_images():
    """Re-review item 1: the mass-mention removal ran before the scam check,
    so the CDN no longer served the images, the check was blind (unknown)
    and the new account was not banned. The images are loaded first."""
    from smarter_dev.bot.moderation_coordinator.replay import REPLAY_PNG

    newbie = Member("imgs", 96, NOW - timedelta(days=1), NOW - timedelta(hours=1))
    post = say(newbie, "@everyone look", NOW, attachments=_shots(4))
    holder = {}

    async def fetch(url, limit):
        if post.message_id in holder["h"].deleted():
            raise RuntimeError("404 Not Found")  # a deleted message's file
        return REPLAY_PNG

    case = ReplayCase(
        name="early delete with images",
        policy=acting(policy(ring=None)),
        messages=[post],
        answers={SCAM: (True, 0.95), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={newbie.id: FIRST}),
        fetch_image=fetch,
    )
    h = holder["h"] = harness(case)
    await h.judge(post)

    assert h.port.calls[0][0] == "delete"  # still removed first
    row = h.recorder.rows[0]
    assert row.classifier["images"]["sent"] == 4
    assert row.classifier["images"]["unseen"] == 0
    assert h.port.count("ban", newbie.id) == 1


async def test_an_original_and_its_escalated_copy_are_sanctioned_once():
    """Re-review item 2: an established member's 4-image post (unheld, one
    sampled image) and its burst copy (held, every image) both come back as
    violations while running side by side. The post would warn and the copy
    time out: one sanction for the post, the strongest (the copy's 24 h
    timeout), and both messages go."""
    member = Member("desk", 97, NOW - timedelta(days=900), NOW - timedelta(days=800))
    first = say(member, "my desk setup", NOW, attachments=_shots(4))
    copy = _copy_in(first, FOURTH)
    case = ReplayCase(
        name="original and escalated copy",
        policy=acting(policy(ring=None, spam=None)),
        messages=[first, copy],
        # The post's one-image check answers first, the copy's later: the
        # post's warn would land before the copy's timeout.
        haiku=_answers(post=(True, 0.05), copy=(True, 0.3)),
        reads=ReplayReads(histories={member.id: ESTABLISHED}),
    )
    h = harness(case)
    h.port.post(copy.channel_id, copy.message_id, copy.author_id, copy.content)
    h.shadow.submit(first)
    await asyncio.sleep(0.05)  # the post's check is out
    h.shadow.submit(copy)
    await h.shadow.drain()

    assert h.calls.total == 2  # the copy escalated: asked again
    sanctions = [
        row.decision["targets"][member.id]["sanction"] for row in h.recorder.rows
    ]
    assert [s["kind"] for s in sanctions if s] == [gw.TIMEOUT]
    assert not any("has been warned" in text for _, text in h.messenger.posts)
    untils = [c[2] for c in h.port.calls if c[0] == "timeout" and c[2] is not None]
    assert len([u for u in untils if u - time.time() > 3600]) == 1
    assert {first.message_id, copy.message_id} <= h.deleted()


def _answers(*, post, copy):
    """Haiku answering ``scam`` per message, ``(answer, seconds)``: the post
    and its copy (told apart by the copy's file URLs, :func:`_copy_in`).
    An :class:`asyncio.Event` in place of the seconds answers once it is set."""
    import json

    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.messages import TextPart
    from pydantic_ai.models.function import FunctionModel

    def build(answers, calls, by_text):
        async def respond(messages, info):
            content = messages[-1].parts[-1].content
            prompt = content if isinstance(content, str) else content[0]
            calls.haiku.append(prompt)
            scam, seconds = copy if "/1/copy/" in prompt else post
            if isinstance(seconds, asyncio.Event):
                await seconds.wait()
            else:
                await asyncio.sleep(seconds)
            body = {
                "participants": {},
                "scam": {"answer": scam, "confidence": 0.95},
                "abuse": {"answer": False, "confidence": 0.95},
            }
            return ModelResponse(parts=[TextPart(json.dumps(body))])

        return FunctionModel(respond, model_name="claude-haiku-5-5")

    return build


async def test_a_clean_original_does_not_lift_its_escalated_copys_hold():
    """Re-review item 4: a held post and its held burst copy share the hold's
    claim. The post's clean verdict released the claim while the copy was
    still being checked; only the last decision releases it now, and the
    copy's confirmed scam ends in its timeout with no lift before it."""
    newbie = Member("fresh", 98, NOW - timedelta(days=30), NOW - timedelta(hours=2))
    # A first message with four images: held (a high-risk author's images).
    first = say(newbie, "my setup", NOW, attachments=_shots(4))
    copy = _copy_in(first, FOURTH)
    case = ReplayCase(
        name="hold shared by a post and its copy",
        policy=acting(policy(ring=None, spam=None)),
        messages=[first, copy],
        haiku=_answers(post=(False, 0.05), copy=(True, 0.3)),
        reads=ReplayReads(histories={newbie.id: FIRST}),
    )
    h = harness(case)
    h.port.post(copy.channel_id, copy.message_id, copy.author_id, copy.content)
    h.shadow.submit(first)
    await asyncio.sleep(0.02)
    h.shadow.submit(copy)
    await h.shadow.drain()

    assert h.calls.total == 2
    lifted = [c for c in h.port.calls if c[0] == "timeout" and c[2] is None]
    assert lifted == []
    outcomes = {
        row.message_id: row.classifier["scam_outcome"] for row in h.recorder.rows
    }
    assert outcomes == {first.message_id: "clean", copy.message_id: "violation"}
    untils = [c[2] for c in h.port.calls if c[0] == "timeout"]
    assert untils[-1] - time.time() > 3600  # the copy's 24 h timeout stands
    assert copy.message_id in h.deleted()
    assert h.executor._holds == {}


async def test_backlogged_messages_never_wait_for_a_slot(monkeypatch):
    """Re-review item 3: a backlog message (past MAX_PENDING) still took a
    classifier slot after its early act, so a raid could fill MAX_BACKLOG
    waiting. With every slot taken, the backlog still finishes."""
    from smarter_dev.bot.moderation_coordinator import shadow as shadow_module

    monkeypatch.setattr(shadow_module, "MAX_PENDING", 1)
    blocker = Member("idle", 84, NOW - timedelta(days=400), NOW - timedelta(days=300))
    spammer = Member("raider", 85, NOW - timedelta(days=400), NOW - timedelta(days=300))
    stuck = say(blocker, "hello", NOW)
    case = ReplayCase(
        name="no slots", policy=acting(policy(ring=None)), messages=[stuck]
    )
    h = harness(case)
    h.port.add_member(spammer.id)
    h.shadow._slots = asyncio.Semaphore(0)  # every slot is taken
    waiting = h.shadow.submit(stuck)
    start = NOW + timedelta(seconds=30)
    backlog = []
    for i in range(12):
        facts = say(spammer, f"buy now {i}", start + timedelta(seconds=i * 0.3))
        h.port.post(facts.channel_id, facts.message_id, facts.author_id, facts.content)
        h.clock[0] = facts.posted_at
        h.case.reads.now = facts.posted_at
        backlog.append(h.shadow.submit(facts))
    try:
        await asyncio.wait_for(asyncio.gather(*backlog), 2)
    finally:
        waiting.cancel()
    assert h.shadow.backlogged == 12
    assert h.port.count("timeout", spammer.id) == 1
    assert len(h.recorder.rows) == 12


async def test_a_failed_decided_check_still_judges_the_message(caplog):
    """Re-review item 7: ``recorded()`` raising (the database is down)
    stopped all moderation. It counts as not decided, with a WARNING."""
    post = say(PINGER, "@everyone check this", NOW)
    case = ReplayCase(name="db down", policy=acting(policy(ring=None)), messages=[post])
    h = harness(case)

    async def down(*args):
        raise ConnectionRefusedError

    h.recorder.recorded = down
    with caplog.at_level("WARNING"):
        await h.judge(post)
    assert post.message_id in h.deleted()
    assert any("could not check whether" in r.getMessage() for r in caplog.records)


# -- the round-3 verification of a62a82be (#310) ------------------------------


async def test_an_original_still_sanctions_when_its_copy_fails_after_its_verdict():
    """Round 3, item 1: the post dropped its sanction once its escalated
    copy's check said yes, trusting the copy to sanction; a copy that failed
    after sharing its verdict (before claiming) left nobody sanctioned. The
    post always claims after waiting: the sanction is its own."""
    member = Member("desk", 99, NOW - timedelta(days=900), NOW - timedelta(days=800))
    first = say(member, "my desk setup", NOW, attachments=_shots(4))
    copy = _copy_in(first, FOURTH)
    copy_failed = asyncio.Event()
    case = ReplayCase(
        name="copy fails after its verdict",
        policy=acting(policy(ring=None, spam=None)),
        messages=[first, copy],
        # The copy answers first: the post's wait finds its verdict shared.
        haiku=_answers(post=(True, copy_failed), copy=(True, 0.01)),
        reads=ReplayReads(histories={member.id: ESTABLISHED}),
    )
    h = harness(case)
    coordinator = h.shadow.coordinator
    one_sanction = coordinator._one_sanction

    async def fail_on_copy(facts, *args):
        if facts.message_id == copy.message_id:
            copy_failed.set()
            raise RuntimeError("the copy failed before claiming")
        return await one_sanction(facts, *args)

    coordinator._one_sanction = fail_on_copy
    h.port.post(copy.channel_id, copy.message_id, copy.author_id, copy.content)
    h.shadow.submit(first)
    async with asyncio.timeout(5):
        while not h.calls.haiku:  # the post's check is out
            await asyncio.sleep(0.005)
    h.shadow.submit(copy)
    await h.shadow.drain()

    assert h.calls.total == 2  # the copy escalated: asked again
    rows = {row.message_id: row for row in h.recorder.rows}
    sanction = rows[first.message_id].decision["targets"][member.id]["sanction"]
    assert sanction is not None and sanction["kind"] == gw.WARN
    assert any("has been warned" in text for _, text in h.messenger.posts)


@pytest.mark.parametrize("placed", [True, False], ids=["post held", "post holding"])
async def test_a_copy_whose_hold_failed_does_not_lift_its_posts_hold(placed):
    """Round 3, item 2: the hold's pending count went up only when a
    containment succeeded but down on every decision, so a copy whose
    containment failed early counted as the last and lifted the post's hold
    while the post was still being checked (``placed``). And a copy decided
    while the post's hold was still being placed found no hold and released
    the claim (``placed=False``; the copy's placing fails at the gateway)."""
    newbie = Member("fresh", 100, NOW - timedelta(days=30), NOW - timedelta(hours=2))
    first = say(newbie, "my setup", NOW, attachments=_shots(4))
    copy = _copy_in(first, FOURTH)
    copy_done = asyncio.Event()
    case = ReplayCase(
        name="copy's hold failed",
        policy=acting(policy(ring=None, spam=None)),
        messages=[first, copy],
        # The copy's clean verdict is carried out first; the post's scam after.
        haiku=_answers(post=(True, copy_done), copy=(False, 0.01)),
        reads=ReplayReads(histories={newbie.id: FIRST}),
    )
    h = harness(case)
    execute = h.executor.execute

    async def note_the_copy(evaluation):
        try:
            return await execute(evaluation)
        finally:
            if evaluation.facts.message_id == copy.message_id:
                copy_done.set()

    h.executor.execute = note_the_copy
    trigger = h.executor._trigger

    def fail_on_copy(facts, **kwargs):
        if facts.message_id == copy.message_id and kwargs["reason"].startswith(
            "held while under review"
        ):
            raise RuntimeError("the copy's hold could not be placed")
        return trigger(facts, **kwargs)

    contain = h.executor.contain

    async def fail_early_on_copy(facts, *args, **kwargs):
        if facts.message_id == copy.message_id:
            return  # failed before the hold counted it (as a lost guild does)
        return await contain(facts, *args, **kwargs)

    gateway_for = h.executor._gateway_for

    class _Placing:
        """The post's timeout is on, but its hold is still being placed (the
        gateway has not answered) until the copy is decided."""

        def __init__(self, gateway):
            self._gateway = gateway

        def __getattr__(self, name):
            return getattr(self._gateway, name)

        async def act(self, trigger, proposals):
            result = await self._gateway.act(trigger, proposals)
            if any(proposal.provisional for proposal in proposals):
                await asyncio.wait_for(copy_done.wait(), 5)
            return result

    if placed:
        h.executor.contain = fail_early_on_copy
    else:
        h.executor._trigger = fail_on_copy
        h.executor._gateway_for = lambda policy: _Placing(gateway_for(policy))
    h.port.post(copy.channel_id, copy.message_id, copy.author_id, copy.content)
    h.shadow.submit(first)

    def held():
        if placed:
            hold = next(iter(h.executor._holds.values()), None)
            return hold is not None and hold.incident_id is not None
        return h.port.count("timeout", newbie.id) == 1

    async with asyncio.timeout(5):
        while not held():  # the post's hold is placed, or its timeout is on
            await asyncio.sleep(0.005)
    h.shadow.submit(copy)
    await h.shadow.drain()

    assert h.calls.total == 2
    outcomes = {
        row.message_id: row.classifier["scam_outcome"] for row in h.recorder.rows
    }
    assert outcomes == {first.message_id: "violation", copy.message_id: "clean"}
    lifted = [c for c in h.port.calls if c[0] == "timeout" and c[2] is None]
    assert lifted == []
    untils = [c[2] for c in h.port.calls if c[0] == "timeout"]
    assert untils[-1] - time.time() > 3600  # the post's 24 h timeout
    assert first.message_id in h.deleted()
    assert h.executor._holds == {}
