"""The cutover (#240): the coordinator's decision, carried out.

Each case runs live-shaped messages through :class:`ModerationShadow` with an
:class:`Executor` over the real moderation gateway and a fake Discord port,
for a guild the coordinator owns. What Discord saw is read back from the port;
what moderators saw, from the incident's entries.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import select

from smarter_dev.bot.moderation_coordinator import act as act_module
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
        case_policy, owner=COORDINATOR, generation=generation, report_channel_id=REPORTS
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


# -- the 17:49 scam, acted on ------------------------------------------------


async def test_the_1749_scam_holds_first_then_bans_and_removes_the_post():
    case, scammer = scam_1749(account_age_days=2)
    case = replace(case, policy=acting(case.policy))
    h = harness(case)
    post = case.messages[0]

    await h.judge(post)

    kinds = [call[0] for call in h.port.calls]
    # Contained before the verdict: the 5-minute hold, then the ban.
    assert kinds.index("timeout") < kinds.index("ban")
    assert ("ban", scammer.id) == h.port.calls[kinds.index("ban")][:2]
    assert post.message_id in h.deleted()
    assert h.messenger.posts[0] == (post.channel_id, INVESTIGATING)
    assert h.messenger.posts[-1] == (post.channel_id, CLOSED_REMOVED)
    notes = await entries(gw.ENTRY_NOTE)
    assert any("upgraded to a ban" in note for note in notes)
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
    case, scammer = scam_1749(account_age_days=30)
    case = replace(case, policy=acting(case.policy))
    h = harness(case)

    async def broken(request):
        raise RuntimeError("model down")

    monkeypatch.setattr(h.shadow.coordinator.classifier, "classify", broken)
    await h.judge(case.messages[0])

    assert [call[0] for call in h.port.calls] == ["timeout"]  # the hold only
    alerts = await entries(gw.ENTRY_ALERT)
    assert any("failed after the hold" in a and "RuntimeError" in a for a in alerts)
    assert h.messenger.posts[-1] == (case.messages[0].channel_id, CLOSED_UNKNOWN)
    assert h.recorder.rows[0].error == "RuntimeError"


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
    await asyncio.sleep(0.01)
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
    await h.judge(case.messages[0])

    assert executed == [case.messages[0].message_id]


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
    assert h.port.posts == []  # not reported, as the handler never was


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
