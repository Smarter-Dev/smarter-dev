"""Tests for the /history slash command and its pure rendering helpers."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import hikari
import pytest

from smarter_dev.bot.plugins import history as history_plugin
from smarter_dev.bot.plugins.history import DISCORD_MESSAGE_CHAR_LIMIT
from smarter_dev.bot.plugins.history import MODERATION_HISTORY_DEPTH
from smarter_dev.bot.plugins.history import HistoryRow
from smarter_dev.bot.plugins.history import TargetProfile
from smarter_dev.bot.plugins.history import build_jump_link
from smarter_dev.bot.plugins.history import format_history_row
from smarter_dev.bot.plugins.history import format_profile_card
from smarter_dev.bot.plugins.history import humanize_time_since
from smarter_dev.bot.plugins.history import neutralize_mass_mentions
from smarter_dev.bot.plugins.history import render_history_message
from smarter_dev.bot.plugins.history import resolve_target_profile
from smarter_dev.bot.plugins.history import to_history_row

ZERO_WIDTH_SPACE = "​"

PERMS_TARGET = "lightbulb.utils.permissions_for"

MODERATE = hikari.Permissions.MODERATE_MEMBERS
ADMIN = hikari.Permissions.ADMINISTRATOR
NONE = hikari.Permissions.NONE

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
GUILD_ID = "111"


def _profile(
    *,
    user_id: str = "222",
    username: str = "offender",
    joined_at: datetime | None = NOW - timedelta(days=90),
    is_guild_member: bool = True,
    has_accepted_rules: bool | None = True,
) -> TargetProfile:
    return TargetProfile(
        user_id=user_id,
        username=username,
        joined_at=joined_at,
        is_guild_member=is_guild_member,
        has_accepted_rules=has_accepted_rules,
    )


def _row(
    *,
    action_type: str = "warn",
    occurred_at: datetime = NOW - timedelta(days=1),
    reason: str | None = "spamming",
    source: str = "manual",
    channel_id: str | None = "555",
    trigger_message_id: str | None = "777",
) -> HistoryRow:
    return HistoryRow(
        action_type=action_type,
        occurred_at=occurred_at,
        reason=reason,
        source=source,
        channel_id=channel_id,
        trigger_message_id=trigger_message_id,
    )


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (timedelta(seconds=5), "just now"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=42), "42 minutes ago"),
        (timedelta(hours=3), "3 hours ago"),
        (timedelta(days=2), "2 days ago"),
        (timedelta(days=10), "1 week ago"),
        (timedelta(days=45), "1 month ago"),
        (timedelta(days=800), "2 years ago"),
    ],
)
def test_humanize_time_since_renders_largest_whole_unit(elapsed, expected):
    assert humanize_time_since(NOW - elapsed, NOW) == expected


def test_build_jump_link_requires_channel_and_message_ids():
    assert build_jump_link(GUILD_ID, "555", "777") == (
        "https://discord.com/channels/111/555/777"
    )
    assert build_jump_link(GUILD_ID, None, "777") is None
    assert build_jump_link(GUILD_ID, "555", None) is None
    assert build_jump_link(GUILD_ID, None, None) is None


def test_format_profile_card_for_current_member():
    card = format_profile_card(_profile(), NOW)
    assert "offender" in card
    assert "222" in card
    assert "3 months ago" in card
    assert "NO LONGER A MEMBER" not in card
    assert "Accepted rules: Yes" in card


def test_format_profile_card_marks_departed_user():
    card = format_profile_card(
        _profile(joined_at=None, is_guild_member=False, has_accepted_rules=None), NOW
    )
    assert "NO LONGER A MEMBER" in card
    assert "Accepted rules" not in card


def test_format_profile_card_omits_unknown_rules_acceptance():
    card = format_profile_card(_profile(has_accepted_rules=None), NOW)
    assert "Accepted rules" not in card
    assert "3 months ago" in card


def test_format_history_row_includes_jump_link_when_ids_present():
    rendered = format_history_row(_row(), GUILD_ID)
    assert "warn" in rendered
    assert "spamming" in rendered
    assert "manual" in rendered
    assert "[Jump To Action](https://discord.com/channels/111/555/777)" in rendered


@pytest.mark.parametrize(
    ("channel_id", "trigger_message_id"),
    [(None, "777"), ("555", None), (None, None)],
)
def test_format_history_row_renders_linkless_when_ids_missing(
    channel_id, trigger_message_id
):
    rendered = format_history_row(
        _row(channel_id=channel_id, trigger_message_id=trigger_message_id), GUILD_ID
    )
    assert "Jump To Action" not in rendered
    assert "warn" in rendered


def test_format_history_row_labels_missing_reason():
    rendered = format_history_row(_row(reason=None), GUILD_ID)
    assert "no reason recorded" in rendered


@pytest.mark.parametrize(
    ("hostile", "expected"),
    [
        ("@everyone", f"@{ZERO_WIDTH_SPACE}everyone"),
        ("@here", f"@{ZERO_WIDTH_SPACE}here"),
        ("@EveryOne", f"@{ZERO_WIDTH_SPACE}EveryOne"),
        ("ping @here now", f"ping @{ZERO_WIDTH_SPACE}here now"),
        ("nothing to defuse", "nothing to defuse"),
    ],
)
def test_neutralize_mass_mentions_breaks_everyone_and_here(hostile, expected):
    assert neutralize_mass_mentions(hostile) == expected


def test_format_profile_card_defuses_mass_mention_in_username():
    card = format_profile_card(_profile(username="evil@here"), NOW)
    assert "@here" not in card
    assert f"evil@{ZERO_WIDTH_SPACE}here" in card


def test_format_history_row_defuses_mass_mention_in_reason():
    rendered = format_history_row(_row(reason="pinged @everyone twice"), GUILD_ID)
    assert "@everyone" not in rendered
    assert f"pinged @{ZERO_WIDTH_SPACE}everyone twice" in rendered


def test_render_history_message_reports_empty_history():
    message = render_history_message(_profile(), [], GUILD_ID, NOW)
    assert "No moderation history" in message
    assert "offender" in message


def test_render_history_message_keeps_rows_newest_first():
    rows = [
        _row(action_type="ban", occurred_at=NOW - timedelta(days=1)),
        _row(action_type="warn", occurred_at=NOW - timedelta(days=9)),
    ]
    message = render_history_message(_profile(), rows, GUILD_ID, NOW)
    assert message.index("ban") < message.index("warn")


def test_render_history_message_truncates_with_older_actions_tail():
    rows = [
        _row(action_type="warn", reason="x" * 120, occurred_at=NOW - timedelta(days=n))
        for n in range(1, 51)
    ]
    message = render_history_message(_profile(), rows, GUILD_ID, NOW)

    assert len(message) <= DISCORD_MESSAGE_CHAR_LIMIT
    rendered_rows = message.count("Jump To Action")
    older_count = len(rows) - rendered_rows
    assert older_count > 0
    assert f"…and {older_count} older actions" in message


def test_render_history_message_omits_tail_when_everything_fits():
    message = render_history_message(_profile(), [_row()], GUILD_ID, NOW)
    assert "older action" not in message


def test_render_history_message_respects_a_tight_char_limit():
    rows = [_row(reason=f"reason number {n}") for n in range(5)]
    message = render_history_message(_profile(), rows, GUILD_ID, NOW, char_limit=400)
    assert len(message) <= 400
    assert "older action" in message


def test_to_history_row_maps_moderation_action_columns():
    action = SimpleNamespace(
        action_type="timeout",
        created_at=NOW - timedelta(hours=2),
        reason="rate limit",
        source="ai",
        channel_id="555",
        trigger_message_id=None,
    )
    assert to_history_row(action) == HistoryRow(
        action_type="timeout",
        occurred_at=NOW - timedelta(hours=2),
        reason="rate limit",
        source="ai",
        channel_id="555",
        trigger_message_id=None,
    )


# --------------------------------------------------------------------------- #
# Target resolution
# --------------------------------------------------------------------------- #


async def test_resolve_target_profile_uses_member_when_present():
    member = SimpleNamespace(
        id=222,
        username="offender",
        joined_at=NOW - timedelta(days=90),
        is_pending=False,
    )
    rest = Mock()
    rest.fetch_member = AsyncMock(return_value=member)
    rest.fetch_user = AsyncMock()

    profile = await resolve_target_profile(rest, 111, 222)

    assert profile == TargetProfile(
        user_id="222",
        username="offender",
        joined_at=NOW - timedelta(days=90),
        is_guild_member=True,
        has_accepted_rules=True,
    )
    rest.fetch_user.assert_not_awaited()


async def test_resolve_target_profile_treats_undefined_pending_as_unknown():
    member = SimpleNamespace(
        id=222,
        username="offender",
        joined_at=NOW - timedelta(days=90),
        is_pending=hikari.UNDEFINED,
    )
    rest = Mock()
    rest.fetch_member = AsyncMock(return_value=member)

    profile = await resolve_target_profile(rest, 111, 222)

    assert profile.has_accepted_rules is None


async def test_resolve_target_profile_falls_back_to_user_endpoint_for_departed():
    rest = Mock()
    rest.fetch_member = AsyncMock(
        side_effect=hikari.NotFoundError(url="u", headers={}, raw_body=b"")
    )
    rest.fetch_user = AsyncMock(
        return_value=SimpleNamespace(id=222, username="departed")
    )

    profile = await resolve_target_profile(rest, 111, 222)

    rest.fetch_user.assert_awaited_once_with(222)
    assert profile == TargetProfile(
        user_id="222",
        username="departed",
        joined_at=None,
        is_guild_member=False,
        has_accepted_rules=None,
    )


# --------------------------------------------------------------------------- #
# /history command
# --------------------------------------------------------------------------- #


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


def _ctx(*, target_user=None, member=None, member_missing=False):
    target_user = target_user or SimpleNamespace(
        id=222, username="offender", mention="<@222>"
    )
    ctx = Mock()
    ctx.options = SimpleNamespace(user=target_user)
    ctx.member = Mock(spec=hikari.InteractionMember)
    ctx.channel_id = 555
    ctx.respond = AsyncMock()
    ctx.author = SimpleNamespace(id=999, username="mod")

    guild = Mock()
    guild.id = 111
    ctx.get_guild = Mock(return_value=guild)

    ctx.bot = Mock()
    ctx.bot.rest = Mock()
    if member_missing:
        ctx.bot.rest.fetch_member = AsyncMock(
            side_effect=hikari.NotFoundError(url="u", headers={}, raw_body=b"")
        )
    else:
        ctx.bot.rest.fetch_member = AsyncMock(
            return_value=member
            or SimpleNamespace(
                id=222,
                username="offender",
                joined_at=NOW - timedelta(days=90),
                is_pending=False,
            )
        )
    ctx.bot.rest.fetch_user = AsyncMock(
        return_value=SimpleNamespace(id=222, username="departed")
    )
    return ctx, guild


def _patch_history_read(actions):
    session = AsyncMock()
    read_calls = {}

    async def _get_actions_for_user(sess, guild_id, target_user_id, limit=50):
        read_calls.update(
            guild_id=guild_id, target_user_id=target_user_id, limit=limit
        )
        return list(actions)

    return session, read_calls, _get_actions_for_user


def _run_history(ctx, actions, permissions=MODERATE):
    session, read_calls, get_actions = _patch_history_read(actions)
    patches = (
        patch(PERMS_TARGET, return_value=permissions),
        patch.object(
            history_plugin,
            "get_db_session_context",
            return_value=_FakeSessionCtx(session),
        ),
        patch.object(
            history_plugin.mod_action_ops,
            "get_actions_for_user",
            side_effect=get_actions,
        ),
    )
    return patches, read_calls


def _action(
    *,
    action_type="warn",
    created_at=NOW - timedelta(days=1),
    reason="spamming",
    source="manual",
    channel_id="555",
    trigger_message_id="777",
):
    return SimpleNamespace(
        action_type=action_type,
        created_at=created_at,
        reason=reason,
        source=source,
        channel_id=channel_id,
        trigger_message_id=trigger_message_id,
    )


async def test_denies_without_moderate_members():
    ctx, _ = _ctx()
    patches, read_calls = _run_history(ctx, [], permissions=NONE)
    with patches[0], patches[1], patches[2]:
        await history_plugin.history(ctx)

    ctx.respond.assert_awaited_once()
    _, kwargs = ctx.respond.call_args
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert read_calls == {}
    ctx.bot.rest.fetch_member.assert_not_awaited()


async def test_denies_outside_a_guild():
    ctx, _ = _ctx()
    ctx.member = None
    patches, read_calls = _run_history(ctx, [])
    with patches[0], patches[1], patches[2]:
        await history_plugin.history(ctx)

    ctx.respond.assert_awaited_once()
    _, kwargs = ctx.respond.call_args
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert read_calls == {}


async def test_reports_empty_history_ephemerally():
    ctx, _ = _ctx()
    patches, read_calls = _run_history(ctx, [])
    with patches[0], patches[1], patches[2]:
        await history_plugin.history(ctx)

    message = ctx.respond.await_args.args[0]
    assert "No moderation history" in message
    assert "offender" in message
    assert ctx.respond.await_args.kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert read_calls == {
        "guild_id": "111",
        "target_user_id": "222",
        "limit": MODERATION_HISTORY_DEPTH,
    }


async def test_renders_history_for_a_departed_user():
    ctx, _ = _ctx(member_missing=True)
    patches, _ = _run_history(ctx, [_action()])
    with patches[0], patches[1], patches[2]:
        await history_plugin.history(ctx)

    message = ctx.respond.await_args.args[0]
    assert "NO LONGER A MEMBER" in message
    assert "departed" in message
    assert "warn" in message
    ctx.bot.rest.fetch_user.assert_awaited_once_with(222)


async def test_renders_jump_links_only_for_rows_with_both_ids():
    ctx, _ = _ctx()
    actions = [
        _action(action_type="ban", channel_id="555", trigger_message_id="777"),
        _action(action_type="kick", channel_id=None, trigger_message_id="888"),
        _action(action_type="timeout", channel_id="666", trigger_message_id=None),
    ]
    patches, _ = _run_history(ctx, actions)
    with patches[0], patches[1], patches[2]:
        await history_plugin.history(ctx)

    message = ctx.respond.await_args.args[0]
    assert message.count("Jump To Action") == 1
    assert "https://discord.com/channels/111/555/777" in message
    assert "kick" in message
    assert "timeout" in message


async def test_response_cannot_ping_with_hostile_username_and_reason():
    hostile_member = SimpleNamespace(
        id=222,
        username="evil@here",
        joined_at=NOW - timedelta(days=90),
        is_pending=False,
    )
    ctx, _ = _ctx(member=hostile_member)
    patches, _ = _run_history(ctx, [_action(reason="mass pinged @everyone")])
    with patches[0], patches[1], patches[2]:
        await history_plugin.history(ctx)

    message = ctx.respond.await_args.args[0]
    kwargs = ctx.respond.await_args.kwargs
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert kwargs["mentions_everyone"] is False
    assert kwargs["user_mentions"] is False
    assert kwargs["role_mentions"] is False
    assert "@everyone" not in message
    assert "@here" not in message
    assert f"evil@{ZERO_WIDTH_SPACE}here" in message
    assert f"mass pinged @{ZERO_WIDTH_SPACE}everyone" in message


async def test_truncates_long_history_with_older_actions_tail():
    ctx, _ = _ctx()
    actions = [
        _action(reason="y" * 140, created_at=NOW - timedelta(days=n))
        for n in range(1, 51)
    ]
    patches, _ = _run_history(ctx, actions)
    with patches[0], patches[1], patches[2]:
        await history_plugin.history(ctx)

    message = ctx.respond.await_args.args[0]
    assert len(message) <= DISCORD_MESSAGE_CHAR_LIMIT
    rendered_rows = message.count("Jump To Action")
    assert f"…and {len(actions) - rendered_rows} older actions" in message
