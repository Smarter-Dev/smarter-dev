"""The scam-banner reference script, run in the real handler runtime.

``scripts/handler_scripts/scam-banner.monty`` is the copy an admin pastes into
the live handler. On 2026-10-05 the live script reviewed the first of seven
copies of a scam posted across seven channels in nine seconds and skipped the
other six, because nothing told it the posts belonged together. These tests pin
the behaviour agreed after that:

* the same post in two channels within ten seconds is a burst: the member is
  held at once, the post is reviewed once, and every copy is removed on a
  confirmed violation;
* a member actioned in the last 30 days gets closer scrutiny, not deletion:
  each later message is reviewed on its own and removed only if it is itself a
  violation;
* every confirmed action is reported to the mod log with the member's record;
* a confirmed repeat within 30 days is held for a day so moderators can act.

Several fires run for one burst, and a review takes longer than the burst. The
last section runs fires against each other and out of order, and checks the
hold the member is left under.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_runtime import run_handler_script

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts/handler_scripts/scam-banner.monty"
).read_text()
MOD_LOG = "728249959098482829"
AUTHOR = "U1"


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
    """Discord as the script's actions leave it."""

    calls: list = field(default_factory=list)
    # The timeout the member is under right now, in seconds; None when free.
    hold: int | None = None

    async def timeout_user(self, user_id, duration_seconds=600):
        self.calls.append(("timeout", user_id, duration_seconds))
        self.hold = duration_seconds
        return "ok"

    async def remove_timeout(self, user_id):
        self.calls.append(("remove_timeout", user_id))
        self.hold = None
        return "ok"

    async def delete_message(self, channel_id, message_id):
        self.calls.append(("delete", channel_id, message_id))
        return "ok"

    async def ban_user(self, user_id, reason=None, delete_message_seconds=0):
        self.calls.append(("ban", user_id))
        return "ok"

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
    claimed: set = field(default_factory=set)
    emitter: _Emitter = field(default_factory=_Emitter)
    actor: _Actor = field(default_factory=_Actor)
    reviews: list = field(default_factory=list)
    history_prompts: list = field(default_factory=list)
    warns: list = field(default_factory=list)
    recent_reads: int = 0
    # Set to hold every review open until the test releases it.
    review_gate: asyncio.Event | None = None
    review_started: asyncio.Event = field(default_factory=asyncio.Event)

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
        return list(self.history)[:limit]

    async def record_warn(self, user_id, reason, channel_id):
        # A warn is the one handler action that lands in the member's record.
        self.warns.append((user_id, reason))
        self.history.insert(0, _warned(seconds=0) | {"reason": reason})
        return len(self.warns)

    async def claim(self, key, ttl_seconds):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

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
        claimer=world.claim,
        budget=admin_budget("message"),
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


async def test_copies_beyond_one_fires_reach_are_named_in_the_report():
    # A fire may take 25 moderation actions. Twenty-five copies cannot all go;
    # the report says how many are left rather than passing over them.
    burst = [_row(f"M{n}", f"C{n}", 0.2 + n * 0.3) for n in range(25)]
    world = _World(recent=burst)

    result = await _fire(world, _context(message_id="M0"), channel_id="C0")

    assert result.outcome == "ok"
    assert len(world.actor.deleted()) == 21
    [report] = world.mod_log()
    assert "removed from 21 post(s), 4 more copies still up" in report


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
    assert world.actor.timeouts() == [24 * 3600]
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
    assert world.actor.hold == 24 * 3600
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
    assert world.actor.hold == 24 * 3600

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
    assert world.actor.hold == 24 * 3600
    assert world.actor.timeouts() == [300, 24 * 3600]


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
        await world.actor.timeout_user(AUTHOR, 24 * 3600)
        return world.verdict

    world.agent = other_violation_confirmed_during_review

    result = await _fire(world, _context(message_id="M2"), "C2")

    assert result.outcome == "ok"
    assert ("remove_timeout", AUTHOR) not in world.actor.calls
    assert world.actor.hold == 24 * 3600


async def test_a_repost_of_a_post_just_confirmed_is_removed_without_a_second_review():
    # Warned for a scam link, the member posts the same thing again a minute
    # later, while the first review's claim on that post is still live.
    world = _World(
        recent=[_row("M9", "C1", 0.3), _row("M1", "C1", 60.0)],
        history=[_warned(seconds=40)],
    )
    world.claimed.add(f"review:{AUTHOR}:the scam")

    result = await _fire(world, _context(message_id="M9"), "C1")

    assert result.outcome == "ok"
    assert world.actor.deleted() == [("C1", "M9")]
    assert world.reviews == []


async def test_a_copy_of_a_post_still_under_review_is_left_for_that_review():
    world = _World(
        recent=[_row("M3", "C3", 0.3), _row("M2", "C2", 1.5), _row("M1", "C1", 3.0)]
    )
    world.claimed.update({f"hold:{AUTHOR}:the scam", f"review:{AUTHOR}:the scam"})

    result = await _fire(world, _context(message_id="M3"), "C3")

    assert result.outcome == "ok"
    assert world.actor.calls == []
    assert world.reviews == []
    assert world.emitter.messages == []
