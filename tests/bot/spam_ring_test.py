"""Tests for the bot-core spam ring detector (``smarter_dev/bot/spam_ring.py``).

The pure layer is tested directly. The shell is driven end to end through
``check_spam_ring`` against a fake REST client and a fake DB session that log
every side effect in one ordered list, so "saved before deleted" is a check on
that order rather than on mocks being called. Both model calls are replaced:
the classifier and the judge are injected by monkeypatching the module.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import hikari
import pytest

from smarter_dev.bot import spam_ring
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.models import SpamRingIncident

GUILD_ID = 100
CHANNEL_ID = 200
OTHER_CHANNEL_ID = 201
REPORT_CHANNEL_ID = "900"
RESPONSE_CHANNEL_ID = "901"
STAFF_ROLE_ID = 500

NOW_DT = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
NOW = NOW_DT.timestamp()
DISCORD_EPOCH_MS = 1420070400000


def snowflake_at(moment: datetime, salt: int = 0) -> int:
    """A snowflake whose embedded timestamp is ``moment``."""
    return ((int(moment.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22) + salt


def _user(name: str, *, created: datetime, salt: int, is_bot: bool = False):
    return SimpleNamespace(id=snowflake_at(created, salt), username=name, is_bot=is_bot)


# Three ring accounts made two days ago, joined yesterday within minutes.
RING_JOIN = NOW_DT - timedelta(days=1)
ALICE = _user("alice_q", created=NOW_DT - timedelta(days=2), salt=1)
BOB = _user("bob_q", created=NOW_DT - timedelta(days=2), salt=2)
CAROL = _user("carol_ad", created=NOW_DT - timedelta(days=2), salt=3)
# An established member and a member of staff who happen to be chatting.
ERIN = _user("erin_regular", created=NOW_DT - timedelta(days=900), salt=4)
STAFF = _user("mod_sam", created=NOW_DT - timedelta(days=900), salt=5)
# A genuine newcomer with an unrelated question.
NED = _user("ned_newbie", created=NOW_DT - timedelta(days=300), salt=6)

AD_TEXT = (
    "You should totally check out HackNova 2026 at Lakeshore Polytechnic University! "
    "$10k prize pool, register at https://devpost.example/hacknova before Friday, "
    "you can team up there"
)


def _member(user, *, joined_at: datetime | None, role_ids=()):
    return SimpleNamespace(
        id=user.id, role_ids=list(role_ids), joined_at=joined_at, user=user
    )


class _History:
    def __init__(self, messages):
        self._messages = messages

    def limit(self, count):
        messages = list(reversed(self._messages))[:count]  # newest first, as Discord

        async def iterate():
            for message in messages:
                yield message

        return iterate()


class _FakeRest:
    def __init__(self, log: list) -> None:
        self.log = log
        self.channel_messages: dict[int, list] = {}
        self.members: dict[int, SimpleNamespace] = {}
        self.created: list[dict] = []
        self.edited: list[dict] = []
        self.timeouts: list[dict] = []
        self.deleted: list[int] = []
        self.edit_member_raises: Exception | None = None
        self._next_id = 10_000

    def fetch_messages(self, channel_id):
        return _History(self.channel_messages.get(int(channel_id), []))

    async def fetch_member(self, guild_id, user_id):
        member = self.members.get(int(user_id))
        if member is None:
            raise hikari.NotFoundError(
                url="url", headers={}, raw_body=b"", message="gone"
            )
        return member

    async def edit_member(self, guild_id, user_id, **kwargs):
        if self.edit_member_raises is not None:
            raise self.edit_member_raises
        self.log.append(("timeout", str(user_id)))
        self.timeouts.append({"user": str(user_id), **kwargs})

    async def delete_message(self, channel_id, message_id):
        self.log.append(("delete", (int(message_id),)))
        self.deleted.append(int(message_id))

    async def delete_messages(self, channel_id, message_ids):
        self.log.append(("delete", tuple(int(m) for m in message_ids)))
        self.deleted.extend(int(m) for m in message_ids)

    async def create_message(self, channel, content, **kwargs):
        self.log.append(("report", str(channel)))
        self._next_id += 1
        self.created.append({"channel": str(channel), "content": content, **kwargs})
        return SimpleNamespace(id=self._next_id)

    async def edit_message(self, channel, message, content, **kwargs):
        self.log.append(("edit_report", str(message)))
        self.edited.append(
            {
                "channel": str(channel),
                "message": str(message),
                "content": content,
                **kwargs,
            }
        )


class _FakeGuild:
    def __init__(self) -> None:
        self.owner_id = 0

    def get_role(self, role_id):
        if int(role_id) == STAFF_ROLE_ID:
            return SimpleNamespace(
                id=role_id, permissions=hikari.Permissions.MANAGE_MESSAGES
            )
        return SimpleNamespace(id=role_id, permissions=hikari.Permissions.NONE)


class _FakeCache:
    def __init__(self, rest: _FakeRest) -> None:
        self.rest = rest

    def get_member(self, guild_id, user_id):
        return self.rest.members.get(int(user_id))


class _FakeBot:
    def __init__(self, rest: _FakeRest) -> None:
        self.rest = rest
        self.cache = _FakeCache(rest)

    def get_me(self):
        return SimpleNamespace(id=1, username="smarter-dev")


class _FakeResult:
    def __init__(self, row) -> None:
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class Harness:
    """Fake bot, DB and models, sharing one ordered side-effect log."""

    def __init__(self, monkeypatch) -> None:
        self.log: list = []
        self.rest = _FakeRest(self.log)
        self.bot = _FakeBot(self.rest)
        self.guild = _FakeGuild()
        self.registry = spam_ring.SpamRingRegistry()
        self.config: ModerationFilterConfig | None = ModerationFilterConfig(
            guild_id=str(GUILD_ID),
            staff_exempt_role_ids=[str(STAFF_ROLE_ID)],
            spam_ring_report_channel_id=REPORT_CHANNEL_ID,
        )
        self.moderation_config = None
        self.incidents: list[SpamRingIncident] = []
        self.added: list = []
        self.actions: list[dict] = []
        self.dispatched: list = []
        self.events: list = []
        self.classified: list[str] = []
        self.promotion_verdict = True
        self.exclusions: set[str] = set()
        self.judge_inputs: list = []
        self.save_fails = False
        self.classifier_gate: asyncio.Event | None = None

        harness = self

        class FakeSession:
            def add(self, row) -> None:
                harness.added.append(row)
                if isinstance(row, SpamRingIncident):
                    if harness.save_fails:
                        raise RuntimeError("db down")
                    row.id = row.id or uuid4()
                    harness.incidents.append(row)

            async def commit(self) -> None:
                if harness.incidents:
                    harness.log.append(
                        ("commit", len(harness.incidents[-1].transcript))
                    )

            async def execute(self, statement):
                return _FakeResult(harness.incidents[-1] if harness.incidents else None)

        @contextlib.asynccontextmanager
        async def fake_session_context():
            yield FakeSession()

        async def get_config(session, guild_id):
            return harness.config

        async def get_moderation_config(session, guild_id):
            return harness.moderation_config

        async def create_action(session, **kwargs):
            harness.log.append(("action", kwargs["target_user_id"]))
            harness.actions.append(kwargs)
            return SimpleNamespace(**kwargs)

        async def dispatch(action, *, bot=None):
            harness.dispatched.append((action, bot))

        async def record_event(bot, event, **kwargs):
            harness.events.append(event)

        async def classify(content):
            harness.classified.append(content)
            if harness.classifier_gate is not None:
                await harness.classifier_gate.wait()
            if isinstance(harness.promotion_verdict, Exception):
                raise harness.promotion_verdict
            return harness.promotion_verdict

        async def judge(transcript, candidates, ad_author_id, now):
            harness.judge_inputs.append(
                (transcript, [c.user_id for c in candidates], ad_author_id)
            )
            return set(harness.exclusions)

        monkeypatch.setattr(spam_ring, "get_db_session_context", fake_session_context)
        monkeypatch.setattr(
            spam_ring.moderation_filter_config_ops, "get_config", get_config
        )
        monkeypatch.setattr(
            spam_ring.moderation_config_ops, "get_config", get_moderation_config
        )
        monkeypatch.setattr(spam_ring.mod_action_ops, "create_action", create_action)
        monkeypatch.setattr(spam_ring, "dispatch_mod_action", dispatch)
        monkeypatch.setattr(spam_ring, "record_guild_event", record_event)
        monkeypatch.setattr(spam_ring, "classify_promotion", classify)
        monkeypatch.setattr(spam_ring, "judge_exclusions", judge)

    def join(self, user, joined_at: datetime, role_ids=()) -> None:
        self.rest.members[int(user.id)] = _member(
            user, joined_at=joined_at, role_ids=role_ids
        )

    def post(
        self,
        user,
        content: str,
        at: datetime,
        *,
        channel_id: int = CHANNEL_ID,
        attachments=(),
    ):
        message = SimpleNamespace(
            id=snowflake_at(at, int(user.id) % 1000),
            author=user,
            content=content,
            attachments=list(attachments),
            created_at=at,
            webhook_id=None,
        )
        self.rest.channel_messages.setdefault(channel_id, []).append(message)
        return message

    def event_for(self, message, *, channel_id: int = CHANNEL_ID):
        return SimpleNamespace(
            guild_id=GUILD_ID,
            channel_id=channel_id,
            message_id=message.id,
            content=message.content,
            author=message.author,
            member=self.rest.members.get(int(message.author.id)),
            message=message,
            is_webhook=False,
            get_guild=lambda: self.guild,
        )

    async def check(
        self, message, *, at: datetime | None = None, channel_id: int = CHANNEL_ID
    ):
        return await spam_ring.check_spam_ring(
            self.bot,
            self.event_for(message, channel_id=channel_id),
            registry=self.registry,
            now=(at or message.created_at).timestamp(),
        )

    def index(self, entry) -> int:
        return self.log.index(entry)

    def first(self, kind: str) -> int:
        return next(i for i, entry in enumerate(self.log) if entry[0] == kind)


@pytest.fixture
def harness(monkeypatch) -> Harness:
    h = Harness(monkeypatch)
    for user in (ALICE, BOB, CAROL):
        h.join(user, RING_JOIN)
    h.join(ERIN, NOW_DT - timedelta(days=800))
    h.join(STAFF, NOW_DT - timedelta(days=800), role_ids=(STAFF_ROLE_ID,))
    h.join(NED, NOW_DT - timedelta(days=2))
    return h


def stage_ring(h: Harness):
    """Question, question, ad — with an established member chatting in between."""
    q1 = h.post(
        ALICE,
        "anyone know any good hackathons coming up for students?",
        NOW_DT - timedelta(minutes=12),
    )
    chat = h.post(
        ERIN,
        "I'm refactoring my parser into a pratt parser today",
        NOW_DT - timedelta(minutes=10),
    )
    q2 = h.post(
        BOB,
        "yeah same, I want to team up with people for one",
        NOW_DT - timedelta(minutes=8),
    )
    ad = h.post(CAROL, AD_TEXT, NOW_DT)
    return q1, chat, q2, ad


# ---------------------------------------------------------------------------
# The ring, end to end
# ---------------------------------------------------------------------------


async def test_three_account_ring_is_swept_with_followups_folded_into_one_report(
    harness,
):
    q1, chat, q2, ad = stage_ring(harness)
    harness.classifier_gate = asyncio.Event()

    sweep = asyncio.create_task(harness.check(ad))
    await asyncio.sleep(0)  # the sweep holds the channel lock, waiting on the model
    # This turn lands while the sweep is still deciding: it is in the history
    # the sweep reads, and its own listener turn queues on the lock.
    followup_1 = harness.post(
        ALICE,
        "omg that sounds amazing, just registered!",
        NOW_DT + timedelta(seconds=20),
    )
    queued = asyncio.create_task(harness.check(followup_1))
    await asyncio.sleep(0)
    harness.classifier_gate.set()
    result = await sweep
    queued_result = await queued
    # This one lands after the sweep (in prod the timeout can fail, so it can).
    followup_2 = harness.post(
        BOB, "same, see you all there", NOW_DT + timedelta(minutes=2)
    )
    late_result = await harness.check(followup_2)

    assert result.action == "swept"
    assert queued_result.action == "already_handled"
    assert late_result.action == "followup"
    # One model call for the ad; the follow-ups never reach the classifier.
    assert harness.classified == [AD_TEXT]

    ring_ids = {str(ALICE.id), str(BOB.id), str(CAROL.id)}
    assert {t["user"] for t in harness.rest.timeouts} == ring_ids
    assert len(harness.rest.timeouts) == 3  # nobody timed out twice
    assert all(
        t["communication_disabled_until"] == NOW_DT + timedelta(days=7)
        for t in harness.rest.timeouts
    )
    assert sorted(harness.rest.deleted) == sorted(
        [q1.id, q2.id, ad.id, followup_1.id, followup_2.id]
    )

    assert len(harness.rest.created) == 1
    report = harness.rest.created[0]
    assert report["channel"] == REPORT_CHANNEL_ID
    assert report["user_mentions"] is False and report["role_mentions"] is False
    assert report["mentions_everyone"] is False
    attachment = report["attachment"]
    assert attachment.filename.startswith(f"spam-ring-{CHANNEL_ID}-")
    transcript = bytes(attachment.data).decode()
    for message in (q1, chat, q2, ad, followup_1):
        assert message.content in transcript
    assert "timed out for 7 days" in report["content"]
    assert "Remove Timeout" in report["content"]
    assert "4 message(s) deleted in total" in report["content"]

    # The late turn edits the one report instead of posting another.
    assert len(harness.rest.edited) == 1
    assert (
        "bob_q: 1 message deleted, already timed out"
        in harness.rest.edited[0]["content"]
    )
    assert followup_2.content in harness.incidents[-1].transcript

    assert {a["target_user_id"] for a in harness.actions} == ring_ids
    assert all(
        a["action_type"] == "timeout" and a["source"] == "handler"
        for a in harness.actions
    )
    assert all(bot is harness.bot for _, bot in harness.dispatched)


async def test_an_established_member_in_the_conversation_is_untouched(harness):
    _, chat, _, ad = stage_ring(harness)

    await harness.check(ad)

    assert str(ERIN.id) not in {t["user"] for t in harness.rest.timeouts}
    assert chat.id not in harness.rest.deleted
    assert str(ERIN.id) not in {a["target_user_id"] for a in harness.actions}
    # The judge never even sees them as a candidate.
    _, candidates, _ = harness.judge_inputs[0]
    assert str(ERIN.id) not in candidates


async def test_staff_who_joined_recently_are_never_candidates(harness):
    harness.join(STAFF, NOW_DT - timedelta(hours=3), role_ids=(STAFF_ROLE_ID,))
    stage_ring(harness)
    staff_msg = harness.post(
        STAFF, "please keep promo out of here", NOW_DT - timedelta(minutes=1)
    )
    ad = harness.rest.channel_messages[CHANNEL_ID][3]

    await harness.check(ad)

    assert str(STAFF.id) not in {t["user"] for t in harness.rest.timeouts}
    assert staff_msg.id not in harness.rest.deleted


async def test_the_judge_can_exclude_an_unrelated_newcomer(harness):
    q1, _, q2, ad = stage_ring(harness)
    ned_msg = harness.post(
        NED,
        "why does my python import fail inside a venv?",
        NOW_DT - timedelta(minutes=5),
    )
    harness.exclusions = {str(NED.id)}

    await harness.check(ad)

    _, candidates, ad_author = harness.judge_inputs[0]
    assert str(NED.id) in candidates  # the rule put them in…
    assert ad_author == str(CAROL.id)
    assert str(NED.id) not in {
        t["user"] for t in harness.rest.timeouts
    }  # …the judge took them out
    assert ned_msg.id not in harness.rest.deleted
    assert {q1.id, q2.id, ad.id} <= set(harness.rest.deleted)


async def test_without_an_exclusion_every_newcomer_in_the_window_is_a_participant(
    harness,
):
    stage_ring(harness)
    ned_msg = harness.post(
        NED,
        "why does my python import fail inside a venv?",
        NOW_DT - timedelta(minutes=5),
    )
    ad = harness.rest.channel_messages[CHANNEL_ID][3]

    await harness.check(ad)

    assert str(NED.id) in {t["user"] for t in harness.rest.timeouts}
    assert ned_msg.id in harness.rest.deleted


async def test_the_judge_cannot_add_members_or_spare_the_ad_author(harness):
    stage_ring(harness)
    ad = harness.rest.channel_messages[CHANNEL_ID][3]
    harness.exclusions = {str(CAROL.id), str(ERIN.id)}

    await harness.check(ad)

    timed_out = {t["user"] for t in harness.rest.timeouts}
    assert str(CAROL.id) in timed_out
    assert str(ERIN.id) not in timed_out


async def test_a_hackathon_mention_by_an_established_member_does_nothing(harness):
    message = harness.post(ERIN, AD_TEXT, NOW_DT)

    assert await harness.check(message) is None

    assert harness.classified == []
    assert harness.log == []


async def test_a_message_without_event_wording_costs_no_lookup(harness, monkeypatch):
    async def must_not_load(guild_id):
        raise AssertionError("loaded config for an ordinary message")

    monkeypatch.setattr(spam_ring, "load_spam_ring_settings", must_not_load)
    message = harness.post(CAROL, "hi everyone, glad to be here", NOW_DT)

    assert await harness.check(message) is None


async def test_a_newcomer_question_that_is_not_an_ad_does_nothing(harness):
    q1, *_ = stage_ring(harness)
    harness.promotion_verdict = False

    assert await harness.check(q1, at=NOW_DT) is None

    assert harness.rest.timeouts == [] and harness.rest.deleted == []


async def test_a_classifier_failure_actions_no_one(harness):
    *_, ad = stage_ring(harness)
    harness.promotion_verdict = RuntimeError("model down")

    assert await harness.check(ad) is None

    assert harness.log == []


async def test_a_judge_failure_leaves_the_rule_standing(harness, monkeypatch):
    stage_ring(harness)
    harness.post(
        NED,
        "why does my python import fail inside a venv?",
        NOW_DT - timedelta(minutes=5),
    )
    ad = harness.rest.channel_messages[CHANNEL_ID][3]

    async def broken_judge(*args):
        raise RuntimeError("model down")

    monkeypatch.setattr(spam_ring, "judge_exclusions", broken_judge)

    await harness.check(ad)

    assert len(harness.rest.timeouts) == 4


# ---------------------------------------------------------------------------
# Order of operations and failure modes
# ---------------------------------------------------------------------------


async def test_the_transcript_is_saved_before_anything_is_deleted(harness):
    *_, ad = stage_ring(harness)

    await harness.check(ad)

    assert harness.first("commit") < harness.first("timeout") < harness.first("delete")
    assert harness.first("delete") < harness.first("report") < harness.first("action")
    saved = harness.incidents[0]
    assert saved.trigger_message_id == str(ad.id)
    assert set(saved.participant_user_ids) == {
        str(ALICE.id),
        str(BOB.id),
        str(CAROL.id),
    }
    assert AD_TEXT in saved.transcript
    # Account age and join time are in the transcript.
    assert (
        "account created" in saved.transcript
        and "joined 2026-10-15" in saved.transcript
    )
    assert "link: https://devpost.example/hacknova" in saved.transcript
    assert saved.report_message_id == str(harness.rest._next_id)


async def test_nothing_is_deleted_when_the_transcript_cannot_be_saved(harness):
    *_, ad = stage_ring(harness)
    harness.save_fails = True

    await harness.check(ad)

    assert harness.rest.deleted == []
    assert len(harness.rest.timeouts) == 3
    report = harness.rest.created[0]
    assert "Messages were NOT deleted" in report["content"]
    assert report["attachment"] is not None


async def test_missing_moderate_members_still_deletes_and_reports_it_plainly(harness):
    q1, _, q2, ad = stage_ring(harness)
    harness.rest.edit_member_raises = hikari.ForbiddenError(
        url="url", headers={}, raw_body=b"", message="Missing Permissions"
    )

    await harness.check(ad)

    assert set(harness.rest.deleted) == {q1.id, q2.id, ad.id}
    assert len(harness.rest.created) == 1
    content = harness.rest.created[0]["content"]
    assert content.count("timeout failed: the bot lacks Moderate Members") == 3
    assert "does not have the Moderate Members permission" in content
    # No audit row for a timeout that never landed; the deletions are remembered.
    assert harness.actions == []
    assert len(harness.events) == 3


async def test_with_no_config_row_it_runs_on_defaults_and_reports_to_the_triage_channel(
    harness,
):
    harness.config = None
    harness.moderation_config = SimpleNamespace(response_channel_id=RESPONSE_CHANNEL_ID)
    *_, ad = stage_ring(harness)

    result = await harness.check(ad)

    assert result.action == "swept"
    assert harness.rest.created[0]["channel"] == RESPONSE_CHANNEL_ID
    assert not any(isinstance(row, ModerationFilterConfig) for row in harness.added)


async def test_mod_alert_channel_is_preferred_over_the_triage_channel(harness):
    harness.config = ModerationFilterConfig(
        guild_id=str(GUILD_ID), mod_alert_channel_id="777"
    )
    harness.moderation_config = SimpleNamespace(response_channel_id=RESPONSE_CHANNEL_ID)
    *_, ad = stage_ring(harness)

    await harness.check(ad)

    assert harness.rest.created[0]["channel"] == "777"


async def test_with_no_channel_anywhere_it_acts_and_keeps_the_incident_row(
    harness, caplog
):
    harness.config = None
    *_, ad = stage_ring(harness)

    with caplog.at_level(logging.WARNING, logger=spam_ring.__name__):
        result = await harness.check(ad)

    assert result.action == "swept"
    assert harness.rest.created == []
    assert len(harness.incidents) == 1 and len(harness.rest.timeouts) == 3
    assert "no report channel" in caplog.text


async def test_a_disabled_guild_is_left_alone(harness):
    harness.config.spam_ring_enabled = False
    *_, ad = stage_ring(harness)

    assert await harness.check(ad) is None
    assert harness.classified == []


# ---------------------------------------------------------------------------
# Follow-up window
# ---------------------------------------------------------------------------


async def test_a_late_ring_account_is_caught_in_the_followup_window(harness):
    *_, ad = stage_ring(harness)
    await harness.check(ad)
    dave = _user("dave_late", created=NOW_DT - timedelta(days=2), salt=7)
    harness.join(dave, RING_JOIN + timedelta(hours=2))
    late = harness.post(dave, "wow I signed up too", NOW_DT + timedelta(minutes=5))

    result = await harness.check(late)

    assert result.action == "followup"
    assert str(dave.id) in {t["user"] for t in harness.rest.timeouts}
    assert late.id in harness.rest.deleted
    assert len(harness.rest.created) == 1
    assert (
        "dave_late: 1 message deleted, timed out for 7 days"
        in harness.rest.edited[-1]["content"]
    )
    assert str(dave.id) in {a["target_user_id"] for a in harness.actions}


async def test_a_newcomer_who_joined_weeks_earlier_is_not_a_followup(harness):
    harness.config.spam_ring_new_member_days = 30
    *_, ad = stage_ring(harness)
    await harness.check(ad)
    harness.join(NED, NOW_DT - timedelta(days=21))
    question = harness.post(
        NED, "what happened to the messages here?", NOW_DT + timedelta(minutes=2)
    )
    actions_before = list(harness.log)

    assert await harness.check(question) is None

    assert harness.log == actions_before
    assert question.id not in harness.rest.deleted


async def test_the_followup_window_closes(harness):
    *_, ad = stage_ring(harness)
    await harness.check(ad)
    dave = _user("dave_late", created=NOW_DT - timedelta(days=2), salt=7)
    harness.join(dave, RING_JOIN)
    late = harness.post(dave, "wow nice", NOW_DT + timedelta(minutes=16))

    assert await harness.check(late) is None
    assert late.id not in harness.rest.deleted


async def test_an_incident_in_one_channel_does_not_reach_another(harness):
    *_, ad = stage_ring(harness)
    await harness.check(ad)
    dave = _user("dave_late", created=NOW_DT - timedelta(days=2), salt=7)
    harness.join(dave, RING_JOIN)
    elsewhere = harness.post(
        dave, "nice", NOW_DT + timedelta(minutes=1), channel_id=OTHER_CHANNEL_ID
    )

    assert await harness.check(elsewhere, channel_id=OTHER_CHANNEL_ID) is None


# ---------------------------------------------------------------------------
# Pure layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "Join the HackNova hackathon this weekend",
        "registration closes friday",
        "anyone want to team up?",
        "it's hosted by a university in Ohio",
        "check https://forms.gle/abc",
        "see example.com/thing",
    ],
)
def test_prefilter_passes_event_wording_and_links(content):
    assert spam_ring.passes_prefilter(content)


@pytest.mark.parametrize(
    "content", ["", "hello there", "my parser is broken", "registry pattern question"]
)
def test_prefilter_ignores_ordinary_messages(content):
    assert not spam_ring.passes_prefilter(content)


def test_links_to_event_domain_matches_host_and_subdomain():
    assert spam_ring.links_to_event_domain("go to https://hacknova.devpost.com/")
    assert not spam_ring.links_to_event_domain("go to https://notdevpost.com/")


def test_new_member_window_is_inclusive_and_unknown_is_not_new():
    window = timedelta(days=7)
    assert spam_ring.is_new_member(NOW_DT - window, NOW_DT, window)
    assert not spam_ring.is_new_member(
        NOW_DT - window - timedelta(seconds=1), NOW_DT, window
    )
    assert not spam_ring.is_new_member(None, NOW_DT, window)


def test_followup_proximity_is_24_hours_either_side():
    joins = [RING_JOIN]
    assert spam_ring.joined_near_participant(RING_JOIN + timedelta(hours=24), joins)
    assert spam_ring.joined_near_participant(RING_JOIN - timedelta(hours=23), joins)
    assert not spam_ring.joined_near_participant(RING_JOIN + timedelta(hours=25), joins)
    assert not spam_ring.joined_near_participant(None, joins)


def test_apply_judge_exclusions_only_removes_candidates_and_never_the_ad_author():
    result = spam_ring.apply_judge_exclusions(
        ["ad", "a", "b"], ad_author_id="ad", excluded_user_ids=["ad", "b", "stranger"]
    )
    assert result == ("ad", "a")


def test_a_departed_young_account_counts_as_new_and_an_old_one_does_not():
    young = spam_ring.AuthorFacts(
        user_id="1",
        username="x",
        account_created=NOW_DT - timedelta(days=1),
        joined_at=None,
        departed=True,
        is_bot=False,
        is_staff=False,
    )
    old = spam_ring.AuthorFacts(
        user_id="2",
        username="y",
        account_created=NOW_DT - timedelta(days=400),
        joined_at=None,
        departed=True,
        is_bot=False,
        is_staff=False,
    )
    messages = [
        spam_ring.TranscriptMessage(
            message_id="m1", author_id="1", timestamp=NOW_DT, content="q"
        ),
        spam_ring.TranscriptMessage(
            message_id="m2", author_id="2", timestamp=NOW_DT, content="q"
        ),
    ]
    candidates = spam_ring.select_candidates(
        messages,
        {"1": young, "2": old},
        ad_author_id="ad",
        now=NOW_DT,
        new_member_window=timedelta(days=7),
    )
    assert candidates == ("ad", "1")


def test_settings_clamp_the_timeout_to_discords_limit():
    config = ModerationFilterConfig(guild_id="1", spam_ring_timeout_seconds=40 * 86400)
    settings = spam_ring.SpamRingSettings.from_config(config)
    assert settings.timeout_seconds == spam_ring.DISCORD_MAX_TIMEOUT_SECONDS


def test_settings_report_channel_fallback_order():
    config = ModerationFilterConfig(guild_id="1", mod_log_channel_id="3")
    assert spam_ring.SpamRingSettings.from_config(config, "4").report_channel_id == "3"
    config = ModerationFilterConfig(guild_id="1")
    assert spam_ring.SpamRingSettings.from_config(config, "4").report_channel_id == "4"
    assert spam_ring.SpamRingSettings.from_config(config).report_channel_id is None


def test_defaults_turn_the_detector_on():
    settings = spam_ring.SpamRingSettings.from_config(
        ModerationFilterConfig.get_defaults("1")
    )
    assert settings.enabled
    assert settings.new_member_window == timedelta(days=7)
    assert settings.timeout_seconds == 7 * 86400
    assert settings.lookback == timedelta(minutes=60)
    assert settings.followup == timedelta(minutes=15)


def test_report_fits_discords_limit_with_many_followups():
    outcomes = [
        spam_ring.AccountOutcome(
            user_id=str(10**17 + i),
            username=f"user{i}",
            timeout="applied",
            deleted_count=2,
        )
        for i in range(4)
    ]
    lines = [
        f"- <t:{i}:T> <@{10**17 + i}> someone_with_a_long_name_{i}: 1 message deleted, already timed out"
        for i in range(80)
    ]
    content = spam_ring.format_report(
        channel_id="1",
        ad_jump_link="https://discord.com/channels/1/2/3",
        outcomes=outcomes,
        timeout_seconds=604800,
        followup_lines=lines,
    )
    assert len(content) <= 2000
    assert lines[-1] in content
    assert "earlier line(s) are in the incident record" in content


def test_registry_releases_closed_incidents_and_idle_locks():
    registry = spam_ring.SpamRingRegistry()
    registry.lock_for("c")
    registry.open_incident(
        spam_ring.ChannelIncident(
            incident_id=None,
            guild_id="g",
            channel_id="c",
            ad_jump_link="",
            followup_until=100.0,
            timeout_seconds=1,
            participant_joins=[],
            outcomes=[],
            timed_out_ids=set(),
        )
    )
    assert registry.active_incident("c", 99.0) is not None
    assert registry.active_incident("c", 100.0) is None
    assert registry._locks == {}


# ---------------------------------------------------------------------------
# Model calls, through the real utility agent (only the model is a test double)
# ---------------------------------------------------------------------------


def _test_model(monkeypatch, output: dict):
    from pydantic_ai.models.test import TestModel

    from smarter_dev.bot.agents import utility_model

    model = TestModel(custom_output_args=output)
    monkeypatch.setattr(utility_model, "build_model_for", lambda catalog_model: model)
    return model


async def test_classify_promotion_runs_the_utility_agent(monkeypatch):
    model = _test_model(
        monkeypatch, {"is_event_promotion": True, "reason": "names an event"}
    )

    assert await spam_ring.classify_promotion(AD_TEXT) is True

    prompt = model.last_model_request_parameters
    assert prompt is not None


async def test_judge_exclusions_returns_the_ids_the_model_named(monkeypatch):
    _test_model(
        monkeypatch, {"exclude_user_ids": ["42"], "reason": "unrelated question"}
    )
    facts = spam_ring.AuthorFacts(
        user_id="42",
        username="ned",
        account_created=NOW_DT - timedelta(days=300),
        joined_at=NOW_DT - timedelta(days=2),
        departed=False,
        is_bot=False,
        is_staff=False,
    )

    assert await spam_ring.judge_exclusions("transcript", [facts], "7", NOW_DT) == {
        "42"
    }


async def test_a_newcomer_the_judge_spared_is_not_swept_up_as_a_followup(harness):
    *_, ad = stage_ring(harness)
    harness.post(
        NED,
        "why does my python import fail inside a venv?",
        NOW_DT - timedelta(minutes=5),
    )
    harness.join(NED, RING_JOIN + timedelta(hours=1))  # joined alongside the ring
    harness.exclusions = {str(NED.id)}
    await harness.check(ad)
    thanks = harness.post(NED, "ah never mind, fixed it", NOW_DT + timedelta(minutes=3))

    assert await harness.check(thanks) is None

    assert str(NED.id) not in {t["user"] for t in harness.rest.timeouts}
    assert thanks.id not in harness.rest.deleted
