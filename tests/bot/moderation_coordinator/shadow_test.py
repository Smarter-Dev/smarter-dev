"""The coordinator in shadow: it records, compares, and never touches Discord."""

from __future__ import annotations

import time
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from smarter_dev.bot.moderation_coordinator import shadow as shadow_module
from smarter_dev.bot.moderation_coordinator.classifier import ClassifierBudget
from smarter_dev.bot.moderation_coordinator.classifier import ModerationClassifier
from smarter_dev.bot.moderation_coordinator.coordinator import Coordinator
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import EVENT_PROMOTION
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.policy import SCAM_BANNER_HANDLER
from smarter_dev.bot.moderation_coordinator.policy import PolicyCache
from smarter_dev.bot.moderation_coordinator.policy import load_policy
from smarter_dev.bot.moderation_coordinator.replay import ModelCalls
from smarter_dev.bot.moderation_coordinator.replay import scripted_haiku
from smarter_dev.bot.moderation_coordinator.replay import scripted_jev
from smarter_dev.bot.moderation_coordinator.shadow import BotReads
from smarter_dev.bot.moderation_coordinator.shadow import LegacyComparer
from smarter_dev.bot.moderation_coordinator.shadow import ModerationShadow
from smarter_dev.bot.moderation_coordinator.shadow import author_history_from
from smarter_dev.bot.moderation_coordinator.shadow import revision_of
from smarter_dev.bot.moderation_coordinator.shadow import shadow_guild_ids
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.models import ModerationShadowDecision
from smarter_dev.web.moderation_gateway import ModerationGateway
from smarter_dev.web.moderation_gateway import Proposal
from smarter_dev.web.moderation_gateway import Trigger
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.gateway import default_session_factory
from smarter_dev.web.moderation_gateway.privacy import delete_member_moderation_content
from smarter_dev.web.retention import prune_moderation_shadow_decisions
from tests.bot.fake_discord import GUILD_ID
from tests.bot.fake_discord import FakeDiscord
from tests.bot.moderation_coordinator.replay_cases import SCAM_TEXT
from tests.bot.moderation_coordinator.replay_cases import policy
from tests.bot.moderation_coordinator.replay_cases import snowflake_at
from tests.moderation_fakes import FakeModerationDiscord

GUILD = str(GUILD_ID)
CHANNEL = 702178352395580001


def sessions():
    return default_session_factory()()


async def rows() -> list[ModerationShadowDecision]:
    async with sessions() as session:
        return list(
            (
                await session.execute(
                    select(ModerationShadowDecision).order_by(
                        ModerationShadowDecision.created_at
                    )
                )
            ).scalars()
        )


def _scammer(discord: FakeDiscord, *, age_days: float = 2):
    now = datetime.now(UTC)
    user_id = int(snowflake_at(now - timedelta(days=age_days), 7))
    member = discord.add_member(
        user_id, username="nitro_drop", joined_at=now - timedelta(hours=1)
    )
    return user_id, member


def _event(discord: FakeDiscord, user_id: int, content: str, *, message_id=None):
    message = discord.add_message(
        message_id or int(snowflake_at(datetime.now(UTC), len(discord.messages))),
        user_id,
        content=content,
    )
    message.attachments = []
    return SimpleNamespace(
        guild_id=GUILD_ID,
        channel_id=CHANNEL,
        message=message,
        member=discord.members.get(user_id),
        is_human=True,
    )


def _shadow(discord, *, answers, calls, policies=None) -> ModerationShadow:
    classifier = ModerationClassifier(
        budget=ClassifierBudget(per_guild_day=100),
        haiku_model=scripted_haiku(answers, calls),
        jev_model=scripted_jev(answers, calls),
    )
    return ModerationShadow(
        Coordinator(BotReads(discord), classifier),
        guild_ids=frozenset({GUILD}),
        policies=policies or PolicyCache(loader=_policy_of),
    )


async def _policy_of(guild_id):
    return policy(guild_id=guild_id)


# -- it cannot act -------------------------------------------------------------


async def test_the_shadow_judges_the_1749_scam_without_touching_discord():
    discord = FakeDiscord()
    user_id, _ = _scammer(discord)
    calls = ModelCalls()
    shadow = _shadow(
        discord,
        answers={
            SCAM: (True, 0.97),
            ABUSE: (False, 0.9),
            EVENT_PROMOTION: (False, 0.9),
        },
        calls=calls,
    )

    await shadow.on_create(_event(discord, user_id, SCAM_TEXT))
    await shadow.drain()

    # It would have banned; it did nothing at all on Discord.
    assert discord.destructive == []
    assert discord.sent == []
    assert discord.edited == []
    # The only Discord traffic is reads: the ring's history read, member lookups.
    assert set(discord.reads) <= {
        "fetch_messages",
        "fetch_member",
        "fetch_guild",
        "fetch_roles",
        "fetch_guild_channels",
        "fetch_user",
    }
    [row] = await rows()
    assert row.strongest_action == gw.BAN
    assert row.target_ids == [str(user_id)]
    assert row.classifier["backend"] == "haiku"
    assert row.classifier["requests"] == 1
    assert row.labels["scam"]["outcome"] == "answered"
    assert row.provisional["resolution"] == "upgraded"
    assert row.legacy_checked_at is None
    assert calls.total == 1


async def test_a_row_never_holds_the_message_text():
    discord = FakeDiscord()
    user_id, _ = _scammer(discord)
    shadow = _shadow(discord, answers={SCAM: (True, 0.97)}, calls=ModelCalls())
    await shadow.on_create(_event(discord, user_id, SCAM_TEXT))
    await shadow.drain()

    [row] = await rows()
    stored = " ".join(
        str(getattr(row, column.name))
        for column in ModerationShadowDecision.__table__.c
    )
    assert "dlscord" not in stored
    assert "nitro giveaway" not in stored


async def test_other_guilds_bots_and_unchanged_edits_are_not_judged():
    discord = FakeDiscord()
    user_id, _ = _scammer(discord)
    shadow = _shadow(discord, answers={}, calls=ModelCalls())

    elsewhere = _event(discord, user_id, "hello")
    elsewhere.guild_id = 1
    await shadow.on_create(elsewhere)
    bot = _event(discord, user_id, "beep")
    bot.is_human = False
    await shadow.on_create(bot)
    unfurl = _event(discord, user_id, "see https://example.com")
    unfurl.old_message = SimpleNamespace(content="see https://example.com")
    await shadow.on_update(unfurl)
    await shadow.drain()

    assert await rows() == []


async def test_an_edit_is_its_own_revision():
    discord = FakeDiscord()
    user_id, _ = _scammer(discord, age_days=900)
    shadow = _shadow(discord, answers={}, calls=ModelCalls())
    event = _event(discord, user_id, "hello")
    await shadow.on_create(event)
    edit = _event(discord, user_id, "hello @everyone", message_id=event.message.id)
    edit.old_message = SimpleNamespace(content="hello")
    edit.message.edited_timestamp = datetime(2026, 10, 19, 12, 0, 1, tzinfo=UTC)
    await shadow.on_update(edit)
    await shadow.drain()

    by_revision = {row.revision: row for row in await rows()}
    created = by_revision.pop("create")
    [edited] = by_revision.values()
    assert created.strongest_action is None
    # The edit's time, never anything derived from its text (#255).
    assert edited.revision == "edit:1792411201000"
    assert edited.revision == revision_of(
        edit.message.edited_timestamp, is_edit=True, now=0
    )
    assert edited.is_edit
    assert edited.strongest_action == gw.DELETE  # edit-ping's cleanup


async def test_a_policy_read_failure_is_recorded_not_guessed():
    discord = FakeDiscord()
    user_id, _ = _scammer(discord)

    async def broken(guild_id):
        raise RuntimeError("database down")

    shadow = _shadow(
        discord, answers={}, calls=ModelCalls(), policies=PolicyCache(loader=broken)
    )
    await shadow.on_create(_event(discord, user_id, SCAM_TEXT))
    await shadow.drain()

    [row] = await rows()
    assert row.error == "RuntimeError"
    assert row.strongest_action is None


async def test_a_full_queue_drops_and_counts(monkeypatch):
    monkeypatch.setattr(shadow_module, "MAX_PENDING", 0)
    discord = FakeDiscord()
    user_id, _ = _scammer(discord)
    shadow = _shadow(discord, answers={}, calls=ModelCalls())
    await shadow.on_create(_event(discord, user_id, "hi"))
    assert shadow.dropped == 1
    assert await rows() == []


def test_shadow_guilds_come_from_the_environment(monkeypatch):
    monkeypatch.setenv(shadow_module.GUILDS_ENV_VAR, " 644299523686006834 , 1,")
    assert shadow_guild_ids() == frozenset({"644299523686006834", "1"})
    monkeypatch.setenv(shadow_module.GUILDS_ENV_VAR, "")
    assert shadow_guild_ids() == frozenset()


async def test_the_registered_shadow_reads_the_day_back_once_it_acts(monkeypatch):
    """The bot's shadow starts with its budget shut; the started listener
    (run once this process acts) reads the day back and keeps its loop."""
    import asyncio

    class Bot:
        listeners: list = []

        def listen(self):
            def register(callback):
                self.listeners.append(callback)
                return callback

            return register

    monkeypatch.setenv(shadow_module.GUILDS_ENV_VAR, GUILD)
    await _shadow_row("1", "42", None, [], classifier={"requests": 1})
    bot = Bot()
    shadow = shadow_module.register_moderation_shadow(bot)
    budget = shadow.coordinator.classifier.budget
    assert not budget.restored

    started = next(
        cb for cb in bot.listeners if cb.__name__ == "on_moderation_shadow_started"
    )
    await started(SimpleNamespace())
    for _ in range(100):
        if budget.restored:
            break
        await asyncio.sleep(0.01)
    try:
        assert budget.restored
        assert budget.spent_today(GUILD, time.time()) == 1
        assert shadow.background is not None and not shadow.background.done()
    finally:
        shadow.background.cancel()


# -- the legacy side -----------------------------------------------------------


async def _shadow_row(
    message_id: str, author: str, strongest, targets, *, at=None, **extra
):
    row = ModerationShadowDecision(
        **extra,
        guild_id=GUILD,
        channel_id=str(CHANNEL),
        message_id=message_id,
        revision="create",
        is_edit=False,
        author_id=author,
        message_at=at or datetime.now(UTC),
        strongest_action=strongest,
        target_ids=targets,
    )
    async with sessions() as session:
        session.add(row)
        await session.commit()


async def test_the_comparison_reads_what_the_legacy_actors_did():
    """The spam engine muted and deleted; the shadow would have banned."""
    port = FakeModerationDiscord(guild_id=GUILD)
    port.add_member("42")
    port.post(str(CHANNEL), "777", "42", SCAM_TEXT)
    gateway = ModerationGateway(port)
    await gateway.act(
        Trigger(
            guild_id=GUILD,
            actor="spam_engine",
            reason="posting scam links",
            channel_id=str(CHANNEL),
            message_id="777",
            author_id="42",
            content=SCAM_TEXT,
        ),
        [
            Proposal(kind=gw.TIMEOUT, target_user_id="42", duration_seconds=86400),
            Proposal(
                kind=gw.DELETE,
                target_user_id="42",
                channel_id=str(CHANNEL),
                message_ids=("777",),
            ),
        ],
    )
    await _shadow_row("777", "42", gw.BAN, ["42"])
    await _shadow_row("778", "43", None, [])
    await _shadow_row("779", "44", gw.WARN, ["44"])

    # Not yet: the legacy side is read once it has had time to settle.
    assert await LegacyComparer().sweep() == 0
    later = LegacyComparer(clock=lambda: time.time() + 300)
    assert await later.sweep() == 3

    by_message = {row.message_id: row for row in await rows()}
    scam = by_message["777"]
    assert scam.legacy_strongest_action == gw.TIMEOUT
    assert scam.legacy["actors"] == ["spam_engine"]
    assert scam.legacy["message_deleted"] is True
    assert scam.agreement == "shadow_stronger"
    assert by_message["778"].agreement == "both_none"
    assert by_message["779"].agreement == "shadow_stronger"
    assert await later.sweep() == 0  # each row is compared once


async def test_each_revision_gets_only_the_legacy_effects_it_earned():
    """The gateway folds an author's copies into one incident: the mute the
    later copy earned is not the earlier copy's."""
    port = FakeModerationDiscord(guild_id=GUILD)
    port.add_member("42")
    gateway = ModerationGateway(port)
    text = "buy followers cheap at my shop, best prices here"
    start = datetime.now(UTC)

    def trigger(message_id):
        return Trigger(
            guild_id=GUILD,
            actor="spam_engine",
            reason="repeating the same message",
            channel_id=str(CHANNEL),
            message_id=message_id,
            author_id="42",
            content=text,
        )

    await gateway.act(trigger("801"), [])  # the first copy: nothing yet
    await _shadow_row("801", "42", None, [], at=start - timedelta(seconds=2))
    await gateway.act(
        trigger("802"),
        [Proposal(kind=gw.TIMEOUT, target_user_id="42", duration_seconds=86400)],
    )
    await _shadow_row("802", "42", gw.TIMEOUT, ["42"], at=start)

    await LegacyComparer(clock=lambda: time.time() + 300).sweep()
    by_message = {row.message_id: row for row in await rows()}
    assert by_message["801"].legacy_strongest_action is None
    assert by_message["801"].agreement == "both_none"
    assert by_message["802"].legacy_strongest_action == gw.TIMEOUT
    assert by_message["802"].agreement == "agree"


async def test_a_burst_gets_each_legacy_action_on_the_message_it_answered():
    """The 14:02 Oct 22 burst: one author, 2 s apart across channels. The
    spam engine warned on the 14:02:12 post, the bump tracker deleted the
    14:02:17 post, and the spam engine timed out on the 14:02:19 post. The
    warn came 4 s before the bump post, and the delete after the next post:
    each still belongs to the post it answered."""
    from smarter_dev.web.models import ModerationOperation

    port = FakeModerationDiscord(guild_id=GUILD)
    port.add_member("42")
    gateway = ModerationGateway(port)
    base = datetime.now(UTC) - timedelta(seconds=60)
    warned, bumped, muted = (
        snowflake_at(base + timedelta(seconds=offset)) for offset in (0, 5, 7)
    )
    bump_channel = str(CHANNEL + 1)
    port.post(bump_channel, bumped, "42", "check out my server")

    def trigger(actor, message_id, channel, related=()):
        return Trigger(
            guild_id=GUILD,
            actor=actor,
            reason="posting in so many channels",
            channel_id=channel,
            message_id=message_id,
            author_id="42",
            related_message_ids=related,
        )

    await gateway.act(
        trigger("spam_engine", warned, str(CHANNEL)),
        [Proposal(kind=gw.WARN, target_user_id="42")],
    )
    await gateway.act(
        trigger("handler:disboard-bump-tracker", bumped, bump_channel, (warned,)),
        [
            Proposal(
                kind=gw.DELETE,
                target_user_id="42",
                channel_id=bump_channel,
                message_ids=(bumped,),
            )
        ],
    )
    await gateway.act(
        trigger("spam_engine", muted, str(CHANNEL + 2), (warned, bumped)),
        [Proposal(kind=gw.TIMEOUT, target_user_id="42", duration_seconds=86400)],
    )
    made = {gw.WARN: 1.07, gw.DELETE: 9.95, gw.TIMEOUT: 11.25}
    async with sessions() as session:
        operations = list(
            (await session.execute(select(ModerationOperation))).scalars()
        )
        assert {op.kind for op in operations} == set(made)
        assert len({op.incident_id for op in operations}) == 1
        for op in operations:
            op.created_at = base + timedelta(seconds=made[op.kind])
        await session.commit()
    await _shadow_row(warned, "42", gw.WARN, ["42"], at=base)
    await _shadow_row(bumped, "42", gw.DELETE, ["42"], at=base + timedelta(seconds=5))
    await _shadow_row(muted, "42", gw.TIMEOUT, ["42"], at=base + timedelta(seconds=7))

    await LegacyComparer(clock=lambda: time.time() + 300).sweep()
    by_message = {row.message_id: row for row in await rows()}
    assert by_message[warned].legacy_strongest_action == gw.WARN
    assert by_message[bumped].legacy_strongest_action == gw.DELETE
    assert by_message[bumped].legacy["message_deleted"] is True
    assert by_message[muted].legacy_strongest_action == gw.TIMEOUT
    assert by_message[muted].legacy["targets"]["42"]["deleted"] is False
    assert {row.agreement for row in by_message.values()} == {"agree"}


def test_an_operation_answers_one_revision():
    from smarter_dev.bot.moderation_coordinator.shadow import answered_revision

    base = datetime(2026, 10, 22, 14, 2, 12, tzinfo=UTC)
    first, second, third = (
        snowflake_at(base + timedelta(seconds=offset)) for offset in (0, 5, 7)
    )
    revisions = [
        ("r1", first, base),
        ("r2", second, base + timedelta(seconds=5)),
        ("r2-edit", second, base + timedelta(seconds=30)),
        ("r3", third, base + timedelta(seconds=7)),
    ]
    revisions.sort(key=lambda item: item[2])

    def op(after: float, message_ids=None):
        return SimpleNamespace(
            created_at=base + timedelta(seconds=after), message_ids=message_ids
        )

    # Named messages: the newest judged one, whenever the operation came.
    assert answered_revision(op(60, [first, third, "1"]), revisions) == "r3"
    assert answered_revision(op(-30, [second]), revisions) == "r2"
    assert answered_revision(op(40, [second]), revisions) == "r2-edit"
    # Nothing named, or nothing judged named: the latest revision before it.
    assert answered_revision(op(1.07), revisions) == "r1"
    assert answered_revision(op(4.5, ["1"]), revisions) == "r2"
    assert answered_revision(op(-2), revisions) is None


def test_a_repeat_hold_is_a_sanction_and_a_review_hold_is_not():
    from smarter_dev.bot.moderation_coordinator.shadow import summarize_legacy
    from smarter_dev.web.models import ModerationOperation

    def hold(seconds):
        return ModerationOperation(
            target_user_id="42",
            kind=gw.TIMEOUT,
            state=gw.CONFIRMED,
            actor="handler:scam-banner",
            requested={"duration_seconds": seconds, "provisional": True},
        )

    review = summarize_legacy([hold(300)], "1")
    assert review["strongest"] is None
    assert review["targets"]["42"]["provisional"] is True
    repeat = summarize_legacy([hold(86400)], "1")
    assert repeat["strongest"] == gw.TIMEOUT


async def test_the_comparison_ignores_moderators_and_other_messages():
    port = FakeModerationDiscord(guild_id=GUILD)
    port.add_member("42")
    gateway = ModerationGateway(port)
    await gateway.act(
        Trigger(
            guild_id=GUILD,
            actor="manual",
            source=gw.SOURCE_MANUAL,
            reason="mod",
            message_id="900",
            author_id="42",
        ),
        [Proposal(kind=gw.TIMEOUT, target_user_id="42", duration_seconds=600)],
    )
    await _shadow_row("900", "42", None, [])
    await LegacyComparer(clock=lambda: time.time() + 300).sweep()
    [row] = await rows()
    assert row.legacy_strongest_action is None
    assert row.agreement == "both_none"


def test_author_history_before_this_message():
    before = datetime(2026, 10, 18, 17, 49, tzinfo=UTC)
    assert author_history_from(None, [], before).is_first_message is True
    old = (before - timedelta(days=40), before - timedelta(days=31))
    history = author_history_from(old, [], before)
    assert (history.is_first_message, history.days_since_last_message) == (False, 31)
    # The dispatch already recorded this very message: its first one.
    same = (before, before)
    history = author_history_from(same, [], before)
    assert (history.is_first_message, history.days_since_last_message) == (True, None)
    # Recorded, but not the first: how long they were silent is not known.
    history = author_history_from((before - timedelta(days=3), before), [], before)
    assert (history.is_first_message, history.days_since_last_message) == (False, None)


# -- the policy it reads ------------------------------------------------------


async def test_policy_follows_the_legacy_actors_switches_and_the_live_script(tmp_path):
    from pathlib import Path

    reference = (
        Path(__file__).resolve().parents[3]
        / "scripts/handler_scripts/scam-banner.monty"
    ).read_text()
    async with sessions() as session:
        session.add(ModerationFilterConfig(guild_id=GUILD, spam_engine_enabled=True))
        for name, script, enabled, channels in (
            (SCAM_BANNER_HANDLER, reference, True, []),
            ("edit-ping-catch", "pass\n", False, []),
            ("disboard-bump-tracker", "pass\n", True, ["555"]),
        ):
            session.add(
                AdminHandler(
                    guild_id=GUILD,
                    name=name,
                    trigger_type="message",
                    settings={},
                    channel_ids=channels,
                    description="d",
                    script=script,
                    created_by_admin="A",
                    enabled=enabled,
                )
            )
        await session.commit()

    loaded = await load_policy(GUILD)
    assert loaded.spam is not None and loaded.ring is not None
    assert loaded.attachments is None  # no attachment config row: off
    assert loaded.scam_gate and loaded.scam_banner_matches_reference is True
    assert loaded.edit_ping is False
    assert loaded.bump_channel_ids == frozenset({"555"})

    async with sessions() as session:
        handler = (
            await session.execute(
                select(AdminHandler).where(AdminHandler.name == SCAM_BANNER_HANDLER)
            )
        ).scalar_one()
        handler.script = reference + "\n# drifted\n"
        await session.commit()
    assert (await load_policy(GUILD)).scam_banner_matches_reference is False


async def test_a_guild_with_nothing_configured_runs_only_the_ring():
    loaded = await load_policy("1")
    assert loaded.spam is None and loaded.attachments is None
    assert loaded.ring is not None  # the ring's own default: on
    assert not loaded.scam_gate and not loaded.edit_ping
    assert loaded.bump_channel_ids == frozenset()


# -- retention and privacy ----------------------------------------------------


def test_the_hourly_sweep_prunes_shadow_rows():
    from smarter_dev.web.retention import SCRUBBERS

    assert SCRUBBERS["moderation_shadow_decisions"] is prune_moderation_shadow_decisions


async def test_rows_are_deleted_after_thirty_days():
    await _shadow_row("1", "42", None, [])
    async with sessions() as session:
        now = datetime.now(UTC)
        assert await prune_moderation_shadow_decisions(session, now, now) == 0
        later = now + timedelta(days=31)
        assert await prune_moderation_shadow_decisions(session, later, later) == 1
        await session.commit()
    assert await rows() == []


async def test_the_days_spend_is_read_back_from_the_rows():
    """#255: the daily cap survives a restart; today's rows are counted, not
    yesterday's or another guild's. A failed read opens on a fresh count."""
    from smarter_dev.bot.moderation_coordinator.shadow import ShadowRecorder
    from smarter_dev.bot.moderation_coordinator.shadow import restore_budget

    now = time.time()
    today = datetime.fromtimestamp(now - now % 86400, UTC)
    await _shadow_row("1", "42", None, [], classifier={"requests": 1})
    await _shadow_row("2", "43", None, [], classifier={"requests": 1})
    await _shadow_row("3", "44", None, [], classifier={"requests": 0})
    await _shadow_row("4", "45", None, [], classifier=None)
    await _shadow_row(
        "5",
        "46",
        None,
        [],
        classifier={"requests": 1},
        created_at=today - timedelta(seconds=1),
    )
    async with sessions() as session:
        session.add(
            ModerationShadowDecision(
                guild_id="999",
                channel_id="1",
                message_id="6",
                revision="create",
                is_edit=False,
                author_id="47",
                message_at=datetime.now(UTC),
                classifier={"requests": 1},
            )
        )
        await session.commit()

    budget = ClassifierBudget(per_guild_day=3, restored=False)
    await restore_budget(budget, ShadowRecorder(), [GUILD])
    assert budget.restored
    assert budget.spent_today(GUILD, time.time()) == 2
    assert budget.try_spend(GUILD, "A", time.time()) is None
    assert budget.try_spend(GUILD, "B", time.time()) == "guild_day"

    class Broken(ShadowRecorder):
        async def requests_today(self, guild_ids, now):
            raise ConnectionError("db down")

    # #310 item 5: a failed read opens the day on a fresh count (shut, every
    # scam check in an acting guild would be unknown), and the next read is
    # merged in.
    fresh = ClassifierBudget(per_guild_day=3, restored=False)
    await restore_budget(fresh, Broken(), [GUILD])
    assert fresh.restored and fresh.restore_pending
    assert fresh.try_spend(GUILD, "A", time.time()) is None
    await restore_budget(fresh, ShadowRecorder(), [GUILD])
    assert not fresh.restore_pending
    assert fresh.spent_today(GUILD, time.time()) == 2  # the larger count wins


async def test_a_deletion_request_removes_the_persons_shadow_rows():
    await _shadow_row("1", "42", gw.BAN, ["42"])
    await _shadow_row("2", "50", gw.TIMEOUT, ["50", "42"])
    await _shadow_row("3", "50", gw.TIMEOUT, ["50", "4200"])
    # Only asked about, never acted on: named in a label and a review line.
    await _shadow_row(
        "4",
        "50",
        None,
        [],
        labels={"participant:42": {"outcome": "low_confidence"}},
        decision={
            "targets": {},
            "review": ["Ring participant check unknown for <@42>"],
        },
    )
    async with sessions() as session:
        deleted = await delete_member_moderation_content(session, "42")
        await session.commit()
    assert deleted.shadow == 3
    assert [row.message_id for row in await rows()] == ["3"]


async def test_a_raid_does_not_look_the_same_authors_up_per_message():
    discord = FakeDiscord()
    user_id, _ = _scammer(discord)
    _event(discord, user_id, "hi")
    reads = BotReads(discord)
    since = datetime.now(UTC) - timedelta(hours=1)
    await reads.channel_window(GUILD, str(CHANNEL), since, 100)
    before = discord.reads.count("fetch_member")
    discord.cached = False  # make a second lookup go to REST
    await reads.channel_window(GUILD, str(CHANNEL), since, 100)
    assert discord.reads.count("fetch_member") == before
