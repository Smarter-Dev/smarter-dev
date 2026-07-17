"""Tests for the per-channel chat-token usage leaderboard query."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from smarter_dev.web.api_native.chat_conversations import (
    channel_usage_leaderboard,
    guild_total_tokens,
)
from smarter_dev.web.models import ChatAgentEngagement, ChatAgentTurn


def _engagement(
    guild_id: str = "G1",
    channel_id: str = "C1",
    channel_name: str | None = None,
) -> ChatAgentEngagement:
    return ChatAgentEngagement(
        guild_id=guild_id,
        channel_id=channel_id,
        channel_name=channel_name,
        activation_user_id="U1",
        activation_username="alice",
        activation_message_id="M1",
    )


def _turn(
    engagement: ChatAgentEngagement,
    tokens_in: int,
    tokens_out: int,
    started_at: datetime | None = None,
) -> ChatAgentTurn:
    return ChatAgentTurn(
        engagement=engagement,
        request_id="req1",
        turn_kind="initial",
        output_kind="send_response",
        triggering_messages=[],
        agent_output={},
        started_at=started_at or datetime.now(UTC),
        chat_tokens_input=tokens_in,
        chat_tokens_output=tokens_out,
    )


async def _leaderboard(db_session, guild_id="G1", days=1, limit=20):
    since = datetime.now(UTC) - timedelta(days=days)
    return await channel_usage_leaderboard(
        db_session, guild_id=guild_id, since=since, limit=limit
    )


async def test_sums_tokens_per_channel_ordered_descending(db_session):
    quiet = _engagement(channel_id="C-quiet")
    busy = _engagement(channel_id="C-busy")
    db_session.add_all(
        [
            quiet,
            busy,
            _turn(quiet, 100, 50),
            _turn(busy, 1000, 500),
            _turn(busy, 200, 300),
        ]
    )
    await db_session.commit()

    rows = await _leaderboard(db_session)
    assert [(r.channel_id, r.total_tokens) for r in rows] == [
        ("C-busy", 2000),
        ("C-quiet", 150),
    ]


async def test_channels_aggregate_across_engagements(db_session):
    first = _engagement(channel_id="C1")
    second = _engagement(channel_id="C1")
    db_session.add_all([first, second, _turn(first, 100, 0), _turn(second, 50, 0)])
    await db_session.commit()

    rows = await _leaderboard(db_session)
    assert [(r.channel_id, r.total_tokens) for r in rows] == [("C1", 150)]


async def test_respects_limit(db_session):
    for i in range(5):
        engagement = _engagement(channel_id=f"C{i}")
        db_session.add_all([engagement, _turn(engagement, (i + 1) * 100, 0)])
    await db_session.commit()

    rows = await _leaderboard(db_session, limit=3)
    assert [r.channel_id for r in rows] == ["C4", "C3", "C2"]


async def test_turns_outside_the_window_are_excluded(db_session):
    engagement = _engagement(channel_id="C1")
    old = datetime.now(UTC) - timedelta(days=10)
    db_session.add_all(
        [
            engagement,
            _turn(engagement, 999, 999, started_at=old),
            _turn(engagement, 100, 0),
        ]
    )
    await db_session.commit()

    rows = await _leaderboard(db_session, days=7)
    assert [(r.channel_id, r.total_tokens) for r in rows] == [("C1", 100)]


async def test_guilds_are_isolated(db_session):
    ours = _engagement(guild_id="G1", channel_id="C1")
    theirs = _engagement(guild_id="G2", channel_id="C2")
    db_session.add_all([ours, theirs, _turn(ours, 100, 0), _turn(theirs, 900, 0)])
    await db_session.commit()

    rows = await _leaderboard(db_session, guild_id="G1")
    assert [r.channel_id for r in rows] == ["C1"]


async def test_channel_name_snapshot_is_surfaced(db_session):
    engagement = _engagement(channel_id="C1", channel_name="general")
    db_session.add_all([engagement, _turn(engagement, 10, 0)])
    await db_session.commit()

    rows = await _leaderboard(db_session)
    assert rows[0].channel_name == "general"


async def test_guild_total_sums_all_time_across_channels(db_session):
    first = _engagement(channel_id="C1")
    second = _engagement(channel_id="C2")
    old = datetime.now(UTC) - timedelta(days=400)
    db_session.add_all(
        [
            first,
            second,
            _turn(first, 100, 50),
            _turn(second, 200, 0, started_at=old),
        ]
    )
    await db_session.commit()

    assert await guild_total_tokens(db_session, guild_id="G1") == 350
    assert await guild_total_tokens(db_session, guild_id="other") == 0


async def test_guild_total_respects_the_window(db_session):
    engagement = _engagement(channel_id="C1")
    old = datetime.now(UTC) - timedelta(days=10)
    db_session.add_all(
        [engagement, _turn(engagement, 100, 0), _turn(engagement, 900, 0, started_at=old)]
    )
    await db_session.commit()

    since = datetime.now(UTC) - timedelta(days=7)
    assert await guild_total_tokens(db_session, guild_id="G1", since=since) == 100
