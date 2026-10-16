"""Tests for the bot-core spam ring detector (``smarter_dev/bot/spam_ring.py``).

The pure layer is tested directly. The shell is driven end to end through
``check_spam_ring`` against a fake REST client and a fake DB session that log
every side effect in one ordered list, so "saved before deleted" is a check on
that order rather than on mocks being called. Both model calls are replaced:
the Jev ad check and participant check are injected by monkeypatching the
module, and the Jev calls themselves are tested against a ``FunctionModel``
stand-in, as the message gate's tests do.
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
KICKER_ROLE_ID = 501

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
# A newcomer who joined near the ring, with an unrelated question.
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
        #: user id -> error raised for that user's timeout only
        self.edit_member_raises_for: dict[str, Exception] = {}
        self.bulk_delete_raises: Exception | None = None
        #: message id -> error raised when deleting that message alone
        self.delete_raises_for: dict[int, Exception] = {}
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
        if str(user_id) in self.edit_member_raises_for:
            raise self.edit_member_raises_for[str(user_id)]
        self.log.append(("timeout", str(user_id)))
        self.timeouts.append({"user": str(user_id), **kwargs})

    async def delete_message(self, channel_id, message_id):
        if int(message_id) in self.delete_raises_for:
            raise self.delete_raises_for[int(message_id)]
        self.log.append(("delete", (int(message_id),)))
        self.deleted.append(int(message_id))

    async def delete_messages(self, channel_id, message_ids):
        if self.bulk_delete_raises is not None:
            raise self.bulk_delete_raises
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
        self.id = GUILD_ID
        self.owner_id = 0
        self.role_permissions = {
            STAFF_ROLE_ID: hikari.Permissions.MANAGE_MESSAGES,
            KICKER_ROLE_ID: hikari.Permissions.KICK_MEMBERS,
        }

    def get_role(self, role_id):
        return SimpleNamespace(
            id=role_id,
            permissions=self.role_permissions.get(
                int(role_id), hikari.Permissions.NONE
            ),
        )


class _FakeCache:
    def __init__(self, rest: _FakeRest) -> None:
        self.rest = rest
        self.channels: dict[int, SimpleNamespace] = {}

    def get_member(self, guild_id, user_id):
        return self.rest.members.get(int(user_id))

    def get_guild_channel(self, channel_id):
        return self.channels.get(int(channel_id))

    def get_thread(self, channel_id):
        return None


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
        self.promotion_verdict: bool | Exception | spam_ring.JevVerdict = True
        #: user id -> verdict; a candidate not named gets a confident yes
        self.participant_verdicts: dict[str, spam_ring.JevVerdict] = {}
        self.participant_error: Exception | None = None
        self.jev_inputs: list = []
        self.save_fails = False
        self.action_fails_for: set[str] = set()
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
            if kwargs["target_user_id"] in harness.action_fails_for:
                raise RuntimeError("db down")
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
            verdict = harness.promotion_verdict
            if isinstance(verdict, Exception):
                raise verdict
            if isinstance(verdict, bool):
                return spam_ring.JevVerdict(yes=verdict, confidence=0.9)
            return verdict

        async def participants(context, ad, candidates):
            harness.jev_inputs.append((context, ad, [c.user_id for c in candidates]))
            if harness.participant_error is not None:
                raise harness.participant_error
            return {
                c.user_id: harness.participant_verdicts.get(
                    c.user_id, spam_ring.JevVerdict(yes=True, confidence=0.9)
                )
                for c in candidates
            }

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
        monkeypatch.setattr(spam_ring, "select_participants", participants)

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
    # Joined six hours before the ring: inside the 24-hour join proximity.
    h.join(NED, RING_JOIN - timedelta(hours=6))
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
    # Jev never even sees them as a candidate.
    _, _, candidates = harness.jev_inputs[0]
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


async def test_jev_picks_two_of_three_candidates_and_only_they_and_the_author_are_actioned(
    harness,
):
    q1, _, q2, ad = stage_ring(harness)
    ned_msg = harness.post(
        NED,
        "why does my python import fail inside a venv?",
        NOW_DT - timedelta(minutes=5),
    )
    harness.participant_verdicts = {
        str(NED.id): spam_ring.JevVerdict(yes=False, confidence=0.9)
    }

    result = await harness.check(ad)

    context, ad_text, candidates = harness.jev_inputs[0]
    assert set(candidates) == {str(ALICE.id), str(BOB.id), str(NED.id)}
    assert str(CAROL.id) not in candidates  # the author is never up for judgment
    assert ad_text == AD_TEXT
    assert any(ned_msg.content in line for line in context)
    timed_out = {t["user"] for t in harness.rest.timeouts}
    assert timed_out == {str(ALICE.id), str(BOB.id), str(CAROL.id)}
    assert ned_msg.id not in harness.rest.deleted
    assert {q1.id, q2.id, ad.id} == set(harness.rest.deleted)
    assert result.incident.spared_user_ids == {str(NED.id)}
    # The verdicts are in the transcript for mods.
    assert "participant check: no, confidence 0.90" in harness.incidents[0].transcript


async def test_a_low_confidence_yes_is_not_actioned(harness):
    q1, _, q2, ad = stage_ring(harness)
    harness.participant_verdicts = {
        str(BOB.id): spam_ring.JevVerdict(yes=True, confidence=0.69)
    }

    await harness.check(ad)

    timed_out = {t["user"] for t in harness.rest.timeouts}
    assert timed_out == {str(ALICE.id), str(CAROL.id)}
    assert q2.id not in harness.rest.deleted


async def test_a_jev_failure_actions_only_the_author_and_lists_the_candidates(harness):
    q1, _, q2, ad = stage_ring(harness)
    harness.participant_error = TimeoutError()

    result = await harness.check(ad)

    assert result.action == "swept"
    assert [t["user"] for t in harness.rest.timeouts] == [str(CAROL.id)]
    assert harness.rest.deleted == [ad.id]
    content = harness.rest.created[0]["content"]
    assert "The participant check failed" in content
    assert f"<@{ALICE.id}>" in content and f"<@{BOB.id}>" in content
    # The follow-up rule does not quietly action them instead.
    later = harness.post(ALICE, "so excited!!", NOW_DT + timedelta(minutes=1))
    assert await harness.check(later) is None
    assert later.id not in harness.rest.deleted


async def test_a_newcomer_who_joined_days_before_the_ad_author_is_untouched(harness):
    *_, ad = stage_ring(harness)
    harness.join(NED, RING_JOIN - timedelta(days=3))
    ned_msg = harness.post(
        NED, "does anyone know a good hackathon?", NOW_DT - timedelta(minutes=4)
    )

    await harness.check(ad)

    _, _, candidates = harness.jev_inputs[0]
    assert str(NED.id) not in candidates
    assert str(NED.id) not in {t["user"] for t in harness.rest.timeouts}
    assert ned_msg.id not in harness.rest.deleted


async def test_a_low_confidence_ad_verdict_does_nothing(harness):
    *_, ad = stage_ring(harness)
    harness.promotion_verdict = spam_ring.JevVerdict(yes=True, confidence=0.5)

    assert await harness.check(ad) is None
    assert harness.log == []


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
    assert content.count("timeout failed: missing permission or role hierarchy") == 3
    assert "Timeouts failed for missing permission or role hierarchy" in content
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


def _facts(user_id: str, joined_at: datetime | None, **overrides):
    values = {
        "user_id": user_id,
        "username": f"user{user_id}",
        "account_created": NOW_DT - timedelta(days=30),
        "joined_at": joined_at,
        "departed": False,
        "is_bot": False,
        "is_staff": False,
    }
    return spam_ring.AuthorFacts(**(values | overrides))


def _said(*author_ids: str):
    return [
        spam_ring.TranscriptMessage(
            message_id=f"m{index}", author_id=author_id, timestamp=NOW_DT, content="q"
        )
        for index, author_id in enumerate(author_ids)
    ]


def test_candidates_must_have_joined_within_a_day_of_the_ad_author():
    ad_join = NOW_DT - timedelta(days=4)
    authors = {
        "near": _facts("near", ad_join + timedelta(hours=23)),
        "far": _facts("far", ad_join - timedelta(days=3)),
        "staff": _facts("staff", ad_join, is_staff=True),
    }
    candidates = spam_ring.select_candidates(
        _said("near", "far", "staff"),
        authors,
        ad_author_id="ad",
        ad_author_joined_at=ad_join,
        now=NOW_DT,
        new_member_window=timedelta(days=7),
    )
    assert candidates == ("ad", "near")
    # Without the author's join time nobody else can be tied to them.
    assert spam_ring.select_candidates(
        _said("near"),
        authors,
        ad_author_id="ad",
        ad_author_joined_at=None,
        now=NOW_DT,
        new_member_window=timedelta(days=7),
    ) == ("ad",)


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
        ad_author_joined_at=NOW_DT - timedelta(days=1),
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
    assert settings.new_member_window == timedelta(days=2)
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


def _incident(channel_id: str = "c", followup_until: float = 100.0):
    return spam_ring.ChannelIncident(
        incident_id=None,
        guild_id="g",
        channel_id=channel_id,
        ad_jump_link="",
        followup_until=followup_until,
        timeout_seconds=1,
        participant_joins=[],
        outcomes=[],
        timed_out_ids=set(),
    )


async def test_registry_closes_incidents_and_drops_locks_once_nobody_holds_them():
    registry = spam_ring.SpamRingRegistry()
    registry.open_incident(_incident())
    async with registry.channel_lock("c"):
        assert registry.is_busy("c")
    assert registry.active_incident("c", 99.0) is not None
    assert registry.active_incident("c", 100.0) is None
    assert registry._locks == {}


async def test_a_lock_with_a_queued_turn_is_never_replaced_by_a_new_one():
    """Old race: release, prune before the waiter runs, a third turn gets a fresh lock."""
    registry = spam_ring.SpamRingRegistry()
    inside = 0
    most_inside = 0
    gates = {name: asyncio.Event() for name in ("first", "second", "third")}

    async def turn(name: str) -> None:
        nonlocal inside, most_inside
        async with registry.channel_lock("c"):
            inside += 1
            most_inside = max(most_inside, inside)
            await gates[name].wait()
            inside -= 1

    first = asyncio.create_task(turn("first"))
    await asyncio.sleep(0)
    second = asyncio.create_task(turn("second"))
    await asyncio.sleep(0)  # queued on the lock
    gates["first"].set()
    await first
    # The first turn released; the second has not run yet. Prune, then arrive.
    registry.prune(10**12)
    third = asyncio.create_task(turn("third"))
    await asyncio.sleep(0)
    for gate in gates.values():
        gate.set()
    await asyncio.gather(second, third)

    assert most_inside == 1
    assert registry._locks == {}


def test_classifier_budget_caps_each_author_and_the_guild():
    budget = spam_ring.ClassifierBudget(
        per_author=2, author_window=600, per_guild_minute=3
    )

    assert budget.try_spend("g", "a", 0.0) is None
    assert budget.try_spend("g", "a", 1.0) is None
    assert budget.try_spend("g", "a", 2.0) == "author"
    assert budget.try_spend("g", "a", 601.0) is None  # the window slid on
    assert budget.try_spend("g", "b", 602.0) is None
    assert budget.try_spend("g", "c", 603.0) is None
    assert budget.try_spend("g", "d", 604.0) == "guild"
    assert budget.try_spend("other", "d", 604.0) is None
    assert budget.try_spend("g", "d", 662.0) is None


async def test_a_link_posting_spammer_costs_at_most_two_ad_checks_per_ten_minutes(
    harness,
):
    for minute in range(5):
        message = harness.post(
            CAROL,
            f"look at https://example.com/{minute}",
            NOW_DT + timedelta(minutes=minute),
        )
        harness.promotion_verdict = False
        await harness.check(message)

    assert len(harness.classified) == 2


async def test_the_guild_cap_skips_classification_past_twenty_a_minute(harness):
    for index in range(25):
        user = _user(
            f"new{index}", created=NOW_DT - timedelta(days=1), salt=100 + index
        )
        harness.join(user, RING_JOIN)
        message = harness.post(
            user, "check https://example.com", NOW_DT + timedelta(seconds=index)
        )
        harness.promotion_verdict = False
        await harness.check(message)

    assert len(harness.classified) == 20


# ---------------------------------------------------------------------------
# Staff
# ---------------------------------------------------------------------------


def test_moderate_kick_and_ban_count_as_staff():
    for permission in (
        hikari.Permissions.MODERATE_MEMBERS,
        hikari.Permissions.KICK_MEMBERS,
        hikari.Permissions.BAN_MEMBERS,
        hikari.Permissions.MANAGE_MESSAGES,
        hikari.Permissions.ADMINISTRATOR,
    ):
        assert spam_ring.has_staff_permissions(permission)
    assert not spam_ring.has_staff_permissions(hikari.Permissions.SEND_MESSAGES)


def test_channel_overwrites_apply_in_discords_order():
    allow_manage = SimpleNamespace(
        allow=hikari.Permissions.MANAGE_MESSAGES, deny=hikari.Permissions.NONE
    )
    deny_manage = SimpleNamespace(
        allow=hikari.Permissions.NONE, deny=hikari.Permissions.MANAGE_MESSAGES
    )
    base = hikari.Permissions.NONE
    # A role grant is overridden by a member-level deny, and vice versa.
    assert spam_ring.apply_channel_overwrites(
        base, {7: allow_manage}, everyone_id="1", role_ids=["7"], member_id="9"
    ) == (hikari.Permissions.MANAGE_MESSAGES)
    assert (
        spam_ring.apply_channel_overwrites(
            base,
            {7: allow_manage, 9: deny_manage},
            everyone_id="1",
            role_ids=["7"],
            member_id="9",
        )
        == hikari.Permissions.NONE
    )


async def test_a_new_member_with_kick_members_is_not_a_candidate(harness):
    harness.join(BOB, RING_JOIN, role_ids=(KICKER_ROLE_ID,))
    q1, _, q2, ad = stage_ring(harness)

    await harness.check(ad)

    _, _, candidates = harness.jev_inputs[0]
    assert str(BOB.id) not in candidates
    assert q2.id not in harness.rest.deleted


async def test_a_channel_overwrite_making_a_newcomer_a_moderator_exempts_them(harness):
    harness.bot.cache.channels[CHANNEL_ID] = SimpleNamespace(
        permission_overwrites={
            BOB.id: SimpleNamespace(
                allow=hikari.Permissions.MANAGE_MESSAGES,
                deny=hikari.Permissions.NONE,
            )
        }
    )
    q1, _, q2, ad = stage_ring(harness)

    await harness.check(ad)

    assert str(BOB.id) not in {t["user"] for t in harness.rest.timeouts}
    assert q2.id not in harness.rest.deleted


# ---------------------------------------------------------------------------
# Unexpected Discord and database errors
# ---------------------------------------------------------------------------


def _server_error():
    return hikari.InternalServerError(
        url="url", status=500, headers={}, raw_body=b"", message="boom"
    )


async def test_a_500_on_the_second_timeout_still_finishes_the_sweep(harness):
    q1, _, q2, ad = stage_ring(harness)
    harness.rest.edit_member_raises_for[str(BOB.id)] = _server_error()

    result = await harness.check(ad)

    assert result.action == "swept"
    assert harness.registry.active_incident(str(CHANNEL_ID), NOW) is result.incident
    assert {t["user"] for t in harness.rest.timeouts} == {str(ALICE.id), str(CAROL.id)}
    # Every message is still deleted and the report still goes out.
    assert set(harness.rest.deleted) == {q1.id, q2.id, ad.id}
    content = harness.rest.created[0]["content"]
    assert "bob_q: **timeout failed: Discord error, retry by hand**" in content
    # Audit rows for exactly the timeouts that landed; the delete is remembered.
    assert {a["target_user_id"] for a in harness.actions} == {
        str(ALICE.id),
        str(CAROL.id),
    }
    assert [e for e in harness.events if str(BOB.id) in str(e)]


async def test_a_failing_delete_is_retried_per_message_and_reported(harness):
    q1, _, q2, ad = stage_ring(harness)
    harness.rest.bulk_delete_raises = _server_error()
    harness.rest.delete_raises_for[q2.id] = _server_error()

    result = await harness.check(ad)

    assert result.action == "swept"
    assert set(harness.rest.deleted) == {q1.id, ad.id}
    content = harness.rest.created[0]["content"]
    assert "1 message(s) could not be deleted" in content
    assert "bob_q: timed out for 7 days, 0 message(s) deleted" in content
    assert result.incident.report_message_id is not None


async def test_a_failing_report_and_audit_write_do_not_stop_the_rest(harness):
    *_, ad = stage_ring(harness)
    harness.action_fails_for = {str(ALICE.id)}

    result = await harness.check(ad)

    assert result.action == "swept"
    assert {a["target_user_id"] for a in harness.actions} == {
        str(BOB.id),
        str(CAROL.id),
    }


async def test_a_followup_deletes_before_it_writes_the_audit_row(harness):
    *_, ad = stage_ring(harness)
    await harness.check(ad)
    dave = _user("dave_late", created=NOW_DT - timedelta(days=2), salt=7)
    harness.join(dave, RING_JOIN + timedelta(hours=2))
    late = harness.post(dave, "wow I signed up too", NOW_DT + timedelta(minutes=5))
    harness.log.clear()

    await harness.check(late)

    assert harness.index(("delete", (late.id,))) < harness.index(
        ("action", str(dave.id))
    )


async def test_a_followup_audit_failure_still_edits_the_report(harness):
    *_, ad = stage_ring(harness)
    await harness.check(ad)
    dave = _user("dave_late", created=NOW_DT - timedelta(days=2), salt=7)
    harness.join(dave, RING_JOIN + timedelta(hours=2))
    harness.action_fails_for = {str(dave.id)}
    late = harness.post(dave, "wow I signed up too", NOW_DT + timedelta(minutes=5))

    result = await harness.check(late)

    assert result.action == "followup"
    assert late.id in harness.rest.deleted
    assert "dave_late" in harness.rest.edited[-1]["content"]


# ---------------------------------------------------------------------------
# Jev calls (only the model is a test double, as in the message gate's tests)
# ---------------------------------------------------------------------------


def _jev_answering(answers: dict, confidence: dict, captured: dict | None = None):
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    def respond(messages, info):
        if captured is not None:
            captured["instructions"] = messages[-1].instructions
            captured["material"] = messages[-1].parts[-1].content
            captured["schema"] = info.output_tools[0].parameters_json_schema
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=answers)],
            provider_details={"confidence": confidence},
        )

    return FunctionModel(respond)


@pytest.fixture
def jev(monkeypatch):
    def use(model):
        monkeypatch.setattr(spam_ring, "_jev_model", model)

    yield use


async def test_classify_promotion_asks_jev_one_question(jev):
    captured: dict = {}
    jev(_jev_answering({"promotes_event": True}, {"promotes_event": 0.92}, captured))

    verdict = await spam_ring.classify_promotion(AD_TEXT)

    assert verdict == spam_ring.JevVerdict(yes=True, confidence=0.92)
    assert verdict.acted_on(spam_ring.AD_MIN_CONFIDENCE)
    assert AD_TEXT in captured["material"]
    assert "PROMOTES" in captured["instructions"]
    assert list(captured["schema"]["properties"]) == ["promotes_event"]


async def test_select_participants_asks_one_question_per_candidate(jev):
    captured: dict = {}
    jev(
        _jev_answering(
            {"candidate_1": True, "candidate_2": False},
            {"candidate_1": 0.8, "candidate_2": 0.6},
            captured,
        )
    )
    candidates = [
        spam_ring.ParticipantCandidate("11", "alice_q", ("any good hackathons?",)),
        spam_ring.ParticipantCandidate("12", "ned", ("my venv is broken",)),
    ]

    verdicts = await spam_ring.select_participants(
        ["[alice_q] any good hackathons?"], AD_TEXT, candidates
    )

    assert verdicts == {
        "11": spam_ring.JevVerdict(yes=True, confidence=0.8),
        "12": spam_ring.JevVerdict(yes=False, confidence=0.6),
    }
    properties = captured["schema"]["properties"]
    assert list(properties) == ["candidate_1", "candidate_2"]
    assert "[12] ned" in properties["candidate_2"]["description"]
    assert "staged conversation" in properties["candidate_2"]["description"]
    assert "my venv is broken" in captured["material"]
    assert "AD:\n" + AD_TEXT in captured["material"]


async def test_a_yes_without_a_confidence_is_not_acted_on(jev):
    jev(_jev_answering({"promotes_event": True}, {}))

    verdict = await spam_ring.classify_promotion(AD_TEXT)

    assert verdict.yes and not verdict.acted_on(spam_ring.AD_MIN_CONFIDENCE)


async def test_a_jev_timeout_propagates(jev, monkeypatch):
    from pydantic_ai.models.function import FunctionModel

    async def slow(messages, info):
        await asyncio.sleep(1)
        raise AssertionError("should have timed out first")

    monkeypatch.setattr(spam_ring, "JEV_TIMEOUT_SECONDS", 0.01)
    jev(FunctionModel(slow))

    with pytest.raises(TimeoutError):
        await spam_ring.classify_promotion(AD_TEXT)


async def test_a_newcomer_jev_declined_is_not_swept_up_as_a_followup(harness):
    *_, ad = stage_ring(harness)
    harness.post(
        NED,
        "why does my python import fail inside a venv?",
        NOW_DT - timedelta(minutes=5),
    )
    harness.join(NED, RING_JOIN + timedelta(hours=1))  # joined alongside the ring
    harness.participant_verdicts = {
        str(NED.id): spam_ring.JevVerdict(yes=False, confidence=0.9)
    }
    await harness.check(ad)
    thanks = harness.post(NED, "ah never mind, fixed it", NOW_DT + timedelta(minutes=3))

    assert await harness.check(thanks) is None

    assert str(NED.id) not in {t["user"] for t in harness.rest.timeouts}
    assert thanks.id not in harness.rest.deleted


async def test_followup_proximity_does_not_widen_with_followup_accounts(harness):
    *_, ad = stage_ring(harness)
    await harness.check(ad)
    # Dave joined 20h after the ring: within 24h of it, so a follow-up.
    dave = _user("dave_late", created=NOW_DT - timedelta(days=2), salt=7)
    harness.join(dave, RING_JOIN + timedelta(hours=20))
    # Eve joined 40h after the ring: within 24h of Dave, but not of the ring.
    eve = _user("eve_later", created=NOW_DT - timedelta(days=2), salt=8)
    harness.join(eve, RING_JOIN + timedelta(hours=40))
    harness.promotion_verdict = False
    dave_msg = harness.post(dave, "signed up!", NOW_DT + timedelta(minutes=2))
    eve_msg = harness.post(eve, "me too, sign up!", NOW_DT + timedelta(minutes=3))

    assert (await harness.check(dave_msg)).action == "followup"
    assert await harness.check(eve_msg) is None

    assert str(eve.id) not in {t["user"] for t in harness.rest.timeouts}
    assert eve_msg.id not in harness.rest.deleted


# ---------------------------------------------------------------------------
# Follow-up authors who were not swept go through Jev
# ---------------------------------------------------------------------------


async def _sweep_then_newcomer(harness, text: str = "is this legit?"):
    *_, ad = stage_ring(harness)
    await harness.check(ad)
    dave = _user("dave_late", created=NOW_DT - timedelta(days=2), salt=7)
    harness.join(dave, RING_JOIN + timedelta(hours=2))
    late = harness.post(dave, text, NOW_DT + timedelta(minutes=5))
    harness.log.clear()
    return dave, late


def _untouched(harness, user, message) -> None:
    assert str(user.id) not in {t["user"] for t in harness.rest.timeouts}
    assert message.id not in harness.rest.deleted
    assert str(user.id) not in {a["target_user_id"] for a in harness.actions}


async def test_a_followup_newcomer_jev_says_no_to_is_untouched_and_listed(harness):
    dave, late = await _sweep_then_newcomer(harness)
    harness.participant_verdicts = {
        str(dave.id): spam_ring.JevVerdict(yes=False, confidence=0.42)
    }

    result = await harness.check(late)

    assert result.action == "followup"
    _untouched(harness, dave, late)
    # Asked alone, with the swept conversation and the ad as material.
    context, ad_text, candidates = harness.jev_inputs[-1]
    assert candidates == [str(dave.id)]
    assert ad_text == AD_TEXT
    assert any("good hackathons" in line for line in context)
    assert (
        "dave_late: new account posted during follow-up; not actioned "
        "(Jev: no, confidence 0.42), review" in harness.rest.edited[-1]["content"]
    )
    assert late.content in harness.incidents[-1].transcript
    assert str(dave.id) not in harness.incidents[-1].participant_user_ids
    assert str(dave.id) in result.incident.spared_user_ids


async def test_a_spared_followup_author_is_not_asked_again(harness):
    dave, late = await _sweep_then_newcomer(harness)
    harness.participant_verdicts = {
        str(dave.id): spam_ring.JevVerdict(yes=False, confidence=0.9)
    }
    await harness.check(late)
    asked = len(harness.jev_inputs)
    again = harness.post(dave, "anyone?", NOW_DT + timedelta(minutes=6))

    assert await harness.check(again) is None
    assert len(harness.jev_inputs) == asked
    _untouched(harness, dave, again)


async def test_a_jev_error_during_followup_actions_no_one(harness):
    dave, late = await _sweep_then_newcomer(harness)
    harness.participant_error = TimeoutError()

    await harness.check(late)

    _untouched(harness, dave, late)
    assert (
        "not actioned (Jev: check failed), review"
        in (harness.rest.edited[-1]["content"])
    )


async def test_a_low_confidence_followup_yes_is_not_actioned(harness):
    dave, late = await _sweep_then_newcomer(harness)
    harness.participant_verdicts = {
        str(dave.id): spam_ring.JevVerdict(yes=True, confidence=0.69)
    }

    await harness.check(late)

    _untouched(harness, dave, late)
    assert "(Jev: yes, confidence 0.69), review" in harness.rest.edited[-1]["content"]


async def test_a_confident_followup_yes_is_actioned(harness):
    dave, late = await _sweep_then_newcomer(harness, "wow I signed up too")
    harness.participant_verdicts = {
        str(dave.id): spam_ring.JevVerdict(yes=True, confidence=0.7)
    }

    await harness.check(late)

    assert [c for *_, c in harness.jev_inputs][-1] == [str(dave.id)]
    assert str(dave.id) in {t["user"] for t in harness.rest.timeouts}
    assert late.id in harness.rest.deleted
    assert (
        "dave_late: 1 message deleted, timed out for 7 days"
        in (harness.rest.edited[-1]["content"])
    )
    assert str(dave.id) in harness.incidents[-1].participant_user_ids


async def test_a_spent_guild_cap_during_followup_means_no_action(harness):
    dave, late = await _sweep_then_newcomer(harness)
    for second in range(spam_ring.CLASSIFIER_CALLS_PER_GUILD_MINUTE):
        harness.registry.budget.try_spend_guild(str(GUILD_ID), NOW + 290 + second / 100)
    asked = len(harness.jev_inputs)

    await harness.check(late)

    assert len(harness.jev_inputs) == asked
    _untouched(harness, dave, late)
    assert (
        "check skipped, classifier cap reached" in (harness.rest.edited[-1]["content"])
    )


async def test_a_followup_check_sees_earlier_followups_in_its_context(harness):
    dave, late = await _sweep_then_newcomer(harness, "first follow-up text")
    harness.participant_verdicts = {
        str(dave.id): spam_ring.JevVerdict(yes=False, confidence=0.9)
    }
    await harness.check(late)
    fay = _user("fay_late", created=NOW_DT - timedelta(days=2), salt=9)
    harness.join(fay, RING_JOIN + timedelta(hours=3))
    second = harness.post(fay, "nice", NOW_DT + timedelta(minutes=7))

    await harness.check(second)

    context, _, candidates = harness.jev_inputs[-1]
    assert candidates == [str(fay.id)]
    assert "[dave_late] first follow-up text" in context
