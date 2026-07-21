"""Tests for the /purge slash command and its shared paging/bulk-delete core."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import hikari
import pytest

from smarter_dev.bot.plugins import purge as purge_plugin
from smarter_dev.bot.purge_core import (
    DISCORD_EPOCH_MS,
    PurgeSelection,
    delete_selected_messages,
    select_purgeable_messages,
)

PERMS_TARGET = "lightbulb.utils.permissions_for"

MANAGE = hikari.Permissions.MANAGE_MESSAGES
ADMIN = hikari.Permissions.ADMINISTRATOR
NONE = hikari.Permissions.NONE


def _snowflake_for(dt: datetime) -> int:
    """Build a Discord snowflake whose creation time is ``dt``."""
    return (int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


class _AsyncIter:
    """Minimal hikari LazyIterator stand-in with a chainable ``.limit``."""

    def __init__(self, items):
        self._items = list(items)

    def limit(self, n):
        return _AsyncIter(self._items[:n])

    def __aiter__(self):
        async def _gen():
            for item in self._items:
                yield item

        return _gen()


def _message(message_id: int, author_id: str):
    return SimpleNamespace(id=message_id, author=SimpleNamespace(id=author_id))


# --------------------------------------------------------------------------- #
# Shared core: select_purgeable_messages / delete_selected_messages
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_select_filters_by_user_and_stops_at_count():
    now = datetime.now(timezone.utc)
    recent = _snowflake_for(now - timedelta(minutes=1))
    messages = [
        _message(recent + 5, "A"),
        _message(recent + 4, "B"),
        _message(recent + 3, "A"),
        _message(recent + 2, "A"),
        _message(recent + 1, "A"),
    ]
    selection = await select_purgeable_messages(
        _AsyncIter(messages), count=2, user_id="A", now=now
    )
    assert selection.message_ids == [recent + 5, recent + 3]
    assert selection.skipped_too_old == 0


@pytest.mark.asyncio
async def test_select_skips_and_counts_messages_older_than_14_days():
    now = datetime.now(timezone.utc)
    recent = _snowflake_for(now - timedelta(minutes=1))
    old = _snowflake_for(now - timedelta(days=20))
    messages = [
        _message(recent + 2, "A"),
        _message(old + 1, "A"),
        _message(old, "A"),
        _message(recent + 1, "A"),
    ]
    selection = await select_purgeable_messages(
        _AsyncIter(messages), count=50, user_id="A", now=now
    )
    assert selection.message_ids == [recent + 2, recent + 1]
    assert selection.skipped_too_old == 2


@pytest.mark.asyncio
async def test_delete_selected_uses_single_delete_for_one_message():
    rest = Mock()
    rest.delete_message = AsyncMock()
    rest.delete_messages = AsyncMock()
    await delete_selected_messages(rest, 42, [100])
    rest.delete_message.assert_awaited_once_with(42, 100)
    rest.delete_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_selected_uses_bulk_delete_for_many_and_noop_for_empty():
    rest = Mock()
    rest.delete_message = AsyncMock()
    rest.delete_messages = AsyncMock()

    await delete_selected_messages(rest, 42, [1, 2, 3])
    rest.delete_messages.assert_awaited_once_with(42, [1, 2, 3])
    rest.delete_message.assert_not_awaited()

    rest.delete_messages.reset_mock()
    await delete_selected_messages(rest, 42, [])
    rest.delete_messages.assert_not_awaited()
    rest.delete_message.assert_not_awaited()


# --------------------------------------------------------------------------- #
# /purge command
# --------------------------------------------------------------------------- #


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *args):
        return False


def _ctx(*, count: int, messages=None, target_user=None, channel_name="general"):
    ctx = Mock()
    ctx.options = SimpleNamespace(count=count, user=target_user)
    ctx.member = Mock(spec=hikari.InteractionMember)
    ctx.channel_id = 555
    ctx.respond = AsyncMock()
    ctx.author = SimpleNamespace(id=999, username="mod")

    channel = Mock()
    channel.name = channel_name

    guild = Mock()
    guild.id = 111
    guild.get_channel = Mock(return_value=channel)
    ctx.get_guild = Mock(return_value=guild)

    ctx.bot = Mock()
    ctx.bot.rest = Mock()
    ctx.bot.rest.fetch_messages = Mock(return_value=_AsyncIter(messages or []))
    ctx.bot.rest.delete_message = AsyncMock()
    ctx.bot.rest.delete_messages = AsyncMock()
    ctx.bot.rest.fetch_member = AsyncMock()
    return ctx, guild


def _patch_audit():
    session = AsyncMock()
    created = {}

    async def _create_action(sess, **kwargs):
        created.update(kwargs)
        return SimpleNamespace(**kwargs)

    dispatch = AsyncMock()
    return session, created, _create_action, dispatch


async def test_denies_without_manage_messages():
    ctx, _ = _ctx(count=5)
    with patch(PERMS_TARGET, return_value=NONE):
        await purge_plugin.purge(ctx)

    ctx.respond.assert_awaited_once()
    _, kwargs = ctx.respond.call_args
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    ctx.bot.rest.fetch_messages.assert_not_called()


@pytest.mark.parametrize("bad_count", [0, 101])
async def test_rejects_out_of_range_count(bad_count):
    ctx, _ = _ctx(count=bad_count)
    with patch(PERMS_TARGET, return_value=MANAGE):
        await purge_plugin.purge(ctx)

    ctx.respond.assert_awaited_once()
    ctx.bot.rest.fetch_messages.assert_not_called()


async def test_per_user_guard_blocks_privileged_target_without_admin():
    target = SimpleNamespace(id=222, username="modtarget", mention="<@222>")
    ctx, guild = _ctx(count=5, target_user=target)
    target_member = Mock(spec=hikari.Member)
    target_member.is_bot = False
    guild.get_member = Mock(return_value=target_member)

    # Invoker has MANAGE (not ADMIN); target has MANAGE.
    with patch(PERMS_TARGET, side_effect=[MANAGE, MANAGE]):
        await purge_plugin.purge(ctx)

    ctx.respond.assert_awaited_once()
    _, kwargs = ctx.respond.call_args
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    ctx.bot.rest.fetch_messages.assert_not_called()


async def test_per_user_guard_allows_privileged_target_with_admin():
    now = datetime.now(timezone.utc)
    recent = _snowflake_for(now - timedelta(minutes=1))
    target = SimpleNamespace(id=222, username="modtarget", mention="<@222>")
    messages = [_message(recent + 2, "222"), _message(recent + 1, "222")]
    ctx, guild = _ctx(count=5, target_user=target, messages=messages)
    target_member = Mock(spec=hikari.Member)
    target_member.is_bot = False
    guild.get_member = Mock(return_value=target_member)

    session, created, create_action, dispatch = _patch_audit()
    # lightbulb.utils.permissions_for expands ADMINISTRATOR to all permissions,
    # so the invoker's resolved perms include MANAGE_MESSAGES.
    with patch(PERMS_TARGET, side_effect=[ADMIN | MANAGE, MANAGE]), \
        patch.object(purge_plugin, "get_db_session_context", return_value=_FakeSessionCtx(session)), \
        patch.object(purge_plugin.mod_action_ops, "create_action", side_effect=create_action), \
        patch.object(purge_plugin, "dispatch_mod_action", dispatch):
        await purge_plugin.purge(ctx)

    ctx.bot.rest.delete_messages.assert_awaited_once_with(555, [recent + 2, recent + 1])
    assert created["action_type"] == "purge"
    assert created["target_user_id"] == "222"
    dispatch.assert_awaited_once()


async def test_per_user_guard_exempts_bot_targets():
    now = datetime.now(timezone.utc)
    recent = _snowflake_for(now - timedelta(minutes=1))
    target = SimpleNamespace(id=333, username="spambot", mention="<@333>")
    messages = [_message(recent + 2, "333"), _message(recent + 1, "333")]
    ctx, guild = _ctx(count=5, target_user=target, messages=messages)
    target_member = Mock(spec=hikari.Member)
    target_member.is_bot = True
    guild.get_member = Mock(return_value=target_member)

    session, created, create_action, dispatch = _patch_audit()
    # Invoker only MANAGE, target (a bot) has MANAGE — guard must NOT block.
    with patch(PERMS_TARGET, side_effect=[MANAGE, MANAGE]), \
        patch.object(purge_plugin, "get_db_session_context", return_value=_FakeSessionCtx(session)), \
        patch.object(purge_plugin.mod_action_ops, "create_action", side_effect=create_action), \
        patch.object(purge_plugin, "dispatch_mod_action", dispatch):
        await purge_plugin.purge(ctx)

    ctx.bot.rest.delete_messages.assert_awaited_once()
    assert created["action_type"] == "purge"


async def test_count_mode_writes_audit_row_and_fires_dispatch():
    now = datetime.now(timezone.utc)
    recent = _snowflake_for(now - timedelta(minutes=1))
    messages = [_message(recent + 3, "A"), _message(recent + 2, "B"), _message(recent + 1, "A")]
    ctx, guild = _ctx(count=3, messages=messages, channel_name="general")

    session, created, create_action, dispatch = _patch_audit()
    with patch(PERMS_TARGET, return_value=MANAGE), \
        patch.object(purge_plugin, "get_db_session_context", return_value=_FakeSessionCtx(session)), \
        patch.object(purge_plugin.mod_action_ops, "create_action", side_effect=create_action), \
        patch.object(purge_plugin, "dispatch_mod_action", dispatch):
        await purge_plugin.purge(ctx)

    # No user filter → all three deleted via bulk delete.
    ctx.bot.rest.delete_messages.assert_awaited_once_with(
        555, [recent + 3, recent + 2, recent + 1]
    )
    assert created["action_type"] == "purge"
    assert created["source"] == "manual"
    assert created["reason"] == "purged 3 messages in #general"
    assert created["moderator_user_id"] == "999"
    session.commit.assert_awaited()
    dispatch.assert_awaited_once()


async def test_reports_shortfall_for_messages_older_than_14_days():
    now = datetime.now(timezone.utc)
    recent = _snowflake_for(now - timedelta(minutes=1))
    old = _snowflake_for(now - timedelta(days=20))
    messages = [
        _message(recent + 2, "A"),
        _message(old + 1, "A"),
        _message(old, "A"),
        _message(recent + 1, "A"),
    ]
    ctx, guild = _ctx(count=10, messages=messages)

    session, created, create_action, dispatch = _patch_audit()
    with patch(PERMS_TARGET, return_value=MANAGE), \
        patch.object(purge_plugin, "get_db_session_context", return_value=_FakeSessionCtx(session)), \
        patch.object(purge_plugin.mod_action_ops, "create_action", side_effect=create_action), \
        patch.object(purge_plugin, "dispatch_mod_action", dispatch):
        await purge_plugin.purge(ctx)

    ctx.bot.rest.delete_messages.assert_awaited_once_with(555, [recent + 2, recent + 1])
    confirmation = ctx.respond.await_args.args[0]
    assert "Purged 2 message" in confirmation
    assert "Skipped 2" in confirmation
    assert "14 days" in confirmation
    assert created["reason"] == "purged 2 messages in #general"


async def test_forbidden_delete_fails_loud_without_audit_row():
    now = datetime.now(timezone.utc)
    recent = _snowflake_for(now - timedelta(minutes=1))
    messages = [_message(recent + 2, "A"), _message(recent + 1, "A")]
    ctx, guild = _ctx(count=5, messages=messages)
    ctx.bot.rest.delete_messages = AsyncMock(
        side_effect=hikari.ForbiddenError(url="u", headers={}, raw_body=b"")
    )

    session, created, create_action, dispatch = _patch_audit()
    with patch(PERMS_TARGET, return_value=MANAGE), \
        patch.object(purge_plugin, "get_db_session_context", return_value=_FakeSessionCtx(session)), \
        patch.object(purge_plugin.mod_action_ops, "create_action", side_effect=create_action), \
        patch.object(purge_plugin, "dispatch_mod_action", dispatch):
        await purge_plugin.purge(ctx)

    # Failed the delete → ephemeral error, no audit row, no dispatch.
    _, kwargs = ctx.respond.call_args
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert created == {}
    dispatch.assert_not_awaited()


async def test_notfound_delete_fails_loud_without_audit_row():
    now = datetime.now(timezone.utc)
    recent = _snowflake_for(now - timedelta(minutes=1))
    messages = [_message(recent + 1, "A")]  # single message → single delete
    ctx, guild = _ctx(count=5, messages=messages)
    ctx.bot.rest.delete_message = AsyncMock(
        side_effect=hikari.NotFoundError(url="u", headers={}, raw_body=b"")
    )

    session, created, create_action, dispatch = _patch_audit()
    with patch(PERMS_TARGET, return_value=MANAGE), \
        patch.object(purge_plugin, "get_db_session_context", return_value=_FakeSessionCtx(session)), \
        patch.object(purge_plugin.mod_action_ops, "create_action", side_effect=create_action), \
        patch.object(purge_plugin, "dispatch_mod_action", dispatch):
        await purge_plugin.purge(ctx)

    # A message vanished mid-action → ephemeral error, no audit row, no dispatch.
    _, kwargs = ctx.respond.call_args
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert created == {}
    dispatch.assert_not_awaited()


async def test_zero_messages_writes_no_audit_row_and_no_dispatch():
    # Count mode but nothing matches (empty history) → confirm 0, but write no
    # ModerationAction and fire no mod_action for an action that didn't happen.
    ctx, guild = _ctx(count=10, messages=[])

    session, created, create_action, dispatch = _patch_audit()
    with patch(PERMS_TARGET, return_value=MANAGE), \
        patch.object(purge_plugin, "get_db_session_context", return_value=_FakeSessionCtx(session)), \
        patch.object(purge_plugin.mod_action_ops, "create_action", side_effect=create_action), \
        patch.object(purge_plugin, "dispatch_mod_action", dispatch):
        await purge_plugin.purge(ctx)

    ctx.bot.rest.delete_messages.assert_not_awaited()
    ctx.bot.rest.delete_message.assert_not_awaited()
    confirmation = ctx.respond.await_args.args[0]
    assert "Purged 0 messages" in confirmation
    assert created == {}
    dispatch.assert_not_awaited()
    session.commit.assert_not_awaited()
