"""Tests for audit-log ingestion of native Discord moderation actions.

Every native mod action (ban/unban/kick/timeout/untimeout performed by a human
moderator or another bot) must become a ``ModerationAction`` row and fire the
``mod_action`` trigger. Actions the bot itself performed are already recorded by
the mod tools, so ingestion must skip them to avoid double-recording.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import hikari
import pytest

from smarter_dev.bot import audit_logger

BOT_ID = 999
GUILD_ID = 555
TARGET_ID = 111
MOD_ID = 222


async def _async_noop(*args, **kwargs) -> None:
    return None


class _AsyncEntryIterator:
    def __init__(self, entries: list) -> None:
        self._entries = list(entries)

    def __aiter__(self) -> "_AsyncEntryIterator":
        return self

    async def __anext__(self):
        if not self._entries:
            raise StopAsyncIteration
        return self._entries.pop(0)


class _AuditLogQuery:
    def __init__(self, entries: list) -> None:
        self._entries = entries

    def limit(self, n: int) -> _AsyncEntryIterator:
        return _AsyncEntryIterator(self._entries[:n])


class _FakeRest:
    def __init__(
        self,
        entries_by_type: dict,
        users: dict,
        fetch_raises: bool = False,
    ) -> None:
        self._entries_by_type = entries_by_type
        self._users = users
        self._fetch_raises = fetch_raises

    def fetch_audit_log(self, guild_id, event_type=None) -> _AuditLogQuery:
        if self._fetch_raises:
            raise RuntimeError("audit log fetch failed")
        return _AuditLogQuery(self._entries_by_type.get(event_type, []))

    async def fetch_user(self, user_id):
        return self._users[user_id]


class _FakeBot:
    def __init__(self, rest: _FakeRest) -> None:
        self.rest = rest

    def get_me(self):
        return SimpleNamespace(id=BOT_ID)


def _audit_entry(target_id, user_id, reason="native reason", created_at=None):
    return SimpleNamespace(
        target_id=target_id,
        user_id=user_id,
        reason=reason,
        id=SimpleNamespace(created_at=created_at or datetime.now(UTC)),
    )


def _user(user_id, username):
    return SimpleNamespace(
        id=user_id,
        username=username,
        mention=f"<@{user_id}>",
        avatar_url=None,
    )


def _member(user_id, username, timeout_until):
    return SimpleNamespace(
        id=user_id,
        username=username,
        nickname=None,
        role_ids=[],
        communication_disabled_until=timeout_until,
        mention=f"<@{user_id}>",
        avatar_url=None,
    )


@pytest.fixture
def recorder(monkeypatch):
    created: list[dict] = []
    dispatched: list = []

    async def fake_create_action(session, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(**kwargs)

    async def fake_dispatch(action) -> None:
        dispatched.append(action)

    @contextlib.asynccontextmanager
    async def fake_ctx():
        yield SimpleNamespace(commit=_async_noop, add=lambda *a, **k: None, flush=_async_noop)

    async def true_should(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(audit_logger.mod_action_ops, "create_action", fake_create_action)
    monkeypatch.setattr(audit_logger, "dispatch_mod_action", fake_dispatch)
    monkeypatch.setattr(audit_logger, "get_db_session_context", fake_ctx)
    monkeypatch.setattr(audit_logger, "should_log_event", true_should)
    monkeypatch.setattr(audit_logger, "send_audit_log", _async_noop)

    return SimpleNamespace(created=created, dispatched=dispatched)


async def test_external_moderator_ban_recorded_and_dispatched(recorder):
    rest = _FakeRest(
        entries_by_type={
            hikari.AuditLogEventType.MEMBER_BAN_ADD: [
                _audit_entry(TARGET_ID, MOD_ID, reason="raiding"),
            ],
        },
        users={MOD_ID: _user(MOD_ID, "modcarol")},
    )
    event = SimpleNamespace(guild_id=GUILD_ID, user=_user(TARGET_ID, "baduser"))

    await audit_logger.log_member_ban(_FakeBot(rest), event)

    assert len(recorder.created) == 1
    row = recorder.created[0]
    assert row["action_type"] == "ban"
    assert row["source"] == "audit_log"
    assert row["target_user_id"] == str(TARGET_ID)
    assert row["target_username"] == "baduser"
    assert row["moderator_user_id"] == str(MOD_ID)
    assert row["moderator_username"] == "modcarol"
    assert row["reason"] == "raiding"
    assert len(recorder.dispatched) == 1


async def test_bot_performed_ban_skipped(recorder):
    rest = _FakeRest(
        entries_by_type={
            hikari.AuditLogEventType.MEMBER_BAN_ADD: [
                _audit_entry(TARGET_ID, BOT_ID, reason="handled by /ban"),
            ],
        },
        users={},
    )
    event = SimpleNamespace(guild_id=GUILD_ID, user=_user(TARGET_ID, "baduser"))

    await audit_logger.log_member_ban(_FakeBot(rest), event)

    assert recorder.created == []
    assert recorder.dispatched == []


async def test_kick_via_audit_log_on_member_leave(recorder):
    rest = _FakeRest(
        entries_by_type={
            hikari.AuditLogEventType.MEMBER_KICK: [
                _audit_entry(TARGET_ID, MOD_ID, reason="spam"),
            ],
        },
        users={MOD_ID: _user(MOD_ID, "modcarol")},
    )
    event = SimpleNamespace(guild_id=GUILD_ID, user=_user(TARGET_ID, "baduser"))

    await audit_logger.log_member_leave(_FakeBot(rest), event)

    assert len(recorder.created) == 1
    row = recorder.created[0]
    assert row["action_type"] == "kick"
    assert row["source"] == "audit_log"
    assert row["moderator_user_id"] == str(MOD_ID)
    assert row["reason"] == "spam"
    assert len(recorder.dispatched) == 1


async def test_timeout_add_recorded_with_duration(recorder):
    new_timeout = datetime.now(UTC) + timedelta(hours=1)
    rest = _FakeRest(
        entries_by_type={
            hikari.AuditLogEventType.MEMBER_UPDATE: [
                _audit_entry(TARGET_ID, MOD_ID, reason="cooldown"),
            ],
        },
        users={MOD_ID: _user(MOD_ID, "modcarol")},
    )
    event = SimpleNamespace(
        guild_id=GUILD_ID,
        member=_member(TARGET_ID, "baduser", new_timeout),
        old_member=_member(TARGET_ID, "baduser", None),
    )

    await audit_logger.log_member_update(_FakeBot(rest), event)

    assert len(recorder.created) == 1
    row = recorder.created[0]
    assert row["action_type"] == "timeout"
    assert row["source"] == "audit_log"
    assert row["moderator_user_id"] == str(MOD_ID)
    assert 3500 < row["duration_seconds"] <= 3600
    assert len(recorder.dispatched) == 1


async def test_timeout_removal_recorded_as_untimeout(recorder):
    old_timeout = datetime.now(UTC) + timedelta(hours=1)
    rest = _FakeRest(
        entries_by_type={
            hikari.AuditLogEventType.MEMBER_UPDATE: [
                _audit_entry(TARGET_ID, MOD_ID, reason="pardoned"),
            ],
        },
        users={MOD_ID: _user(MOD_ID, "modcarol")},
    )
    event = SimpleNamespace(
        guild_id=GUILD_ID,
        member=_member(TARGET_ID, "baduser", None),
        old_member=_member(TARGET_ID, "baduser", old_timeout),
    )

    await audit_logger.log_member_update(_FakeBot(rest), event)

    assert len(recorder.created) == 1
    row = recorder.created[0]
    assert row["action_type"] == "untimeout"
    assert row["source"] == "audit_log"
    assert row["moderator_user_id"] == str(MOD_ID)
    assert row["moderator_username"] == "modcarol"
    assert row["reason"] == "pardoned"
    assert len(recorder.dispatched) == 1


async def test_bot_performed_untimeout_skipped(recorder):
    old_timeout = datetime.now(UTC) + timedelta(hours=1)
    rest = _FakeRest(
        entries_by_type={
            hikari.AuditLogEventType.MEMBER_UPDATE: [
                _audit_entry(TARGET_ID, BOT_ID, reason="handled by tool"),
            ],
        },
        users={},
    )
    event = SimpleNamespace(
        guild_id=GUILD_ID,
        member=_member(TARGET_ID, "baduser", None),
        old_member=_member(TARGET_ID, "baduser", old_timeout),
    )

    await audit_logger.log_member_update(_FakeBot(rest), event)

    assert recorder.created == []
    assert recorder.dispatched == []


async def test_external_unban_recorded_and_dispatched(recorder):
    rest = _FakeRest(
        entries_by_type={
            hikari.AuditLogEventType.MEMBER_BAN_REMOVE: [
                _audit_entry(TARGET_ID, MOD_ID, reason="appeal granted"),
            ],
        },
        users={MOD_ID: _user(MOD_ID, "modcarol")},
    )
    event = SimpleNamespace(guild_id=GUILD_ID, user=_user(TARGET_ID, "baduser"))

    await audit_logger.log_member_unban(_FakeBot(rest), event)

    assert len(recorder.created) == 1
    row = recorder.created[0]
    assert row["action_type"] == "unban"
    assert row["source"] == "audit_log"
    assert row["moderator_user_id"] == str(MOD_ID)
    assert row["reason"] == "appeal granted"
    assert len(recorder.dispatched) == 1


async def test_audit_log_fetch_failure_records_nothing(recorder):
    rest = _FakeRest(entries_by_type={}, users={}, fetch_raises=True)
    event = SimpleNamespace(guild_id=GUILD_ID, user=_user(TARGET_ID, "baduser"))

    # Must not raise even though the audit-log fetch fails.
    await audit_logger.log_member_ban(_FakeBot(rest), event)

    assert recorder.created == []
    assert recorder.dispatched == []
