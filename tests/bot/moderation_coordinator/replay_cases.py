"""Recorded incidents and negatives, as replay cases for the coordinator.

* **The 2026-10-18 17:49 scam**: ``@everyone free nitro giveaway, claim it``
  with a ``dlscord.gift`` link, from an account two days old (the production
  case: ban) and, as the variant, from a 30-day-old compromised account.
* **A cross-channel burst** of one lure from an established account.
* **The staged hackathon ring** the spam ring was built for (two questions,
  an established member chatting, the ad, follow-ups), the same accounts and
  texts as ``tests/bot/spam_ring_test.py``.
* **Established-member negatives**: links, crypto talk, an image dump, a
  moderator's ``@here``, a newcomer's honest question.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import EVENT_PROMOTION
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import Attachment
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import MessageFacts
from smarter_dev.bot.moderation_coordinator.model import participant_label
from smarter_dev.bot.moderation_coordinator.policy import AttachmentPolicy
from smarter_dev.bot.moderation_coordinator.policy import GuildPolicy
from smarter_dev.bot.moderation_coordinator.replay import ReplayCase
from smarter_dev.bot.moderation_coordinator.replay import ReplayReads
from smarter_dev.bot.spam_engine import SpamEngineSettings
from smarter_dev.bot.spam_ring import AuthorFacts
from smarter_dev.bot.spam_ring import SpamRingSettings
from smarter_dev.bot.spam_ring import TranscriptMessage
from smarter_dev.web.models import ModerationFilterConfig

GUILD = "644299523686006834"
GENERAL = "702178352395580001"
OTHER = "702178352395583499"
THIRD = "702178352395583500"
BUMP = "702178352395583498"
NOW = datetime(2026, 10, 18, 17, 49, 35, tzinfo=UTC)
DISCORD_EPOCH_MS = 1420070400000

SCAM_TEXT = "@everyone free nitro giveaway, claim it https://dlscord.gift/abc123"
AD_TEXT = (
    "You should totally check out HackNova 2026 at Lakeshore Polytechnic University! "
    "$10k prize pool, register at https://devpost.example/hacknova before Friday, "
    "you can team up there"
)


def snowflake_at(moment: datetime, salt: int = 0) -> str:
    return str(((int(moment.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22) + salt)


@dataclass(frozen=True)
class Member:
    name: str
    salt: int
    created: datetime
    joined: datetime | None

    @property
    def id(self) -> str:
        return snowflake_at(self.created, self.salt)

    def facts(self, *, staff: bool = False, departed: bool = False) -> AuthorFacts:
        return AuthorFacts(
            user_id=self.id,
            username=self.name,
            account_created=self.created,
            joined_at=None if departed else self.joined,
            departed=departed,
            is_bot=False,
            is_staff=staff,
        )


def policy(**over) -> GuildPolicy:
    """smarter.dev's live policy: every legacy actor on, defaults throughout."""
    config = ModerationFilterConfig(guild_id=GUILD, spam_engine_enabled=True)
    return replace(
        GuildPolicy(
            guild_id=GUILD,
            spam=SpamEngineSettings.from_config(config),
            attachments=AttachmentPolicy(
                ignored_extensions=(".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4"),
                warn_extensions=(".txt", ".pdf", ".py"),
            ),
            ring=SpamRingSettings.from_config(config),
            scam_gate=True,
            scam_banner_matches_reference=True,
            edit_ping=True,
            bump_channel_ids=frozenset({BUMP}),
        ),
        **over,
    )


_message_salt = [100]


def say(
    member: Member,
    content: str,
    at: datetime,
    *,
    channel: str = GENERAL,
    attachments: tuple[Attachment, ...] = (),
    message_id: str | None = None,
    is_edit: bool = False,
) -> MessageFacts:
    _message_salt[0] += 1
    return MessageFacts(
        guild_id=GUILD,
        channel_id=channel,
        message_id=message_id or snowflake_at(at, _message_salt[0]),
        author_id=member.id,
        author_name=member.name,
        content=content,
        posted_at=at.timestamp(),
        account_created_at=member.created,
        joined_at=member.joined,
        attachments=attachments,
        revision="edit:x" if is_edit else "create",
        is_edit=is_edit,
    )


def transcript(*messages: MessageFacts) -> list[TranscriptMessage]:
    return [
        TranscriptMessage(
            message_id=m.message_id,
            author_id=m.author_id,
            timestamp=datetime.fromtimestamp(m.posted_at, UTC),
            content=m.content,
        )
        for m in messages
    ]


ESTABLISHED = AuthorHistory(is_first_message=False, days_since_last_message=0)

# ---------------------------------------------------------------------------
# 17:49
# ---------------------------------------------------------------------------


def scam_1749(*, account_age_days: int, answers=None) -> tuple[ReplayCase, Member]:
    """The scam post. Production: two-day-old account, joined that hour."""
    young = account_age_days < 7
    scammer = Member(
        "nitro_drop",
        salt=1,
        created=NOW - timedelta(days=account_age_days),
        joined=NOW - timedelta(hours=1) if young else NOW - timedelta(days=30),
    )
    regular = Member(
        "erin_regular", 4, NOW - timedelta(days=900), NOW - timedelta(days=800)
    )
    chat = say(
        regular, "anyone tried the new pattern matching?", NOW - timedelta(minutes=3)
    )
    post = say(scammer, SCAM_TEXT, NOW)
    reads = ReplayReads(
        histories={
            scammer.id: AuthorHistory(
                is_first_message=True, days_since_last_message=None
            )
            if young
            else ESTABLISHED
        },
        windows={
            GENERAL: (
                transcript(chat, post),
                {regular.id: regular.facts(), scammer.id: scammer.facts()},
            )
        },
    )
    case = ReplayCase(
        name=f"17:49 scam, account {account_age_days} days old",
        policy=policy(),
        messages=[post],
        answers=answers
        if answers is not None
        else {SCAM: (True, 0.97), ABUSE: (False, 0.95), EVENT_PROMOTION: (False, 0.96)},
        reads=reads,
    )
    return case, scammer


def scam_burst() -> tuple[ReplayCase, Member, list[MessageFacts]]:
    """One lure pasted into three channels within two seconds by a member of
    two years whose account was taken over."""
    victim = Member("dana_dev", 9, NOW - timedelta(days=700), NOW - timedelta(days=650))
    lure = "Free Steam gift card for the first 50, claim here https://steamcommunlty.example/claim"
    posts = [
        say(victim, lure, NOW),
        say(victim, lure, NOW + timedelta(seconds=1), channel=OTHER),
        say(victim, lure, NOW + timedelta(seconds=2), channel=THIRD),
    ]
    case = ReplayCase(
        name="cross-channel burst from a compromised member",
        policy=policy(),
        messages=posts,
        answers={SCAM: (True, 0.93), ABUSE: (False, 0.9)},
        reads=ReplayReads(histories={victim.id: ESTABLISHED}),
    )
    return case, victim, posts


# ---------------------------------------------------------------------------
# The staged hackathon ring
# ---------------------------------------------------------------------------

RING_JOIN = NOW - timedelta(days=1)
ALICE = Member("alice_q", 1, NOW - timedelta(days=2), RING_JOIN)
BOB = Member("bob_q", 2, NOW - timedelta(days=2), RING_JOIN)
CAROL = Member("carol_ad", 3, NOW - timedelta(days=2), RING_JOIN)
ERIN = Member("erin_regular", 4, NOW - timedelta(days=900), NOW - timedelta(days=800))
SAM = Member("mod_sam", 5, NOW - timedelta(days=900), NOW - timedelta(days=800))
NED = Member("ned_newbie", 6, NOW - timedelta(days=300), RING_JOIN - timedelta(hours=6))
DAVE = Member("dave_late", 7, NOW - timedelta(days=2), RING_JOIN + timedelta(hours=2))


def staged_ring() -> tuple[ReplayCase, dict[str, MessageFacts]]:
    """Question, chat, question, a newcomer's real question, the ad, then a
    ring account's cheer, a staff reply and a late ring account's cheer."""
    q1 = say(
        ALICE,
        "anyone know any good hackathons coming up for students?",
        NOW - timedelta(minutes=12),
    )
    chat = say(
        ERIN,
        "I'm refactoring my parser into a pratt parser today",
        NOW - timedelta(minutes=10),
    )
    q2 = say(
        BOB,
        "yeah same, I want to team up with people for one",
        NOW - timedelta(minutes=8),
    )
    ned = say(
        NED, "why does my python import fail inside a venv?", NOW - timedelta(minutes=5)
    )
    ad = say(CAROL, AD_TEXT, NOW)
    cheer = say(
        ALICE, "omg just registered, see you all there!", NOW + timedelta(minutes=2)
    )
    staff = say(
        SAM, "please keep event promotion in #events", NOW + timedelta(minutes=3)
    )
    late = say(DAVE, "signed up too, thanks for the tip", NOW + timedelta(minutes=4))
    window = transcript(q1, chat, q2, ned, ad)
    authors = {
        ALICE.id: ALICE.facts(),
        BOB.id: BOB.facts(),
        CAROL.id: CAROL.facts(),
        ERIN.id: ERIN.facts(),
        NED.id: NED.facts(),
    }
    case = ReplayCase(
        name="staged hackathon ring",
        policy=policy(),
        messages=[q1, chat, q2, ned, ad, cheer, staff, late],
        answers={EVENT_PROMOTION: (False, 0.9)},
        answers_by_text={
            "HackNova": {
                EVENT_PROMOTION: (True, 0.95),
                participant_label(ALICE.id): (True, 0.9),
                participant_label(BOB.id): (True, 0.88),
                participant_label(NED.id): (False, 0.92),
                participant_label(DAVE.id): (True, 0.9),
            }
        },
        reads=ReplayReads(
            staff={SAM.id: "holds a staff role"}, windows={GENERAL: (window, authors)}
        ),
    )
    return case, {
        "q1": q1,
        "chat": chat,
        "q2": q2,
        "ned": ned,
        "ad": ad,
        "cheer": cheer,
        "staff": staff,
        "late": late,
    }


# ---------------------------------------------------------------------------
# Established-member negatives
# ---------------------------------------------------------------------------


def established_negatives() -> tuple[ReplayCase, list[MessageFacts]]:
    """What a busy evening of regulars looks like. None of it is actioned."""
    regular = Member(
        "erin_regular", 4, NOW - timedelta(days=900), NOW - timedelta(days=800)
    )
    other = Member("rui", 11, NOW - timedelta(days=400), NOW - timedelta(days=380))
    mod = Member("mod_sam", 5, NOW - timedelta(days=900), NOW - timedelta(days=800))
    images = tuple(
        Attachment(
            f"shot{i}.png", "image/png", 20_000 + i, f"https://cdn.example/{i}.png"
        )
        for i in range(5)
    )
    messages = [
        say(
            regular,
            "the docs are at https://docs.python.org/3/library/asyncio.html",
            NOW,
        ),
        say(
            other,
            "is anyone holding bitcoin long term? my wallet app keeps crashing",
            NOW + timedelta(seconds=40),
        ),
        say(
            regular, "here's my setup", NOW + timedelta(seconds=80), attachments=images
        ),
        say(
            mod,
            "@here server maintenance tonight, details https://status.example",
            NOW + timedelta(seconds=120),
        ),
        say(
            other,
            "claim the free tier of the API, it's enough for side projects",
            NOW + timedelta(seconds=160),
        ),
    ]
    case = ReplayCase(
        name="established members",
        policy=policy(),
        messages=messages,
        answers={SCAM: (False, 0.95), ABUSE: (False, 0.97)},
        reads=ReplayReads(
            staff={mod.id: "holds a staff role"},
            histories={regular.id: ESTABLISHED, other.id: ESTABLISHED},
        ),
    )
    return case, messages
