"""Tests for the spam ring helpers the moderation coordinator shares
(``smarter_dev/bot/spam_ring.py``).

The bot-core detector that swept and acted on rings was removed after the #240
cutover. What remains is the pure layer (trigger, candidates, settings) and
the two Discord readers the coordinator's live path calls, which are driven
here against a fake REST client and cache.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import hikari
import pytest

from smarter_dev.bot import spam_ring

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
        id=user.id,
        role_ids=list(role_ids),
        joined_at=joined_at,
        user=user,
        is_bot=user.is_bot,
    )


class _FakeRest:
    def __init__(self) -> None:
        self.members: dict[int, SimpleNamespace] = {}
        #: user id -> error raised when that member is looked up
        self.fetch_member_raises_for: dict[int, Exception] = {}

    async def fetch_roles(self, guild_id):
        return list(self.guild.get_roles().values())

    async def fetch_member(self, guild_id, user_id):
        if int(user_id) in self.fetch_member_raises_for:
            raise self.fetch_member_raises_for[int(user_id)]
        member = self.members.get(int(user_id))
        if member is None:
            raise hikari.NotFoundError(
                url="url", headers={}, raw_body=b"", message="gone"
            )
        return member


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

    def get_roles(self):
        # @everyone (the guild's own id) grants nothing staff-like.
        return {
            role_id: self.get_role(role_id)
            for role_id in (GUILD_ID, *self.role_permissions)
        }


class _FakeCache:
    def __init__(self, rest: _FakeRest, guild: _FakeGuild) -> None:
        self.rest = rest
        self.guild = guild
        self.channels: dict[int, SimpleNamespace] = {
            CHANNEL_ID: SimpleNamespace(permission_overwrites={})
        }

    def get_guild(self, guild_id):
        return self.guild if int(guild_id) == GUILD_ID else None

    def get_guild_channels_view_for_guild(self, guild_id):
        return dict(self.channels)

    def get_member(self, guild_id, user_id):
        return self.rest.members.get(int(user_id))

    def get_user(self, user_id):
        member = self.rest.members.get(int(user_id))
        return member.user if member else None

    def get_guild_channel(self, channel_id):
        return self.channels.get(int(channel_id))

    def get_thread(self, channel_id):
        return None


class _FakeBot:
    def __init__(self, rest: _FakeRest, guild: _FakeGuild) -> None:
        self.rest = rest
        self.cache = _FakeCache(rest, guild)

    def get_me(self):
        return SimpleNamespace(id=1, username="smarter-dev")


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


def test_clamp_caps_the_timeout_at_discords_limit():
    assert (
        spam_ring.clamp_timeout_seconds(40 * 86400)
        == spam_ring.DISCORD_MAX_TIMEOUT_SECONDS
    )
    assert spam_ring.clamp_timeout_seconds(7 * 86400) == 7 * 86400


# ---------------------------------------------------------------------------
# Discord readers (the coordinator's live path)
# ---------------------------------------------------------------------------


def _bot(*, cached: bool = True) -> tuple[_FakeBot, _FakeRest]:
    rest = _FakeRest()
    guild = _FakeGuild()
    rest.guild = guild
    bot = _FakeBot(rest, guild)
    if not cached:
        bot.cache.get_member = lambda guild_id, user_id: None
    return bot, rest


async def test_resolve_author_reads_the_join_time_of_a_new_member():
    bot, rest = _bot()
    rest.members[int(ALICE.id)] = _member(ALICE, joined_at=RING_JOIN)

    facts = await spam_ring._resolve_author(bot, GUILD_ID, ALICE, ())

    assert facts.user_id == str(ALICE.id)
    assert facts.username == "alice_q"
    assert facts.joined_at == RING_JOIN
    assert facts.account_created == NOW_DT - timedelta(days=2)
    assert not facts.departed and not facts.is_staff and not facts.is_bot


@pytest.mark.parametrize(
    ("role_id", "extra_roles"),
    [(STAFF_ROLE_ID, ()), (KICKER_ROLE_ID, ()), (777, ("777",))],
    ids=["manage_messages", "kick_members", "configured_staff_role"],
)
async def test_resolve_author_marks_staff(role_id, extra_roles):
    bot, rest = _bot()
    rest.members[int(NED.id)] = _member(NED, joined_at=RING_JOIN, role_ids=(role_id,))

    facts = await spam_ring._resolve_author(bot, GUILD_ID, NED, extra_roles)

    assert facts.is_staff


async def test_resolve_author_falls_back_to_rest_and_knows_who_left():
    bot, rest = _bot(cached=False)
    rest.members[int(BOB.id)] = _member(BOB, joined_at=RING_JOIN)

    present = await spam_ring._resolve_author(bot, GUILD_ID, BOB, ())
    departed = await spam_ring._resolve_author(bot, GUILD_ID, CAROL, ())

    assert present.joined_at == RING_JOIN and not present.departed
    assert departed.departed and departed.joined_at is None
    # A departed young account still counts as new through its creation time.
    assert departed.effective_join() == NOW_DT - timedelta(days=2)


async def test_resolve_author_fails_closed_when_the_lookup_errors():
    """Unknown is not new, and an author it cannot clear counts as staff."""
    bot, rest = _bot(cached=False)
    rest.fetch_member_raises_for[int(ALICE.id)] = RuntimeError("discord down")

    facts = await spam_ring._resolve_author(bot, GUILD_ID, ALICE, ())

    assert facts.joined_at is None and not facts.departed
    assert facts.effective_join() is None
    assert facts.is_staff


def test_to_transcript_message_keeps_attachments_and_webhooks():
    message = SimpleNamespace(
        id=42,
        author=SimpleNamespace(id=7),
        created_at=NOW_DT,
        content=None,
        attachments=[SimpleNamespace(url="https://cdn.example/a.png")],
        webhook_id=99,
    )

    converted = spam_ring._to_transcript_message(message)

    assert converted == spam_ring.TranscriptMessage(
        message_id="42",
        author_id="7",
        timestamp=NOW_DT,
        content="",
        attachment_urls=("https://cdn.example/a.png",),
        is_webhook=True,
    )


def test_transcript_filename_is_utc_and_names_the_channel():
    moment = datetime(2026, 10, 16, 16, 0, 5, tzinfo=UTC).astimezone()
    assert (
        spam_ring.transcript_filename("200", moment)
        == "spam-ring-200-20261016T160005Z.txt"
    )
