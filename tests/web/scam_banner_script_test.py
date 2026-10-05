"""The scam-banner reference script, run in the real handler runtime.

``scripts/handler_scripts/scam-banner.monty`` is the copy an admin pastes into
the live handler. On 2026-10-05 the live script reviewed the first of seven
copies of a scam posted across seven channels in nine seconds and skipped the
other six, because nothing told it the posts belonged together. These tests pin
the behaviour agreed after that:

* a cross-channel burst is contained at once, reviewed once, and removed whole
  on a confirmed violation;
* a member actioned in the last 30 days gets closer scrutiny, not deletion:
  each later message is reviewed on its own and removed only if it is itself a
  violation;
* every confirmed action is reported to the mod log with the member's record;
* a confirmed repeat within 30 days is held for a day so moderators can act.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

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
    calls: list = field(default_factory=list)

    async def timeout_user(self, user_id, duration_seconds=600):
        self.calls.append(("timeout", user_id, duration_seconds))
        return "ok"

    async def remove_timeout(self, user_id):
        self.calls.append(("remove_timeout", user_id))
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
    """Everything one fire can reach, with the verdict the reviewer will give."""

    verdict: str = "VIOLATION: crypto casino screenshots"
    recent: list = field(default_factory=list)
    history: list = field(default_factory=list)
    claimed: set = field(default_factory=set)
    emitter: _Emitter = field(default_factory=_Emitter)
    actor: _Actor = field(default_factory=_Actor)
    reviews: list = field(default_factory=list)
    history_prompts: list = field(default_factory=list)
    warns: list = field(default_factory=list)

    async def agent(self, prompt, has_tools, budget):
        if "UNTRUSTED RECORD" in prompt:
            self.history_prompts.append(prompt)
            return "Second scam post in a month; the account looks compromised."
        self.reviews.append(prompt)
        return self.verdict

    async def read_recent(self, user_id):
        return list(self.recent)

    async def read_history(self, user_id, limit):
        return list(self.history)

    async def record_warn(self, user_id, reason, channel_id):
        self.warns.append((user_id, reason))
        return len(self.warns)

    async def claim(self, key, ttl_seconds):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def mod_log(self) -> list[str]:
        return [content for channel, content in self.emitter.messages if channel == MOD_LOG]


def _ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def _image(name: str = "1.png") -> dict:
    return {
        "url": f"https://cdn.example/{name}",
        "content_type": "image/png",
        "filename": name,
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


def _row(message_id: str, channel_id: str, age_seconds: float, files: int = 1) -> dict:
    return {
        "channel_id": channel_id,
        "message_id": message_id,
        "age_seconds": age_seconds,
        "attachment_count": files,
        "has_link": False,
    }


def _warned(**delta) -> dict:
    return {
        "action_type": "warn",
        "reason": "Your scam-like post suggests your account may be compromised.",
        "source": "handler",
        "moderator_username": "handler:scam-banner",
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
    """Seven single-image posts in seven channels, 1.3-2.1 s apart, newest first."""
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
    # Every post of the burst is gone, each from the channel it was posted in.
    assert world.actor.deleted() == sorted(
        (f"C{n}", f"M{n}") for n in range(1, 8)
    )
    assert len(world.warns) == 1
    [report] = world.mod_log()
    assert "removed from 7 post(s)" in report
    assert "No earlier moderation actions on record." in report
    # A first offence from an established member is not held for a day.
    assert world.actor.timeouts() == [300]


async def test_a_later_fire_of_the_same_burst_only_contains():
    # Fire for post 3 while the fire for post 2 holds the burst's review.
    world = _World(recent=_oct_5_burst()[4:])
    world.claimed.add(f"burst:{AUTHOR}:M1")

    result = await _fire(world, _context(message_id="M3"), channel_id="C3")

    assert result.outcome == "ok"
    assert world.actor.calls == [("timeout", AUTHOR, 300)]
    assert world.reviews == []
    assert world.emitter.messages == []


async def test_burst_judged_clean_lifts_the_timeout_and_deletes_nothing():
    # A member sharing one screenshot in two help channels.
    world = _World(verdict="CLEAN", recent=[_row("M2", "C2", 0.3), _row("M1", "C1", 4.0)])

    result = await _fire(world, _context())

    assert result.outcome == "ok"
    assert world.actor.calls == [("timeout", AUTHOR, 300), ("remove_timeout", AUTHOR)]
    assert world.warns == []
    assert world.mod_log() == []


async def test_posts_in_one_channel_are_not_a_burst():
    world = _World(recent=[_row("M2", "C1", 0.3), _row("M1", "C1", 2.0)])

    result = await _fire(world, _context(), channel_id="C1")

    assert result.outcome == "ok"
    assert world.actor.calls == []
    assert world.reviews == []


async def test_posts_more_than_ten_seconds_apart_are_not_a_burst():
    world = _World(recent=[_row("M2", "C2", 0.3), _row("M1", "C1", 14.0)])

    await _fire(world, _context())

    assert world.actor.calls == []
    assert world.reviews == []


async def test_text_only_posts_never_make_a_burst():
    # Chatting in two channels at once is ordinary; only posts carrying a file
    # or a link count.
    world = _World(
        recent=[_row("M2", "C2", 0.3), _row("M1", "C1", 2.0, files=0)]
    )

    await _fire(world, _context())

    assert world.actor.calls == []
    assert world.reviews == []


async def test_first_post_reviewed_alone_still_sweeps_the_burst_that_followed():
    # The first post of the Oct 5 burst: reviewed as a first-ever message before
    # any other post existed. By the time the verdict lands the burst is there.
    world = _World(recent=[_row("M1", "C1", 0.1)])

    async def burst_arrives_during_review(prompt, has_tools, budget):
        world.recent = [_row("M3", "C3", 12.0), _row("M2", "C2", 13.5), _row("M1", "C1", 15.0)]
        world.reviews.append(prompt)
        return world.verdict

    world.agent = burst_arrives_during_review
    context = _context(message_id="M1", author_is_first_message=True)

    result = await _fire(world, context, channel_id="C1")

    assert result.outcome == "ok"
    assert world.actor.deleted() == [("C1", "M1"), ("C2", "M2"), ("C3", "M3")]


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
    # Only this message is removed: there is no burst to sweep.
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


async def test_first_ever_message_with_a_scam_keyword_is_reviewed():
    world = _World(verdict="CLEAN")
    context = _context(
        message_content="anyone here use a hardware wallet?",
        attachments=[],
        author_is_first_message=True,
    )

    result = await _fire(world, context)

    assert result.outcome == "ok"
    assert len(world.reviews) == 1
    # No file or link, so the recent-message list was never read.
    assert result.usage["lookups"] == 0
    assert world.actor.calls == []


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
    # Not contained first, so the wording is about the message, not the account.
    [(_, reason)] = world.warns
    assert reason.startswith("Your message was removed")


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


async def test_second_confirming_fire_removes_but_does_not_warn_or_report_again():
    world = _World(recent=[_row("M2", "C2", 0.2, files=4)])
    world.claimed.add(f"actioned:{AUTHOR}")
    context = _context(attachments=[_image(f"{n}.png") for n in range(4)])

    result = await _fire(world, context)

    assert result.outcome == "ok"
    assert world.actor.deleted() == [("C2", "M2")]
    assert world.warns == []
    assert world.mod_log() == []


async def test_unreadable_verdict_removes_nothing():
    world = _World(
        verdict="I could not open the image.",
        recent=[_row("M2", "C2", 0.2), _row("M1", "C1", 1.4)],
    )

    result = await _fire(world, _context())

    assert result.outcome == "ok"
    assert world.actor.deleted() == []
    assert world.warns == []
