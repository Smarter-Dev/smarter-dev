"""The audit log's echoes of moderation, over a real database.

When the bot bans or times a member out through the moderation gateway,
Discord's audit log echoes the action back to ``audit_logger``. That echo is
linked to the gateway's own row (``link_audit_entry``), never recorded again;
a moderator's action by hand is recorded as theirs.

Kept from ``moderation_replay_test.py`` (the 2026-10-18 incident replayed
through the legacy spam engine and scam-banner), which went with those actors
after the #240 cutover; here the gateway acts directly.
"""

from __future__ import annotations

import contextlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import hikari
from sqlalchemy import select

from smarter_dev.bot import audit_logger
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.moderation_gateway import ModerationGateway
from smarter_dev.web.moderation_gateway import Proposal
from smarter_dev.web.moderation_gateway import Trigger
from smarter_dev.web.moderation_gateway import types as gw
from smarter_dev.web.moderation_gateway.locks import MemberLocks
from smarter_dev.web.moderation_gateway.locks import _LocalLocks
from tests.bot.fake_discord import BOT_ID as FAKE_BOT_ID
from tests.moderation_fakes import GUILD
from tests.moderation_fakes import REPORT_CHANNEL
from tests.moderation_fakes import FakeModerationDiscord

SCAMMER = "1428776400000000001"
CHANNEL = "702178352395583498"
MESSAGE = "1428776500000000001"
SCAM = "@everyone free nitro giveaway, claim it https://dlscord.gift/abc123"


class _EntryId(int):
    @property
    def created_at(self) -> datetime:
        return datetime.now(UTC)


class _AuditLog:
    def __init__(self, entries: dict) -> None:
        self._entries = entries

    def fetch_audit_log(self, guild_id, event_type=None):
        entries = self._entries.get(event_type, [])

        class _Query:
            def limit(self, n):
                async def pages():
                    if entries:
                        yield entries[:n]

                return pages()

        return _Query()

    async def fetch_user(self, user_id):
        return SimpleNamespace(id=user_id, username="someone")


async def _rows(database) -> list[ModerationAction]:
    async with database() as session:
        return list(
            (
                await session.execute(
                    select(ModerationAction).order_by(ModerationAction.created_at)
                )
            ).scalars()
        )


async def _bot_times_out_then_bans(database) -> None:
    """The bot's own timeout and ban, recorded by the gateway."""
    discord = FakeModerationDiscord(guild_id=GUILD)
    discord.add_member(SCAMMER)
    discord.post(CHANNEL, MESSAGE, SCAMMER, SCAM)

    async def announce(action):
        return None

    discord.announce = announce
    gateway = ModerationGateway(
        discord, locks=MemberLocks(database, local=_LocalLocks())
    )
    trigger = Trigger(
        guild_id=GUILD,
        actor="moderation_coordinator",
        reason="Auto-moderation violation",
        channel_id=CHANNEL,
        message_id=MESSAGE,
        author_id=SCAMMER,
        content=SCAM,
        report_channel_id=REPORT_CHANNEL,
    )
    await gateway.act(
        trigger,
        [
            Proposal(
                kind=gw.TIMEOUT,
                target_user_id=SCAMMER,
                duration_seconds=86400,
                reason="Auto-moderation violation",
            )
        ],
    )
    await gateway.act(
        trigger,
        [Proposal(kind=gw.BAN, target_user_id=SCAMMER, reason="Automated review")],
    )


def _quiet_audit_logger(monkeypatch, create_action) -> None:
    async def yes(*args, **kwargs):
        return True

    async def nothing(*args, **kwargs):
        return None

    @contextlib.asynccontextmanager
    async def no_session():
        yield SimpleNamespace(commit=nothing)

    monkeypatch.setattr(audit_logger, "should_log_event", yes)
    monkeypatch.setattr(audit_logger, "send_audit_log", nothing)
    monkeypatch.setattr(audit_logger, "get_db_session_context", no_session)
    monkeypatch.setattr(audit_logger.mod_action_ops, "create_action", create_action)
    monkeypatch.setattr(audit_logger, "dispatch_mod_action", nothing)


async def test_the_bots_own_audit_entries_are_linked_not_recorded_again(
    moderation_gateway_db, monkeypatch
):
    await _bot_times_out_then_bans(moderation_gateway_db)
    before = await _rows(moderation_gateway_db)
    assert sorted(row.action_type for row in before) == ["ban", "timeout"]

    created: list = []

    async def create_action(session, **kwargs):
        created.append(kwargs)

    _quiet_audit_logger(monkeypatch, create_action)

    timeout_change = SimpleNamespace(
        key=hikari.AuditLogChangeKey.COMMUNICATION_DISABLED_UNTIL
    )
    bot = SimpleNamespace(
        get_me=lambda: SimpleNamespace(id=FAKE_BOT_ID),
        rest=_AuditLog(
            {
                hikari.AuditLogEventType.MEMBER_BAN_ADD: [
                    SimpleNamespace(
                        id=_EntryId(9001),
                        target_id=int(SCAMMER),
                        user_id=FAKE_BOT_ID,
                        reason="Automated review",
                        changes=[],
                    )
                ],
                hikari.AuditLogEventType.MEMBER_UPDATE: [
                    SimpleNamespace(
                        id=_EntryId(9000),
                        target_id=int(SCAMMER),
                        user_id=FAKE_BOT_ID,
                        reason="Auto-moderation violation",
                        changes=[timeout_change],
                    )
                ],
            }
        ),
    )
    user = SimpleNamespace(
        id=int(SCAMMER), username="nitro_giveaway", mention="<@1>", avatar_url=None
    )
    await audit_logger.log_member_update(
        bot,
        SimpleNamespace(
            guild_id=int(GUILD),
            old_member=SimpleNamespace(
                communication_disabled_until=None,
                username="nitro_giveaway",
                nickname=None,
                role_ids=[],
            ),
            member=SimpleNamespace(
                id=int(SCAMMER),
                communication_disabled_until=datetime.now(UTC) + timedelta(days=1),
                username="nitro_giveaway",
                nickname=None,
                role_ids=[],
                mention="<@1>",
                avatar_url=None,
            ),
        ),
    )
    await audit_logger.log_member_ban(
        bot, SimpleNamespace(guild_id=int(GUILD), user=user)
    )

    assert created == []
    after = {row.action_type: row for row in await _rows(moderation_gateway_db)}
    assert len(after) == len(before)
    assert after["ban"].audit_entry_id == "9001"
    assert after["timeout"].audit_entry_id == "9000"


async def test_a_moderators_ban_keeps_their_name_and_entry(
    moderation_gateway_db, monkeypatch
):
    created: list = []

    async def create_action(session, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(**kwargs)

    _quiet_audit_logger(monkeypatch, create_action)

    bot = SimpleNamespace(
        get_me=lambda: SimpleNamespace(id=FAKE_BOT_ID),
        rest=_AuditLog(
            {
                hikari.AuditLogEventType.MEMBER_BAN_ADD: [
                    SimpleNamespace(
                        id=_EntryId(9100),
                        target_id=int(SCAMMER),
                        user_id=42,
                        reason="by hand",
                        changes=[],
                    )
                ]
            }
        ),
    )
    await audit_logger.log_member_ban(
        bot,
        SimpleNamespace(
            guild_id=int(GUILD),
            user=SimpleNamespace(
                id=int(SCAMMER), username="x", mention="<@1>", avatar_url=None
            ),
        ),
    )

    [row] = created
    assert row["moderator_user_id"] == "42"
    assert row["source"] == "audit_log"
    assert row["audit_entry_id"] == "9100"
