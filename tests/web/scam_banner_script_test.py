"""The scam-banner reference script, run in the real handler runtime.

``scripts/handler_scripts/scam-banner.monty`` is the copy an admin pastes into
the live handler. On 2026-10-05 the live script reviewed the first of seven
copies of a scam posted across seven channels in nine seconds and skipped the
other six, because nothing told it the posts belonged together. These tests pin
the behaviour agreed after that:

* a post that looks the same (text, file names and sizes) in two channels
  within ten seconds is a burst: the member is held at once, the post is
  reviewed once, and every copy is removed on a confirmed violation;
* a member actioned in the last 30 days gets closer scrutiny, not deletion:
  each later message is reviewed on its own and removed only if it is itself a
  violation;
* every confirmed action is reported to the mod log with the member's record;
* a confirmed repeat within 30 days is held for a day so moderators can act.

Several fires run for one burst, and a review takes longer than the burst. The
last section runs fires against each other and out of order — pausing them
inside the review, inside a history read and inside a timeout call — and checks
the timeout the member is left under. The fires share real claims and real
member holds over one Redis; only Discord and the agent are stood in for.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from functools import partial
from pathlib import Path

import fakeredis.aioredis as fakeredis_aioredis
import pytest

from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_caps import claim_handler_key
from smarter_dev.web.handler_caps import handler_claim_key
from smarter_dev.web.handler_caps import handler_key_claimed
from smarter_dev.web.handler_holds import MemberHolds
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_runtime import run_handler_script

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts/handler_scripts/scam-banner.monty"
).read_text()
MOD_LOG = "728249959098482829"
AUTHOR = "U1"
HANDLER = "H1"
DAY = 24 * 3600


# -- fakes ---------------------------------------------------------------------


@dataclass
class _Emitter:
    messages: list = field(default_factory=list)
    dms: list = field(default_factory=list)

    async def create_message(
        self, channel_id, content, ping_role_id=None, tolerate_missing_target=False
    ):
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"

    async def send_dm(self, user_id, content):
        self.dms.append((user_id, content))
        return True

    async def get_thread_parent_id(self, thread_id):
        return None

    async def get_channel_guild_id(self, channel_id):
        return "G1"


@dataclass
class _Limiter:
    async def hit(self, key, limit, window_seconds=None):
        return True


@dataclass
class _Actor:
    """Discord: the member's timeout as it stands, and every call made."""

    calls: list = field(default_factory=list)
    # The length of the timeout the member is under right now; None when free.
    hold: int | None = None
    until: datetime | None = None
    # Set to keep a timeout write open until the test lets it through.
    write_gate: asyncio.Event | None = None
    write_started: asyncio.Event = field(default_factory=asyncio.Event)

    async def timeout_until(self, user_id):
        return self.until

    async def set_timeout_until(self, user_id, until, duration_seconds):
        self.write_started.set()
        if self.write_gate is not None:
            await self.write_gate.wait()
        self.calls.append(("timeout", user_id, duration_seconds))
        self.hold, self.until = duration_seconds, until
        return until

    async def timeout_user(self, user_id, duration_seconds=600):
        until = datetime.now(UTC) + timedelta(seconds=duration_seconds)
        await self.set_timeout_until(user_id, until, duration_seconds)
        return "ok"

    async def remove_timeout(self, user_id):
        self.calls.append(("remove_timeout", user_id))
        self.hold = self.until = None
        return "ok"

    async def delete_message(self, channel_id, message_id):
        self.calls.append(("delete", channel_id, message_id))
        return "ok"

    async def ban_user(self, user_id, reason=None, delete_message_seconds=0):
        self.calls.append(("ban", user_id))
        return "ok"

    def moderator_times_out(self, seconds: int) -> None:
        """A moderator acting by hand: straight to Discord, past any lock."""
        self.hold = seconds
        self.until = datetime.now(UTC) + timedelta(seconds=seconds, milliseconds=417)

    def deleted(self) -> list:
        return sorted(call[1:] for call in self.calls if call[0] == "delete")

    def timeouts(self) -> list:
        return [call[2] for call in self.calls if call[0] == "timeout"]


@dataclass
class _World:
    """Everything a fire can reach. Fires that share a world share its state."""

    verdict: str = "VIOLATION: crypto casino screenshots"
    recent: list = field(default_factory=list)
    history: list = field(default_factory=list)
    redis: object = field(default_factory=fakeredis_aioredis.FakeRedis)
    emitter: _Emitter = field(default_factory=_Emitter)
    actor: _Actor = field(default_factory=_Actor)
    reviews: list = field(default_factory=list)
    history_prompts: list = field(default_factory=list)
    warns: list = field(default_factory=list)
    timers: list = field(default_factory=list)
    recent_reads: int = 0
    # Set to hold every review open until the test releases it.
    review_gate: asyncio.Event | None = None
    review_started: asyncio.Event = field(default_factory=asyncio.Event)
    # Set to hold the next history read open, after it has taken its snapshot.
    history_gate: asyncio.Event | None = None
    history_read: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def holds(self) -> MemberHolds:
        return MemberHolds(
            redis=self.redis, guild_id="G1", handler_id=HANDLER, actor=self.actor
        )

    async def agent(self, prompt, has_tools, budget):
        if "UNTRUSTED RECORD" in prompt:
            self.history_prompts.append(prompt)
            return "Second scam post in a month; the account looks compromised."
        self.reviews.append(prompt)
        self.review_started.set()
        if self.review_gate is not None:
            await self.review_gate.wait()
        return self.verdict

    async def read_recent(self, user_id):
        self.recent_reads += 1
        return list(self.recent)

    async def read_history(self, user_id, limit):
        snapshot = list(self.history)[:limit]
        gate, self.history_gate = self.history_gate, None
        if gate is not None:
            self.history_read.set()
            await gate.wait()
        return snapshot

    async def record_warn(self, user_id, reason, channel_id):
        # A warn is the one handler action that lands in the member's record.
        self.warns.append((user_id, reason))
        self.history.insert(0, _warned(seconds=0) | {"reason": reason})
        return len(self.warns)

    async def schedule_timer(self, fire_at, refire_context):
        self.timers.append(refire_context)

    async def mark(self, *keys: str) -> None:
        """Claim ``keys`` as an earlier fire of this handler would have."""
        for key in keys:
            await self.redis.set(handler_claim_key(HANDLER, key), "1", ex=120)

    def mod_log(self) -> list[str]:
        return [
            content for channel, content in self.emitter.messages if channel == MOD_LOG
        ]


def _ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def _image(name: str = "1.png") -> dict:
    return {
        "url": f"https://cdn.example/{name}",
        "content_type": "image/png",
        "filename": name,
        "size": 48_211,
    }


def _context(message_id: str = "M2", **overrides) -> dict:
    """A message from an established, active member with a two-year-old account."""
    context = {
        "trigger_type": "message",
        "guild_id": "G1",
        "message_id": message_id,
        "message_content": "",
        "author_id": AUTHOR,
        "author_has_manage_messages": False,
        "author_account_created_at": _ago(days=800),
        "author_joined_at": _ago(days=300),
        "author_is_first_message": False,
        "author_days_since_last_message": 0,
        "attachments": [_image()],
        "is_thread": False,
    }
    context.update(overrides)
    return context


def _row(
    message_id: str,
    channel_id: str,
    age_seconds: float,
    files: int = 1,
    content: str = "the scam",
) -> dict:
    """One recent-message row; rows with the same ``content`` are copies."""
    return {
        "channel_id": channel_id,
        "message_id": message_id,
        "age_seconds": age_seconds,
        "attachment_count": files,
        "has_link": False,
        "content_hash": content,
    }


def _warned(source: str = "handler", **delta) -> dict:
    return {
        "action_type": "warn",
        "reason": "Your scam-like post suggests your account may be compromised.",
        "source": source,
        "moderator_username": "handler:scam-banner" if source == "handler" else "a-mod",
        "duration_seconds": None,
        "channel_id": "C1",
        "trigger_message_id": None,
        "created_at": _ago(**delta),
    }


async def _fire(world: _World, context: dict, channel_id: str = "C2"):
    return await run_handler_script(
        SCRIPT,
        context,
        channel_id=channel_id,
        guild_id="G1",
        emitter=world.emitter,
        limiter=_Limiter(),
        agent_runner=world.agent,
        mod_action_reader=world.read_history,
        mod_action_recorder=world.record_warn,
        recent_messages_reader=world.read_recent,
        handler_id=HANDLER,
        timer_scheduler=world.schedule_timer,
        claimer=partial(claim_handler_key, world.redis, HANDLER),
        claim_reader=partial(handler_key_claimed, world.redis, HANDLER),
        holds=world.holds,
        budget=admin_budget(context["trigger_type"]),
        actor=world.actor,
    )


# -- the script itself ---------------------------------------------------------


def test_reference_script_passes_the_handler_lint():
    assert lint_script(SCRIPT) is None


# -- ordinary traffic stays cheap ----------------------------------------------


async def test_plain_text_from_an_established_member_costs_nothing():
    world = _World()

    result = await _fire(world, _context(message_content="morning all", attachments=[]))

    assert result.outcome == "ok"
    assert result.usage["lookups"] == 0
    assert result.usage["agent_calls"] == 0
    assert world.actor.calls == []


async def test_a_single_image_from_a_member_in_good_standing_is_not_reviewed():
    world = _World(recent=[_row("M2", "C2", 0.2)])

    result = await _fire(world, _context())

    assert result.outcome == "ok"
    assert world.reviews == []
    assert world.actor.calls == []
    assert world.emitter.messages == []


async def test_staff_are_exempt_before_any_lookup():
    world = _World(recent=[_row("M2", "C2", 0.2), _row("M1", "C1", 1.5)])

    result = await _fire(world, _context(author_has_manage_messages=True))

    assert result.usage["lookups"] == 0
    assert world.actor.calls == []


# -- the cross-channel burst ---------------------------------------------------


def _oct_5_burst() -> list[dict]:
    """Seven copies of one post in seven channels, 1.3-2.1 s apart, newest first."""
    ages = [0.2, 1.6, 3.2, 5.3, 6.7, 8.2, 9.5]
    return [
        _row(f"M{7 - index}", f"C{7 - index}", age) for index, age in enumerate(ages)
    ]


async def test_burst_is_contained_reviewed_once_and_removed_whole():
    # The 2026-10-05 miss: an established account, one image per post, so
    # neither the four-image rule nor the new-member rule applied to posts 2-7.
    world = _World(recent=_oct_5_burst())

    result = await _fire(world, _context(message_id="M7"), channel_id="C7")

    assert result.outcome == "ok"
    # Contained before the review, with the short timeout.
    assert world.actor.calls[0] == ("timeout", AUTHOR, 300)
    assert len(world.reviews) == 1
    # Every copy is gone, each from the channel it was posted in.
    assert world.actor.deleted() == sorted((f"C{n}", f"M{n}") for n in range(1, 8))
    assert len(world.warns) == 1
    [report] = world.mod_log()
    assert "removed from 7 post(s)" in report
    assert "No earlier moderation actions on record." in report
    # A first offence from an established member is not held for a day.
    assert world.actor.timeouts() == [300]
    assert world.actor.hold == 300


async def test_burst_judged_clean_lifts_the_timeout_and_deletes_nothing():
    # A member sharing one screenshot in two help channels.
    world = _World(
        verdict="CLEAN", recent=[_row("M2", "C2", 0.3), _row("M1", "C1", 4.0)]
    )

    result = await _fire(world, _context())

    assert result.outcome == "ok"
    assert world.actor.calls == [("timeout", AUTHOR, 300), ("remove_timeout", AUTHOR)]
    assert world.warns == []
    assert world.mod_log() == []


async def test_copies_in_one_channel_are_not_a_burst():
    world = _World(recent=[_row("M2", "C1", 0.3), _row("M1", "C1", 2.0)])

    result = await _fire(world, _context(), channel_id="C1")

    assert result.outcome == "ok"
    assert world.actor.calls == []
    assert world.reviews == []


async def test_copies_more_than_ten_seconds_apart_are_not_a_burst():
    world = _World(recent=[_row("M2", "C2", 0.3), _row("M1", "C1", 14.0)])

    await _fire(world, _context())

    assert world.actor.calls == []
    assert world.reviews == []


async def test_different_posts_in_two_channels_are_not_a_burst():
    # An unreviewed image in one channel, then an ordinary help screenshot in
    # another two seconds later. Reviewing the screenshot would say nothing
    # about the first post, so the pair is not treated as one thing: nobody is
    # held, and no clean verdict vouches for a post the review never saw.
    world = _World(
        verdict="CLEAN",
        recent=[
            _row("M2", "C2", 0.3, content="help screenshot"),
            _row("M1", "C1", 2.3, content="something else"),
        ],
    )

    result = await _fire(world, _context())

    assert result.outcome == "ok"
    assert world.actor.calls == []
    assert world.reviews == []


async def test_text_without_a_file_or_link_never_reads_the_recent_list():
    world = _World(verdict="CLEAN")
    context = _context(
        message_content="anyone here use a hardware wallet?",
        attachments=[],
        author_is_first_message=True,
    )

    result = await _fire(world, context)

    assert result.outcome == "ok"
    # Reviewed, as any first message with a scam keyword is.
    assert len(world.reviews) == 1
    assert world.recent_reads == 0
    assert world.actor.calls == []


async def test_first_post_reviewed_alone_still_sweeps_the_burst_that_followed():
    # The first post of the Oct 5 burst: reviewed as a first-ever message before
    # any other post existed. By the time the verdict lands the burst is there.
    world = _World(recent=[_row("M1", "C1", 0.1)])

    async def burst_arrives_during_review(prompt, has_tools, budget):
        world.recent = [
            _row("M3", "C3", 12.0),
            _row("M2", "C2", 13.5),
            _row("M1", "C1", 15.0),
        ]
        world.reviews.append(prompt)
        return world.verdict

    world.agent = burst_arrives_during_review
    context = _context(message_id="M1", author_is_first_message=True)

    result = await _fire(world, context, channel_id="C1")

    assert result.outcome == "ok"
    assert world.actor.deleted() == [("C1", "M1"), ("C2", "M2"), ("C3", "M3")]
    # The warning names a compromised account: by the verdict it was a burst.
    [(_, reason)] = world.warns
    assert "may be compromised" in reason


async def test_confirmed_violation_removes_every_copy_whatever_its_age():
    # Ten seconds decides whether to hold the member before a review. Once the
    # review confirms the post, every copy of it goes: a copy posted a minute
    # ago, and one in the same channel, are the same confirmed scam.
    world = _World(
        recent=[
            _row("M4", "C3", 0.2),
            _row("M3", "C2", 9.2),
            _row("M2", "C2", 41.0),
            _row("M1", "C1", 70.0),
            _row("M0", "C1", 80.0, content="an unrelated screenshot"),
        ]
    )

    result = await _fire(world, _context(message_id="M4"), channel_id="C3")

    assert result.outcome == "ok"
    assert world.actor.deleted() == [
        ("C1", "M1"),
        ("C2", "M2"),
        ("C2", "M3"),
        ("C3", "M4"),
    ]


async def test_copies_beyond_one_fires_reach_are_removed_by_a_follow_up_fire():
    # A fire may take 25 moderation actions, so it removes 21 posts itself and
    # arms a timer; the timer's fire removes the rest. Nothing is left up.
    burst = [_row(f"M{n}", f"C{n}", 0.2 + n * 0.3) for n in range(25)]
    world = _World(recent=burst)

    result = await _fire(world, _context(message_id="M0"), channel_id="C0")

    assert result.outcome == "ok"
    assert len(world.actor.deleted()) == 21
    [report] = world.mod_log()
    assert "removed from 21 post(s), 4 more within a minute" in report
    [refire] = world.timers
    assert refire["trigger_type"] == "timer"

    follow_up = await _fire(world, refire, channel_id="C0")

    assert follow_up.outcome == "ok"
    assert world.actor.deleted() == sorted((f"C{n}", f"M{n}") for n in range(25))
    # The follow-up only removes: no second review, warning or report.
    assert len(world.reviews) == 1
    assert len(world.warns) == 1
    assert len(world.mod_log()) == 1


async def test_a_whole_recorded_burst_fits_one_fire_and_one_follow_up():
    # The record keeps at most 30 posts per member; all of them being copies
    # is the most there can be to remove.
    burst = [_row(f"M{n}", f"C{n}", 0.2 + n * 0.3) for n in range(30)]
    world = _World(recent=burst, history=[_warned(days=22)])

    result = await _fire(world, _context(message_id="M0"), channel_id="C0")
    [refire] = world.timers
    follow_up = await _fire(world, refire, channel_id="C0")

    assert (result.outcome, follow_up.outcome) == ("ok", "ok")
    assert len(world.actor.deleted()) == 30
    assert world.actor.hold == DAY


# -- closer scrutiny for a recently actioned member -----------------------------


async def test_recently_actioned_member_is_reviewed_and_a_clean_post_stays():
    world = _World(
        verdict="CLEAN", recent=[_row("M2", "C2", 0.2)], history=[_warned(days=5)]
    )

    result = await _fire(world, _context())

    assert result.outcome == "ok"
    assert len(world.reviews) == 1
    # Reviewed on its own merits: nothing removed, nobody timed out.
    assert world.actor.calls == []
    assert world.warns == []


async def test_an_action_older_than_thirty_days_earns_no_extra_scrutiny():
    world = _World(recent=[_row("M2", "C2", 0.2)], history=[_warned(days=45)])

    await _fire(world, _context())

    assert world.reviews == []


async def test_repeat_violation_is_held_for_a_day_and_flagged_to_mods():
    # The Oct 5 18:36 account: caught for the same scam on Sep 13.
    world = _World(recent=[_row("M2", "C2", 0.2)], history=[_warned(days=22)])

    result = await _fire(world, _context())

    assert result.outcome == "ok"
    # Only this message is removed: there are no copies to sweep.
    assert world.actor.deleted() == [("C2", "M2")]
    assert world.actor.timeouts() == [DAY]
    assert len(world.warns) == 1
    [report] = world.mod_log()
    assert "timed out for 24 hours" in report
    assert "1 earlier action(s) on record." in report
    assert "Second scam post in a month" in report
    # The agent was shown the record as untrusted data, not the scam itself.
    [history_prompt] = world.history_prompts
    assert "warn: Your scam-like post" in history_prompt


@pytest.mark.parametrize("source", ["handler", "manual", "audit_log"])
async def test_an_action_minutes_ago_counts_as_the_earlier_action(source):
    # A moderator's warning five minutes ago, or this script's own warning for
    # a different post five minutes ago: either way the next confirmed
    # violation is a repeat, is held for a day, and is reported with the record.
    world = _World(recent=[_row("M2", "C2", 0.2)], history=[_warned(source, minutes=5)])

    result = await _fire(world, _context())

    assert result.outcome == "ok"
    assert world.actor.deleted() == [("C2", "M2")]
    assert world.actor.hold == DAY
    [report] = world.mod_log()
    assert "timed out for 24 hours" in report
    assert "1 earlier action(s) on record." in report
    assert len(world.history_prompts) == 1


async def test_history_note_cannot_ping_from_the_agents_reply():
    world = _World(recent=[_row("M2", "C2", 0.2)], history=[_warned(days=22)])

    async def agent(prompt, has_tools, budget):
        if "UNTRUSTED RECORD" in prompt:
            return "@everyone look at <@123>"
        return world.verdict

    world.agent = agent

    await _fire(world, _context())

    [report] = world.mod_log()
    assert "@everyone" not in report
    assert "<@123>" not in report


# -- outcomes that were already there ------------------------------------------


async def test_long_silent_member_with_a_link_is_reviewed_and_removed():
    world = _World(recent=[_row("M2", "C2", 0.2, files=0) | {"has_link": True}])
    context = _context(
        message_content="free nitro https://scam.example/claim",
        attachments=[],
        author_days_since_last_message=90,
    )

    result = await _fire(world, context)

    assert result.outcome == "ok"
    assert world.actor.deleted() == [("C2", "M2")]
    # Not a burst and not four images, so the wording is about the message.
    [(_, reason)] = world.warns
    assert reason.startswith("Your message was removed")
    # A first offence outside a burst is warned, not timed out.
    assert world.actor.timeouts() == []


async def test_thread_message_is_deleted_from_its_thread():
    world = _World(recent=[_row("M2", "T9", 0.2, files=4)])
    context = _context(
        attachments=[_image(f"{n}.png") for n in range(4)],
        is_thread=True,
        thread_id="T9",
    )

    result = await _fire(world, context, channel_id="C-PARENT")

    assert result.outcome == "ok"
    assert world.actor.deleted() == [("T9", "M2")]
    assert ("T9", "-# Investigating potential scam") in world.emitter.messages


async def test_four_images_in_one_channel_are_contained_and_only_that_post_removed():
    world = _World(recent=[_row("M2", "C2", 0.2, files=4)])
    context = _context(attachments=[_image(f"{n}.png") for n in range(4)])

    result = await _fire(world, context)

    assert result.outcome == "ok"
    assert world.actor.calls[0] == ("timeout", AUTHOR, 300)
    assert world.actor.deleted() == [("C2", "M2")]
    assert len(world.warns) == 1
    assert len(world.mod_log()) == 1


async def test_four_images_are_still_contained_when_the_recent_list_has_no_row():
    # The note for this message was lost or already aged out: the four-image
    # rule needs nothing from the list.
    world = _World(verdict="CLEAN", recent=[])
    context = _context(attachments=[_image(f"{n}.png") for n in range(4)])

    result = await _fire(world, context)

    assert result.outcome == "ok"
    assert world.actor.calls == [("timeout", AUTHOR, 300), ("remove_timeout", AUTHOR)]


async def test_account_under_a_week_old_is_banned_and_reported():
    world = _World(recent=[_row("M2", "C2", 0.2), _row("M1", "C1", 1.4)])
    context = _context(
        author_account_created_at=_ago(days=2), author_joined_at=_ago(hours=1)
    )

    result = await _fire(world, context)

    assert result.outcome == "ok"
    assert ("ban", AUTHOR) in world.actor.calls
    assert world.warns == []
    [report] = world.mod_log()
    assert "user banned" in report


async def test_unreadable_verdict_removes_nothing():
    world = _World(
        verdict="I could not open the image.",
        recent=[_row("M2", "C2", 0.2), _row("M1", "C1", 1.4)],
    )

    result = await _fire(world, _context())

    assert result.outcome == "ok"
    assert world.actor.deleted() == []
    assert world.warns == []


# -- fires against each other, and out of order ---------------------------------


async def _first_post_under_review_then_a_copy_arrives(world: _World):
    """Fire M1 (a first-ever message) and hold its review open; then fire M2.

    M2 is the same post in a second channel two seconds later, which is the
    order the Oct 5 burst arrived in. Returns the two fire results once the
    review has been released.
    """
    world.review_gate = asyncio.Event()
    world.recent = [_row("M1", "C1", 0.1)]
    first = asyncio.create_task(
        _fire(world, _context(message_id="M1", author_is_first_message=True), "C1")
    )
    await asyncio.wait_for(world.review_started.wait(), timeout=5)

    world.recent = [_row("M2", "C2", 0.1), _row("M1", "C1", 2.1)]
    # A second review would wait on the same gate; fail instead of hanging.
    second = await asyncio.wait_for(
        _fire(world, _context(message_id="M2"), "C2"), timeout=5
    )

    world.recent = [_row("M2", "C2", 15.0), _row("M1", "C1", 17.0)]
    world.review_gate.set()
    return await first, second


async def test_a_copy_arriving_mid_review_holds_the_member_without_a_second_review():
    world = _World()

    first, second = await _first_post_under_review_then_a_copy_arrives(world)

    assert first.outcome == "ok" and second.outcome == "ok"
    # One review for the post and its copy, and the copy's fire placed the hold.
    assert len(world.reviews) == 1
    assert world.actor.timeouts() == [300]
    assert world.actor.deleted() == [("C1", "M1"), ("C2", "M2")]
    assert len(world.warns) == 1
    assert len(world.mod_log()) == 1
    assert world.actor.hold == 300


async def test_a_clean_review_lifts_the_hold_another_fire_placed_for_its_copy():
    world = _World(verdict="CLEAN")

    first, second = await _first_post_under_review_then_a_copy_arrives(world)

    assert first.outcome == "ok" and second.outcome == "ok"
    assert len(world.reviews) == 1
    assert world.actor.calls == [("timeout", AUTHOR, 300), ("remove_timeout", AUTHOR)]
    assert world.actor.hold is None


async def test_a_fire_delayed_past_a_clean_verdict_does_not_hold_the_member_again():
    # The fire for the second copy sat in the queue until the review of the
    # first had already cleared the member. Holding them now would leave them
    # timed out for five minutes with no review left to lift it.
    world = _World(
        verdict="CLEAN", recent=[_row("M2", "C2", 0.3), _row("M1", "C1", 4.0)]
    )
    await _fire(world, _context(message_id="M2"), "C2")
    assert world.actor.hold is None

    world.recent = [
        _row("M3", "C3", 30.0),
        _row("M2", "C2", 31.0),
        _row("M1", "C1", 35.0),
    ]
    late = await _fire(world, _context(message_id="M3"), "C3")

    assert late.outcome == "ok"
    assert world.actor.hold is None
    assert world.actor.timeouts() == [300]
    assert len(world.reviews) == 1


async def test_a_clean_verdict_with_no_hold_placed_stops_a_late_fire_placing_one():
    # The first post was reviewed alone and judged clean before the fire for
    # its copy ran at all. That late fire must not start a hold nobody ends.
    world = _World(verdict="CLEAN", recent=[_row("M1", "C1", 0.1)])
    await _fire(world, _context(message_id="M1", author_is_first_message=True), "C1")

    world.recent = [_row("M2", "C2", 20.0), _row("M1", "C1", 22.0)]
    late = await _fire(world, _context(message_id="M2"), "C2")

    assert late.outcome == "ok"
    assert world.actor.calls == []
    assert len(world.reviews) == 1


async def test_a_fire_delayed_past_a_confirmed_repeat_does_not_shorten_the_day_hold():
    world = _World(
        recent=[_row("M2", "C2", 0.3), _row("M1", "C1", 4.0)],
        history=[_warned(days=22)],
    )
    await _fire(world, _context(message_id="M2"), "C2")
    assert world.actor.hold == DAY

    # A different post from the same moment, whose fire only runs now.
    world.recent = [
        _row("M4", "C4", 28.0, files=4, content="another post"),
        _row("M2", "C2", 30.0),
        _row("M1", "C1", 34.0),
    ]
    context = _context(
        message_id="M4", attachments=[_image(f"{n}.png") for n in range(4)]
    )
    world.verdict = "CLEAN"
    late = await _fire(world, context, "C4")

    assert late.outcome == "ok"
    # Still reviewed on its own, but the member's day-long hold is untouched.
    assert len(world.reviews) == 2
    assert world.actor.hold == DAY
    assert world.actor.timeouts() == [300, DAY]
    assert ("remove_timeout", AUTHOR) not in world.actor.calls


async def test_a_copys_fire_holding_stale_history_does_not_shorten_the_day_hold():
    # M1 is under review on its own, by a member warned three weeks ago. The
    # fire for M2, a copy in another channel, has read the member's history
    # and is paused there when M1 is confirmed and the member held for a day.
    # M2's fire then carries on with what it read before any of that.
    world = _World(history=[_warned(days=22)], recent=[_row("M1", "C1", 0.1)])
    world.review_gate = asyncio.Event()
    first = asyncio.create_task(_fire(world, _context(message_id="M1"), "C1"))
    await asyncio.wait_for(world.review_started.wait(), timeout=5)

    world.recent = [_row("M2", "C2", 0.1), _row("M1", "C1", 2.1)]
    world.history_gate = asyncio.Event()
    paused = world.history_gate
    second = asyncio.create_task(_fire(world, _context(message_id="M2"), "C2"))
    await asyncio.wait_for(world.history_read.wait(), timeout=5)

    world.review_gate.set()
    assert (await asyncio.wait_for(first, timeout=5)).outcome == "ok"
    assert world.actor.hold == DAY

    paused.set()
    assert (await asyncio.wait_for(second, timeout=5)).outcome == "ok"

    assert len(world.reviews) == 1
    assert world.actor.timeouts() == [DAY]
    assert world.actor.hold == DAY


async def test_a_timeout_call_still_in_flight_does_not_outlive_a_clean_verdict():
    # M2's fire has begun the hold for the burst, and its timeout call is
    # still on its way to Discord when M1's review comes back clean. The clean
    # verdict must end that hold, not finish before it starts.
    world = _World(verdict="CLEAN", recent=[_row("M1", "C1", 0.1)])
    world.review_gate = asyncio.Event()
    first = asyncio.create_task(
        _fire(world, _context(message_id="M1", author_is_first_message=True), "C1")
    )
    await asyncio.wait_for(world.review_started.wait(), timeout=5)

    world.recent = [_row("M2", "C2", 0.1), _row("M1", "C1", 2.1)]
    world.actor.write_gate = asyncio.Event()
    second = asyncio.create_task(_fire(world, _context(message_id="M2"), "C2"))
    await asyncio.wait_for(world.actor.write_started.wait(), timeout=5)

    world.review_gate.set()
    await asyncio.sleep(0.2)
    # The clean fire is waiting its turn on the member's timeout.
    assert not first.done()

    world.actor.write_gate.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    assert [r.outcome for r in results] == ["ok", "ok"]
    assert len(world.reviews) == 1
    assert world.actor.calls == [("timeout", AUTHOR, 300), ("remove_timeout", AUTHOR)]
    assert world.actor.hold is None


def _two_posts_under_review(world: _World):
    """Fire two different four-image posts and hold both reviews open.

    Returns the two fires and a function that lets one review finish with a
    verdict.
    """
    gates = {"post A": asyncio.Event(), "post B": asyncio.Event()}
    verdicts: dict[str, str] = {}
    started: dict[str, asyncio.Event] = {name: asyncio.Event() for name in gates}

    async def agent(prompt, has_tools, budget):
        if "UNTRUSTED RECORD" in prompt:
            return "No pattern."
        name = "post A" if "post A" in prompt else "post B"
        world.reviews.append(name)
        started[name].set()
        await gates[name].wait()
        return verdicts[name]

    world.agent = agent
    world.recent = [
        _row("MB", "C2", 0.2, files=4, content="post B"),
        _row("MA", "C1", 1.0, files=4, content="post A"),
    ]
    images = [_image(f"{n}.png") for n in range(4)]
    fires = {
        "post A": asyncio.create_task(
            _fire(
                world,
                _context(message_id="MA", message_content="post A", attachments=images),
                "C1",
            )
        ),
        "post B": asyncio.create_task(
            _fire(
                world,
                _context(message_id="MB", message_content="post B", attachments=images),
                "C2",
            )
        ),
    }

    async def finish(name: str, verdict: str):
        await asyncio.wait_for(started[name].wait(), timeout=5)
        verdicts[name] = verdict
        gates[name].set()
        return await asyncio.wait_for(fires[name], timeout=5)

    async def both_started():
        for event in started.values():
            await asyncio.wait_for(event.wait(), timeout=5)

    return finish, both_started


async def test_a_clean_post_does_not_release_a_member_with_another_post_under_review():
    # Two different posts, each held for and each under its own review. The
    # first comes back clean while the second is still being looked at.
    world = _World()
    finish, both_started = _two_posts_under_review(world)
    await both_started()
    # One write or two, as the two holds fall in the same second or not.
    assert set(world.actor.timeouts()) == {300}

    clean = await finish("post A", "CLEAN")

    assert clean.outcome == "ok"
    assert world.actor.hold == 300
    assert ("remove_timeout", AUTHOR) not in world.actor.calls

    # The second is confirmed: the member is still held, and stays held.
    confirmed = await finish("post B", "VIOLATION: scam")

    assert confirmed.outcome == "ok"
    assert world.actor.deleted() == [("C2", "MB")]
    assert world.actor.hold == 300
    assert ("remove_timeout", AUTHOR) not in world.actor.calls


async def test_the_last_of_two_clean_posts_releases_the_member():
    world = _World()
    finish, both_started = _two_posts_under_review(world)
    await both_started()

    await finish("post B", "CLEAN")
    assert world.actor.hold == 300
    await finish("post A", "CLEAN")

    assert world.actor.hold is None
    assert world.actor.deleted() == []


async def test_a_clean_review_leaves_a_timeout_a_moderator_set_meanwhile():
    # While this burst is under review a moderator times the member out for an
    # hour by hand. This burst turning out clean must not undo that.
    world = _World(
        verdict="CLEAN", recent=[_row("M2", "C2", 0.3), _row("M1", "C1", 4.0)]
    )

    async def moderator_acts_during_review(prompt, has_tools, budget):
        world.reviews.append(prompt)
        world.actor.moderator_times_out(3600)
        return world.verdict

    world.agent = moderator_acts_during_review

    result = await _fire(world, _context(message_id="M2"), "C2")

    assert result.outcome == "ok"
    assert ("remove_timeout", AUTHOR) not in world.actor.calls
    assert world.actor.hold == 3600


async def test_a_burst_never_shortens_a_timeout_a_moderator_already_set():
    # The moderator's timeout landed while the burst's fires were queued.
    world = _World(
        verdict="CLEAN", recent=[_row("M2", "C2", 0.3), _row("M1", "C1", 4.0)]
    )
    world.actor.moderator_times_out(7 * DAY)

    result = await _fire(world, _context(message_id="M2"), "C2")

    assert result.outcome == "ok"
    assert world.actor.timeouts() == []
    assert world.actor.hold == 7 * DAY


async def test_a_confirmed_repeat_never_shortens_a_moderators_longer_timeout():
    world = _World(recent=[_row("M2", "C2", 0.2)], history=[_warned(days=22)])

    async def moderator_acts_during_review(prompt, has_tools, budget):
        if "UNTRUSTED RECORD" in prompt:
            return "A repeat."
        world.actor.moderator_times_out(7 * DAY)
        return world.verdict

    world.agent = moderator_acts_during_review

    result = await _fire(world, _context())

    assert result.outcome == "ok"
    assert len(world.warns) == 1
    assert world.actor.timeouts() == []
    assert world.actor.hold == 7 * DAY


async def test_a_clean_review_leaves_a_hold_placed_for_something_else_meanwhile():
    # While this burst is under review, a different post by the same member is
    # confirmed as a repeat and held for a day. This burst turning out clean
    # must not release that hold.
    world = _World(
        verdict="CLEAN", recent=[_row("M2", "C2", 0.3), _row("M1", "C1", 4.0)]
    )

    async def other_violation_confirmed_during_review(prompt, has_tools, budget):
        world.reviews.append(prompt)
        await world.record_warn(AUTHOR, "a different post", "C9")
        await world.holds.hold(AUTHOR, "repeat:a different post", DAY)
        return world.verdict

    world.agent = other_violation_confirmed_during_review

    result = await _fire(world, _context(message_id="M2"), "C2")

    assert result.outcome == "ok"
    assert ("remove_timeout", AUTHOR) not in world.actor.calls
    assert world.actor.hold == DAY


async def test_a_repost_of_a_post_just_confirmed_is_removed_without_a_second_review():
    # Warned for a scam link, the member posts the same thing again a minute
    # later, while the first review's claim on that post is still live.
    world = _World(
        recent=[_row("M9", "C1", 0.3), _row("M1", "C1", 60.0)],
        history=[_warned(seconds=40)],
    )
    await world.mark("review:the scam", "bad:the scam")

    result = await _fire(world, _context(message_id="M9"), "C1")

    assert result.outcome == "ok"
    assert world.actor.deleted() == [("C1", "M9")]
    assert world.reviews == []


async def test_a_repost_of_a_post_judged_clean_stays_whatever_else_the_member_did():
    # M1 was reviewed and judged clean. A moderator then warned the member
    # over something unrelated. M2, the same clean post again twenty seconds
    # after M1, must not be removed on the strength of that warning.
    world = _World(verdict="CLEAN", recent=[_row("M1", "C1", 0.1)])
    await _fire(world, _context(message_id="M1", author_is_first_message=True), "C1")
    world.history.insert(0, _warned("manual", seconds=5))

    world.recent = [_row("M2", "C2", 0.2), _row("M1", "C1", 20.0)]
    repost = await _fire(world, _context(message_id="M2"), "C2")

    assert repost.outcome == "ok"
    assert world.actor.deleted() == []
    assert len(world.reviews) == 1
    assert world.warns == []
    assert world.mod_log() == []


async def test_a_copy_that_misses_the_sweep_finds_the_verdict_and_removes_itself():
    # The review confirmed M1 and swept the copies it could see. M2 was posted
    # after that sweep read the list; its own fire runs next.
    world = _World(recent=[_row("M1", "C1", 0.1)])
    await _fire(world, _context(message_id="M1", author_is_first_message=True), "C1")
    assert world.actor.deleted() == [("C1", "M1")]

    world.recent = [_row("M2", "C2", 0.2), _row("M1", "C1", 30.0)]
    late = await _fire(world, _context(message_id="M2"), "C2")

    assert late.outcome == "ok"
    assert world.actor.deleted() == [("C1", "M1"), ("C2", "M2")]
    assert len(world.reviews) == 1
    assert len(world.warns) == 1


async def test_a_copy_of_a_post_still_under_review_is_left_for_that_review():
    world = _World(
        recent=[_row("M3", "C3", 0.3), _row("M2", "C2", 1.5), _row("M1", "C1", 3.0)]
    )
    await world.mark("review:the scam")
    await world.holds.hold(AUTHOR, "the scam", 300)
    world.actor.calls.clear()

    result = await _fire(world, _context(message_id="M3"), "C3")

    assert result.outcome == "ok"
    assert world.actor.calls == []
    assert world.reviews == []
    assert world.emitter.messages == []
