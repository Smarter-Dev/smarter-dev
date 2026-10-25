"""Production's admin handler scripts against the runtime without sanctions (#242).

``tests/fixtures/live_admin_handlers`` holds every live admin handler's script
as production stored it on 2026-10-25. Each one must still pass the checks a
save runs (the removed-function refusal and the safety lint) and must run to
completion against the new runtime surface: a script that still named a
removed function would end in a ``NameError`` on its next fire.

The scripts run in the real runtime with a real :class:`AdminActor` whose
moderation port is the fake Discord the gateway tests use, so
``delete_message`` takes the real bot-messages-only path. Only the actor's
role and thread REST calls are stood in for.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import pytest

from smarter_dev.web.admin_actions import AdminActor
from smarter_dev.web.admin_actions import HandlerFireFacts
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_moderation_policy import moderation_refusal
from smarter_dev.web.handler_runtime import run_handler_script
from tests.moderation_fakes import FakeMessage
from tests.moderation_fakes import FakeModerationDiscord

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "live_admin_handlers"
GUILD = "644299523686006834"
DISBOARD = "302050872383242240"
BUMP_CHANNEL = "702178352395583498"
BUMP_KING_ROLE = "715584554861330465"
NEWCOMER_ROLE = "888160821673349140"
MEMBER_ROLE = "644325811301777426"
RELAY_FORUM = "1529245717678264320"
STATS_CHANNEL = "968972011407826954"
MEMBER = "555000111222333444"

LIVE_NAMES = sorted(
    path.stem.removeprefix("live-") for path in FIXTURES.glob("*.monty")
)


def _script(name: str) -> str:
    return (FIXTURES / f"live-{name}.monty").read_text()


def test_the_snapshot_has_all_nine_live_handlers():
    assert LIVE_NAMES == [
        "disboard-bump-tracker",
        "dm-relay-forum-reply",
        "dm-relay-mirror",
        "leaver-post-janitor",
        "member-count-display",
        "onboard-and-promote",
        "onboarding-reconcile",
        "rejoin-alert",
        "server-birthday-announcement",
    ]


@pytest.mark.parametrize("name", LIVE_NAMES)
def test_every_live_script_passes_the_save_time_checks(name):
    script = _script(name)
    assert moderation_refusal(script) is None
    assert lint_script(script) is None


# -- fakes ----------------------------------------------------------------------


@dataclass
class _Emitter:
    """Every emitter call a live script makes, answered happily."""

    messages: list = field(default_factory=list)
    reactions: list = field(default_factory=list)
    renames: list = field(default_factory=list)
    posts: list = field(default_factory=list)
    dms: list = field(default_factory=list)
    threads: dict = field(default_factory=dict)
    role_members: dict = field(default_factory=dict)
    member_count: int = 1234

    async def create_message(
        self, channel_id, content, ping_role_id=None, tolerate_missing_target=False
    ):
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"

    async def add_reaction(self, channel_id, message_id, emoji):
        self.reactions.append((channel_id, message_id, emoji))

    async def rename_channel(self, channel_id, name):
        self.renames.append((channel_id, name))
        return True

    async def create_post(self, channel_id, title, content, tag_names=None):
        self.posts.append((channel_id, title, content))
        return f"post{len(self.posts)}"

    async def send_dm(self, user_id, content):
        self.dms.append((user_id, content))
        return "dm1"

    async def list_threads(self, channel_id, limit=50):
        return list(self.threads.get(channel_id, []))

    async def get_guild_member_count(self):
        return self.member_count

    async def get_role_members(self, role_id):
        return list(self.role_members.get(role_id, []))

    async def get_thread_parent_id(self, thread_id):
        return None

    async def get_channel_guild_id(self, channel_id):
        return GUILD


@dataclass
class _Limiter:
    async def hit(self, key, limit, window_seconds=None):
        return True


@dataclass
class _Timers:
    armed: list = field(default_factory=list)

    async def __call__(self, fire_at, refire_context):
        self.armed.append(refire_context)


class _Actor(AdminActor):
    """The real actor; its role and thread REST calls answer without Discord."""

    async def role_refusal(self, role_id):
        return None

    async def _mutate_role(self, method, user_id, role_id, reason):
        self.role_calls.append((method, user_id, role_id))
        return True

    async def _patch_thread(self, thread_id, payload):
        self.role_calls.append(("PATCH", thread_id, payload))
        return True


def _actor(discord: FakeModerationDiscord, name: str) -> _Actor:
    actor = _Actor(
        bot_token="t",
        guild_id=GUILD,
        staff_exempt_role_ids=(),
        moderation_port=discord,
        fire=HandlerFireFacts(handler_id="H1", handler_name=name),
    )
    actor.role_calls = []
    return actor


async def _fire(
    name: str,
    context: dict,
    *,
    trigger: str,
    channel_id: str = "",
    channel_ids: list | None = None,
    allowed_role_ids: list | None = None,
    guild_memory: dict | None = None,
    emitter: _Emitter | None = None,
    discord: FakeModerationDiscord | None = None,
    mod_action_reader=None,
):
    emitter = emitter or _Emitter()
    discord = discord or FakeModerationDiscord(guild_id=GUILD)
    actor = _actor(discord, name)
    timers = _Timers()
    kwargs = {}
    if mod_action_reader is not None:
        kwargs["mod_action_reader"] = mod_action_reader
    result = await run_handler_script(
        _script(name),
        context,
        channel_id=channel_id,
        guild_id=GUILD,
        emitter=emitter,
        limiter=_Limiter(),
        budget=admin_budget(trigger),
        actor=actor,
        channel_ids=channel_ids or [],
        allowed_role_ids=allowed_role_ids or [],
        guild_memory=guild_memory or {},
        timer_scheduler=timers,
        handler_id="H1",
        **kwargs,
    )
    return result, emitter, discord, actor, timers


def _ok(result) -> None:
    assert result.outcome == "ok", result.error
    assert "NameError" not in (result.error or "")


# -- disboard-bump-tracker ------------------------------------------------------


def _bump_message(message_id: str, **extra) -> dict:
    return {
        "trigger_type": "message",
        "message_id": message_id,
        "author_id": DISBOARD,
        "author_is_bot": True,
        "message_content": "",
        "embeds": [],
        "interaction_user_id": None,
        **extra,
    }


def _bump_discord(*messages: FakeMessage) -> FakeModerationDiscord:
    discord = FakeModerationDiscord(guild_id=GUILD)
    for message in messages:
        discord.messages[message.message_id] = message
    return discord


async def test_disboard_deletes_disboards_cooldown_notice():
    discord = _bump_discord(
        FakeMessage(BUMP_CHANNEL, "N1", DISBOARD, "wait", is_bot=True)
    )
    result, _, discord, _, _ = await _fire(
        "disboard-bump-tracker",
        _bump_message("N1"),
        trigger="message",
        channel_id=BUMP_CHANNEL,
        channel_ids=[BUMP_CHANNEL],
        allowed_role_ids=[BUMP_KING_ROLE],
        discord=discord,
    )
    _ok(result)
    assert discord.count("delete") == 1
    assert "N1" not in discord.messages


async def test_disboard_leaves_a_members_chatter_to_the_coordinator():
    discord = _bump_discord(FakeMessage(BUMP_CHANNEL, "C1", MEMBER, "hi"))
    result, _, discord, _, _ = await _fire(
        "disboard-bump-tracker",
        {**_bump_message("C1"), "author_id": MEMBER, "author_is_bot": False},
        trigger="message",
        channel_id=BUMP_CHANNEL,
        channel_ids=[BUMP_CHANNEL],
        allowed_role_ids=[BUMP_KING_ROLE],
        discord=discord,
    )
    _ok(result)
    assert discord.count("delete") == 0
    assert "C1" in discord.messages


async def test_disboard_bump_deletes_bot_messages_and_refuses_a_members():
    """A confirmed bump clears the bot's old reminder and the previous
    confirmation. Here the stored "previous confirmation" id is a member's
    message: delete_message refuses it, the member's message stays, and the
    fire carries on to crown the bumper and arm the reminder."""
    discord = _bump_discord(
        FakeMessage(BUMP_CHANNEL, "B2", DISBOARD, "", is_bot=True),
        FakeMessage(BUMP_CHANNEL, "REM1", "1000", "reminder", is_bot=True),
        FakeMessage(BUMP_CHANNEL, "MEM1", MEMBER, "a member's message"),
    )
    result, emitter, discord, actor, timers = await _fire(
        "disboard-bump-tracker",
        _bump_message(
            "B2",
            embeds=[{"title": None, "description": "Bump done! :thumbsup:"}],
            interaction_user_id="777000111222333444",
        ),
        trigger="message",
        channel_id=BUMP_CHANNEL,
        channel_ids=[BUMP_CHANNEL],
        allowed_role_ids=[BUMP_KING_ROLE],
        guild_memory={
            "disboard_reminder_message_id": "REM1",
            "disboard_confirmation_message_id": "MEM1",
        },
        discord=discord,
    )
    _ok(result)
    assert "REM1" not in discord.messages  # the bot's own old reminder: deleted
    assert "MEM1" in discord.messages  # a member's message: refused
    assert discord.count("delete") == 1
    assert ("PUT", "777000111222333444", BUMP_KING_ROLE) in actor.role_calls
    assert len(timers.armed) == 1
    assert any("Bump King" in content for _, content in emitter.messages)


async def test_disboard_reminder_timer_still_posts():
    result, emitter, _, _, _ = await _fire(
        "disboard-bump-tracker",
        {"trigger_type": "timer", "payload": {"bump_at": 100}},
        trigger="message",
        channel_id=BUMP_CHANNEL,
        channel_ids=[BUMP_CHANNEL],
        allowed_role_ids=[BUMP_KING_ROLE],
        guild_memory={"disboard_last_bump_at": 100, "disboard_reminded": False},
    )
    _ok(result)
    assert len(emitter.messages) == 1


# -- the DM relay ---------------------------------------------------------------


async def test_dm_relay_mirror_opens_a_post_and_dms_once():
    result, emitter, _, _, _ = await _fire(
        "dm-relay-mirror",
        {
            "trigger_type": "dm_message",
            "author_id": MEMBER,
            "author_display_name": "Ada",
            "content": "hello staff",
            "attachment_urls": [],
            "message_id": "D1",
            "dm_channel_id": "DM1",
        },
        trigger="dm_message",
        channel_id=RELAY_FORUM,
        channel_ids=[RELAY_FORUM],
    )
    _ok(result)
    assert emitter.posts == [(RELAY_FORUM, f"DM: Ada ({MEMBER})", "hello staff")]
    assert [user for user, _ in emitter.dms] == [MEMBER]
    assert result.guild_memory_writes == {"dm_forum_posts": {MEMBER: "post1"}}


async def test_dm_relay_mirror_reuses_a_post():
    result, emitter, _, actor, _ = await _fire(
        "dm-relay-mirror",
        {
            "trigger_type": "dm_message",
            "author_id": MEMBER,
            "author_display_name": "Ada",
            "content": "again",
            "attachment_urls": [],
        },
        trigger="dm_message",
        channel_id=RELAY_FORUM,
        channel_ids=[RELAY_FORUM],
        guild_memory={"dm_forum_posts": {MEMBER: "T9"}},
    )
    _ok(result)
    assert ("PATCH", "T9", {"archived": False}) in actor.role_calls
    assert ("T9", "again") in emitter.messages


async def test_dm_relay_forum_reply_delivers_a_staff_reply():
    result, emitter, _, _, _ = await _fire(
        "dm-relay-forum-reply",
        {
            "trigger_type": "message",
            "is_thread": True,
            "thread_id": "T9",
            "message_id": "S1",
            "author_id": "999",
            "message_content": "we can help",
            "attachments": [],
        },
        trigger="message",
        channel_id=RELAY_FORUM,
        channel_ids=[RELAY_FORUM],
        guild_memory={"dm_forum_posts": {MEMBER: "T9"}},
    )
    _ok(result)
    assert emitter.dms == [(MEMBER, "we can help")]
    assert emitter.reactions == [("T9", "S1", "📤")]


# -- member lifecycle -----------------------------------------------------------


async def test_leaver_post_janitor_closes_the_leavers_posts():
    emitter = _Emitter(
        threads={
            "1049001614922481755": [
                {"thread_id": "T1", "owner_id": MEMBER, "archived": False},
                {"thread_id": "T2", "owner_id": "1", "archived": False},
            ]
        }
    )
    result, emitter, _, actor, _ = await _fire(
        "leaver-post-janitor",
        {"trigger_type": "member_leave", "member_id": MEMBER},
        trigger="member_leave",
        emitter=emitter,
    )
    _ok(result)
    assert [call[1] for call in actor.role_calls] == ["T1"]
    assert [channel for channel, _ in emitter.messages] == ["T1"]


async def test_onboard_and_promote_grants_then_promotes():
    allowed = [NEWCOMER_ROLE, MEMBER_ROLE]
    accepted, _, _, actor, timers = await _fire(
        "onboard-and-promote",
        {"trigger_type": "member_rules_accepted", "member_id": MEMBER},
        trigger="member_rules_accepted",
        allowed_role_ids=allowed,
    )
    _ok(accepted)
    assert actor.role_calls == [("PUT", MEMBER, NEWCOMER_ROLE)]
    assert timers.armed[0]["payload"] == {"member_id": MEMBER}

    promoted, _, _, actor, _ = await _fire(
        "onboard-and-promote",
        {"trigger_type": "timer", "payload": {"member_id": MEMBER}},
        trigger="member_rules_accepted",
        allowed_role_ids=allowed,
    )
    _ok(promoted)
    assert actor.role_calls == [
        ("PUT", MEMBER, MEMBER_ROLE),
        ("DELETE", MEMBER, NEWCOMER_ROLE),
    ]


async def test_onboarding_reconcile_promotes_an_overdue_newcomer():
    emitter = _Emitter(
        role_members={
            NEWCOMER_ROLE: [
                {
                    "member_id": MEMBER,
                    "pending": False,
                    "joined_at": "2020-01-01T00:00:00+00:00",
                }
            ]
        }
    )
    result, _, _, actor, _ = await _fire(
        "onboarding-reconcile",
        {"trigger_type": "schedule"},
        trigger="schedule",
        allowed_role_ids=[MEMBER_ROLE, NEWCOMER_ROLE],
        emitter=emitter,
    )
    _ok(result)
    assert actor.role_calls == [
        ("PUT", MEMBER, MEMBER_ROLE),
        ("DELETE", MEMBER, NEWCOMER_ROLE),
    ]


async def test_member_count_display_renames_the_stats_channel():
    result, emitter, _, _, _ = await _fire(
        "member-count-display",
        {"trigger_type": "schedule"},
        trigger="schedule",
        channel_id=STATS_CHANNEL,
        channel_ids=[STATS_CHANNEL],
    )
    _ok(result)
    assert emitter.renames == [(STATS_CHANNEL, "📊Members: 1.2k")]


async def test_server_birthday_announcement_runs():
    result, _, _, _, _ = await _fire(
        "server-birthday-announcement",
        {"trigger_type": "schedule"},
        trigger="schedule",
        channel_id="1266565514793062400",
        channel_ids=["1266565514793062400"],
    )
    _ok(result)


# -- rejoin-alert: the version-1 script production runs until it is updated -----


async def test_the_old_rejoin_alert_still_posts_to_its_mod_log_channel():
    async def rows(user_id, limit):
        return [{"action_type": "ban", "created_at": "2026-05-04T18:30:00+00:00"}]

    result, emitter, discord, _, _ = await _fire(
        "rejoin-alert",
        {
            "trigger_type": "member_join",
            "member_id": MEMBER,
            "username": "recidivist",
            "is_bot": False,
        },
        trigger="member_join",
        mod_action_reader=rows,
    )
    _ok(result)
    assert emitter.messages == [
        (
            "728249959098482829",
            f"**Member Rejoined** — recidivist ({MEMBER}) has 1+ prior mod "
            "actions, most recent: ban on 2026-05-04",
        )
    ]
    assert discord.posts == []
