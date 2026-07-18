"""Tests for the E6 startup rules-acceptance replay (bot-core, trigger synthesis).

The replay is deliberately thin: a pure selector picks cached/REST-paged members
whose pending -> accepted transition the bot may have missed while down, and a
paced background task re-dispatches a synthetic member_rules_accepted for each
through the NORMAL dispatch path so the onboarding handler cannot tell a replay
from a live delta except by the is_reconciliation flag. All logic is pure or
clock/dispatch-injected, so it is TDD-able without a gateway and without real
sleeps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from smarter_dev.bot.plugins import handler_events
from smarter_dev.bot.plugins.handler_events import (
    _rules_accepted_context,
    find_missed_rules_acceptances,
    member_update_deltas,
    replay_missed_rules_acceptances,
    replay_startup_rules_acceptances,
)

# A real 2015+ Discord snowflake so account_created_at resolves sanely.
SNOWFLAKE = 733364234141827073
GUILD_ID = 42


def make_member(
    *,
    member_id=SNOWFLAKE,
    username="alice",
    display_name="Alice",
    nickname=None,
    is_bot=False,
    is_pending=False,
    role_ids=(GUILD_ID,),  # @everyone (== guild id) only, by default
    guild_id=GUILD_ID,
    avatar_hash="abc",
    guild_avatar_hash=None,
    joined_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
):
    return SimpleNamespace(
        id=member_id,
        username=username,
        display_name=display_name,
        nickname=nickname,
        is_bot=is_bot,
        is_pending=is_pending,
        role_ids=tuple(role_ids),
        guild_id=guild_id,
        avatar_hash=avatar_hash,
        guild_avatar_hash=guild_avatar_hash,
        joined_at=joined_at,
    )


# ---------------------------------------------------------------------------
# find_missed_rules_acceptances — the pure selector
# ---------------------------------------------------------------------------


def test_pending_member_is_excluded():
    # Still pending: the acceptance never happened, nothing to replay.
    result = find_missed_rules_acceptances([make_member(is_pending=True)])
    assert result == []


def test_member_with_non_everyone_role_is_excluded():
    # Holds a real role -> already onboarded some earlier time; do not re-fire.
    member = make_member(role_ids=(GUILD_ID, 100))
    assert find_missed_rules_acceptances([member]) == []


def test_roleless_non_pending_member_is_included():
    member = make_member(role_ids=(GUILD_ID,))
    result = find_missed_rules_acceptances([member])
    assert len(result) == 1
    assert result[0]["member_id"] == str(SNOWFLAKE)
    assert result[0]["trigger_type"] == "member_rules_accepted"


def test_bot_is_excluded():
    # A role-less non-pending bot is not an un-onboarded human.
    assert find_missed_rules_acceptances([make_member(is_bot=True)]) == []


def test_context_carries_reconciliation_flag_and_matches_real_shape():
    member = make_member()
    (context,) = find_missed_rules_acceptances([member])
    assert context["is_reconciliation"] is True
    # Identical to the live context apart from the reconciliation flag.
    without_flag = {k: v for k, v in context.items() if k != "is_reconciliation"}
    assert without_flag == _rules_accepted_context(member)


def test_selector_filters_a_mixed_population():
    members = [
        make_member(member_id=SNOWFLAKE + 1),  # included
        make_member(member_id=SNOWFLAKE + 2, is_pending=True),  # excluded
        make_member(member_id=SNOWFLAKE + 3, role_ids=(GUILD_ID, 5)),  # excluded
        make_member(member_id=SNOWFLAKE + 4, is_bot=True),  # excluded
        make_member(member_id=SNOWFLAKE + 5),  # included
    ]
    ids = [c["member_id"] for c in find_missed_rules_acceptances(members)]
    assert ids == [str(SNOWFLAKE + 1), str(SNOWFLAKE + 5)]


def test_real_rules_accepted_context_omits_reconciliation_flag():
    # A live pending -> accepted delta must NOT be flagged as reconciliation.
    old = make_member(is_pending=True)
    new = make_member(is_pending=False)
    ((trigger, context),) = member_update_deltas(old, new)
    assert trigger == "member_rules_accepted"
    assert "is_reconciliation" not in context


# ---------------------------------------------------------------------------
# replay_missed_rules_acceptances — paced dispatch (clock/dispatch injected)
# ---------------------------------------------------------------------------


async def test_replay_dispatches_each_selected_member_once():
    members = [make_member(member_id=SNOWFLAKE + i) for i in range(3)]
    calls = []

    async def fake_dispatch(channel_id, guild_id, trigger_type, context):
        calls.append((channel_id, guild_id, trigger_type, context))

    count = await replay_missed_rules_acceptances(
        str(GUILD_ID), members, fake_dispatch
    )
    assert count == 3
    assert len(calls) == 3
    for channel_id, guild_id, trigger_type, context in calls:
        assert channel_id == ""  # member events have no home channel
        assert guild_id == str(GUILD_ID)
        assert trigger_type == "member_rules_accepted"
        assert context["is_reconciliation"] is True


async def test_replay_does_nothing_when_no_missed_members():
    slept = []
    calls = []

    async def fake_dispatch(channel_id, guild_id, trigger_type, context):
        calls.append(context)

    async def fake_sleep(seconds):
        slept.append(seconds)

    count = await replay_missed_rules_acceptances(
        str(GUILD_ID),
        [make_member(is_pending=True)],
        fake_dispatch,
        sleep=fake_sleep,
    )
    assert count == 0
    assert calls == []
    assert slept == []  # nothing to pace


async def test_replay_paces_large_backlog_across_windows():
    # 150 missed members with a 60/window cap must drain over 3 windows (two
    # inter-window waits), never firing more than one window's worth in a burst.
    members = [make_member(member_id=SNOWFLAKE + i) for i in range(150)]
    timeline = []

    async def fake_dispatch(channel_id, guild_id, trigger_type, context):
        timeline.append(("dispatch", context["member_id"]))

    async def fake_sleep(seconds):
        timeline.append(("sleep", seconds))

    count = await replay_missed_rules_acceptances(
        str(GUILD_ID),
        members,
        fake_dispatch,
        batch_size=60,
        window_seconds=60,
        sleep=fake_sleep,
    )
    assert count == 150
    dispatches = [event for event in timeline if event[0] == "dispatch"]
    sleeps = [event for event in timeline if event[0] == "sleep"]
    assert len(dispatches) == 150
    assert len(sleeps) == 2  # 60 | wait | 60 | wait | 30
    # No burst: the first wait lands exactly after one window's worth of fires,
    # so a real join in the same window still has headroom under the cap.
    assert timeline.index(("sleep", 60)) == 60
    # And the second window is a full batch too (no early/late wait).
    assert timeline[61:121] == [
        ("dispatch", str(SNOWFLAKE + i)) for i in range(60, 120)
    ]


# ---------------------------------------------------------------------------
# replay_startup_rules_acceptances — routes synthetic fires through _dispatch
# ---------------------------------------------------------------------------


class AsyncMemberIterator:
    """Minimal async-iterable standing in for hikari's fetch_members result."""

    def __init__(self, members):
        self._members = list(members)

    def __aiter__(self):
        self._it = iter(self._members)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def make_fake_bot(guild_ids, members):
    rest = SimpleNamespace(
        fetch_members=lambda guild_id: AsyncMemberIterator(members)
    )
    cache = SimpleNamespace(
        get_guilds_view=lambda: {gid: object() for gid in guild_ids}
    )
    return SimpleNamespace(rest=rest, cache=cache)


async def test_startup_replay_routes_synthetic_fires_through_dispatch(monkeypatch):
    # The synthetic fires must go through the SAME _dispatch that on_member_update
    # calls for a live delta — handlers cannot tell them apart except by the flag.
    calls = []

    async def fake_dispatch(channel_id, guild_id, trigger_type, context, *, bot_message=False):
        calls.append((channel_id, guild_id, trigger_type, context))

    monkeypatch.setattr(handler_events, "_dispatch", fake_dispatch)

    members = [
        make_member(member_id=SNOWFLAKE + 1),  # included
        make_member(member_id=SNOWFLAKE + 2, is_pending=True),  # excluded
        make_member(member_id=SNOWFLAKE + 3, role_ids=(GUILD_ID, 7)),  # excluded
    ]
    bot = make_fake_bot([GUILD_ID], members)

    await replay_startup_rules_acceptances(bot)

    assert len(calls) == 1
    channel_id, guild_id, trigger_type, context = calls[0]
    assert channel_id == ""
    assert guild_id == str(GUILD_ID)
    assert trigger_type == "member_rules_accepted"
    assert context["member_id"] == str(SNOWFLAKE + 1)
    assert context["is_reconciliation"] is True


async def test_startup_replay_survives_a_guild_fetch_failure(monkeypatch):
    # One guild's REST failure must not abort the replay for the others.
    calls = []

    async def fake_dispatch(channel_id, guild_id, trigger_type, context, *, bot_message=False):
        calls.append((guild_id, context["member_id"]))

    monkeypatch.setattr(handler_events, "_dispatch", fake_dispatch)

    good_member = make_member(member_id=SNOWFLAKE + 9, guild_id=99, role_ids=(99,))

    def fetch_members(guild_id):
        if guild_id == 1:
            raise RuntimeError("gateway hiccup")
        return AsyncMemberIterator([good_member])

    bot = SimpleNamespace(
        rest=SimpleNamespace(fetch_members=fetch_members),
        cache=SimpleNamespace(get_guilds_view=lambda: {1: object(), 99: object()}),
    )

    await replay_startup_rules_acceptances(bot)

    assert calls == [("99", str(SNOWFLAKE + 9))]
