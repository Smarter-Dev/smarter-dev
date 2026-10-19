"""The 2026-10-18 17:49 UTC incident, replayed through the gateway.

One scam post by a two-day-old account. In production the spam engine timed
the author out for a day and posted its alert; four seconds later the
scam-banner handler banned them and posted a second report, and neither the
ban nor the delete reached ``moderation_actions``.

Here the real spam engine and the real scam-banner script (the reference copy
in ``scripts/handler_scripts``, run in the real handler runtime) act on one
fake Discord through the moderation gateway, over a real database. Whatever
the order, and when they race, the incident must end with exactly one ban, one
deletion, one report and one row per confirmed effect. The audit log's echoes
of the bot's own ban and timeout are then linked to those rows, not recorded
again.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from functools import partial
from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_aioredis
import hikari
import pytest
from sqlalchemy import select

from smarter_dev.bot import audit_logger
from smarter_dev.bot import spam_engine
from smarter_dev.web.admin_actions import AdminActor
from smarter_dev.web.admin_actions import HandlerFireFacts
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_caps import claim_handler_key
from smarter_dev.web.handler_caps import handler_key_claimed
from smarter_dev.web.handler_holds import MemberHolds
from smarter_dev.web.handler_recent_messages import content_hash
from smarter_dev.web.handler_runtime import run_handler_script
from smarter_dev.web.handler_script_services import _mod_action_row
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.models import ModerationIncident
from smarter_dev.web.moderation_gateway import ModerationGateway
from smarter_dev.web.moderation_gateway.locks import MemberLocks
from smarter_dev.web.moderation_gateway.locks import _LocalLocks
from tests.bot.fake_discord import BOT_ID as FAKE_BOT_ID
from tests.bot.fake_discord import FakeDiscord
from tests.moderation_fakes import GUILD
from tests.moderation_fakes import REPORT_CHANNEL
from tests.moderation_fakes import FakeModerationDiscord
from tests.web.scam_banner_script_test import SCRIPT
from tests.web.scam_banner_script_test import _Emitter
from tests.web.scam_banner_script_test import _Limiter

SCAMMER = "1428776400000000001"
CHANNEL = "702178352395583498"
MESSAGE = "1428776500000000001"
MOD_ROLE = "644390354157568014"
HANDLER = "scam-banner-id"
SCAM = "@everyone free nitro giveaway, claim it https://dlscord.gift/abc123"


def _ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


class _World:
    """One Discord, one database; the bot and the handler worker act on it."""

    def __init__(self, database) -> None:
        self.database = database
        self.discord = FakeModerationDiscord(guild_id=GUILD)
        self.discord.add_member(SCAMMER)
        self.discord.post(CHANNEL, MESSAGE, SCAMMER, SCAM)
        self.redis = fakeredis_aioredis.FakeRedis()
        self.handler_messages = _Emitter()
        self.reviews: list[str] = []

    def gateway(self, *, process: _LocalLocks) -> ModerationGateway:
        """A gateway with its own process's lock queue, like a separate pod."""
        return ModerationGateway(
            self.discord, locks=MemberLocks(self.database, local=process)
        )

    async def rows(self) -> list[ModerationAction]:
        async with self.database() as session:
            return list(
                (
                    await session.execute(
                        select(ModerationAction).order_by(ModerationAction.created_at)
                    )
                ).scalars()
            )

    async def history(self, user_id, limit):
        """list_mod_actions over the real records, as the fire job reads them."""
        rows = []
        for row in reversed(await self.rows()):
            # SQLite hands timestamps back naive; Postgres keeps the zone.
            if row.created_at.tzinfo is None:
                row.created_at = row.created_at.replace(tzinfo=UTC)
            rows.append(_mod_action_row(row))
        return rows[:limit]

    async def agent(self, prompt, has_tools, budget):
        if "UNTRUSTED RECORD" in prompt:
            return "Timed out by the spam engine moments ago for the same post."
        self.reviews.append(prompt)
        return "VIOLATION: nitro scam with a lookalike gift link"

    def report_posts(self) -> list[dict]:
        return self.discord.reports(REPORT_CHANNEL)


@pytest.fixture
def world(moderation_gateway_db, monkeypatch) -> _World:
    world = _World(moderation_gateway_db)

    config = ModerationFilterConfig(
        guild_id=GUILD,
        spam_engine_enabled=True,
        mod_alert_channel_id=REPORT_CHANNEL,
        mod_ping_role_id=MOD_ROLE,
        mute_duration_seconds=86400,
    )

    async def get_config(session, guild_id):
        return config

    @contextlib.asynccontextmanager
    async def no_session():
        yield SimpleNamespace()

    async def announce(action):
        return None

    monkeypatch.setattr(
        spam_engine.moderation_filter_config_ops, "get_config", get_config
    )
    monkeypatch.setattr(spam_engine, "get_db_session_context", no_session)
    monkeypatch.setattr(world.discord, "announce", announce)
    spam_engine.reset_spam_states()
    return world


async def _spam_engine(world: _World, process: _LocalLocks) -> None:
    """The bot: its spam engine sees the post."""
    bot = FakeDiscord(guild_id=int(GUILD))
    bot.add_member(int(SCAMMER), username="nitro_giveaway")
    gateway = world.gateway(process=process)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            spam_engine, "bot_gateway", lambda bot, extra_role_ids=(): gateway
        )
        await spam_engine.check_spam_engine(
            bot,
            SimpleNamespace(
                guild_id=int(GUILD),
                channel_id=int(CHANNEL),
                message_id=int(MESSAGE),
                content=SCAM,
                author=SimpleNamespace(
                    id=int(SCAMMER), username="nitro_giveaway", is_bot=False
                ),
                member=SimpleNamespace(id=int(SCAMMER), role_ids=[], is_bot=False),
            ),
        )


async def _scam_banner(world: _World, process: _LocalLocks):
    """The handler worker: scam-banner fires for the same post."""
    context = {
        "trigger_type": "message",
        "guild_id": GUILD,
        "message_id": MESSAGE,
        "message_content": SCAM,
        "author_id": SCAMMER,
        "author_has_manage_messages": False,
        "author_account_created_at": _ago(days=2),
        "author_joined_at": _ago(hours=1),
        "author_is_first_message": True,
        "author_days_since_last_message": 0,
        "attachments": [],
        "is_thread": False,
    }
    row = {
        "channel_id": CHANNEL,
        "message_id": MESSAGE,
        "age_seconds": 4.0,
        "attachment_count": 0,
        "has_link": True,
        "content_hash": content_hash(context),
    }

    async def recent(user_id):
        return [row]

    async def count_warns(user_id):
        return 0

    async def report_channel():
        return REPORT_CHANNEL

    async def no_timer(fire_at, refire_context):
        return None

    actor = AdminActor(
        bot_token="t",
        guild_id=GUILD,
        moderation_port=world.discord,
        fire=HandlerFireFacts(
            handler_id=HANDLER,
            handler_name="scam-banner",
            channel_id=CHANNEL,
            message_id=MESSAGE,
            author_id=SCAMMER,
            content=SCAM,
        ),
    )
    actor._gateway = world.gateway(process=process)
    return await run_handler_script(
        SCRIPT,
        context,
        channel_id=CHANNEL,
        guild_id=GUILD,
        emitter=world.handler_messages,
        limiter=_Limiter(),
        agent_runner=world.agent,
        mod_action_reader=world.history,
        warn_counter=count_warns,
        report_channel_reader=report_channel,
        recent_messages_reader=recent,
        handler_id=HANDLER,
        timer_scheduler=no_timer,
        claimer=partial(claim_handler_key, world.redis, HANDLER),
        claim_reader=partial(handler_key_claimed, world.redis, HANDLER),
        holds=MemberHolds(
            redis=world.redis, guild_id=GUILD, handler_id=HANDLER, actor=actor
        ),
        budget=admin_budget("message"),
        actor=actor,
    )


async def _assert_one_action_one_report(world: _World) -> list[ModerationAction]:
    assert world.discord.count("ban") == 1
    assert world.discord.count("delete") == 1
    assert world.discord.count("timeout") <= 1
    assert SCAMMER in world.discord.bans

    rows = await world.rows()
    kinds = sorted(row.action_type for row in rows)
    assert kinds.count("ban") == 1
    assert kinds.count("delete") == 1
    assert set(kinds) <= {"ban", "delete", "timeout"}
    assert len({row.incident_id for row in rows}) == 1
    assert all(row.operation_id is not None for row in rows)
    async with world.database() as session:
        incidents = (await session.execute(select(ModerationIncident))).scalars().all()
    assert len(incidents) == 1

    # One report, in the moderators' channel; the handler posted none of its own.
    assert len(world.report_posts()) == 1
    assert not [m for m in world.handler_messages.messages if m[0] == REPORT_CHANNEL]
    report = world.discord.latest_report_text()
    assert "ban: confirmed" in report
    assert "scam-banner" in report and "spam engine" in report, report
    assert "https://dlscord.gift" not in report
    return rows


async def test_the_1749_incident_in_production_order(world):
    """Spam engine first; four seconds later, scam-banner."""
    await _spam_engine(world, _LocalLocks())
    assert world.discord.count("timeout") == 1
    assert len(world.report_posts()) == 1
    assert world.report_posts()[0]["role_mention_ids"] == [MOD_ROLE]

    result = await _scam_banner(world, _LocalLocks())

    assert result.outcome == "ok", result.error
    assert len(world.reviews) == 1
    rows = await _assert_one_action_one_report(world)
    assert sorted(row.action_type for row in rows) == ["ban", "delete", "timeout"]
    by_type = {row.action_type: row for row in rows}
    assert by_type["timeout"].actor == "spam_engine"
    assert by_type["timeout"].applied_duration_seconds in range(86390, 86401)
    assert by_type["delete"].actor == "spam_engine"
    assert by_type["ban"].actor == "handler:scam-banner"
    assert by_type["ban"].moderator_username == "handler:scam-banner"
    # The handler's report became a note on the one report.
    assert "Automated Scam/Abuse Action" in world.discord.latest_report_text()
    # Its history read already saw the spam engine's timeout.
    assert "spam engine moments ago" in world.discord.latest_report_text()


async def test_the_1749_incident_with_the_handler_first(world):
    result = await _scam_banner(world, _LocalLocks())
    assert result.outcome == "ok", result.error

    await _spam_engine(world, _LocalLocks())

    rows = await _assert_one_action_one_report(world)
    # The member was banned first: the engine's timeout had nothing to land on.
    assert sorted(row.action_type for row in rows) == ["ban", "delete"]
    assert world.discord.count("timeout") == 0


async def test_the_1749_incident_with_both_detectors_racing(world):
    """The two processes act on the same post within milliseconds."""
    _, result = await asyncio.gather(
        _spam_engine(world, _LocalLocks()), _scam_banner(world, _LocalLocks())
    )

    assert result.outcome == "ok", result.error
    await _assert_one_action_one_report(world)


# -- the audit log's echoes ---------------------------------------------------------


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


async def test_the_bots_own_audit_entries_are_linked_not_recorded_again(
    world, monkeypatch
):
    await _spam_engine(world, _LocalLocks())
    await _scam_banner(world, _LocalLocks())
    before = await world.rows()

    async def yes(*args, **kwargs):
        return True

    async def nothing(*args, **kwargs):
        return None

    created: list = []

    async def create_action(session, **kwargs):
        created.append(kwargs)

    @contextlib.asynccontextmanager
    async def no_session():
        yield SimpleNamespace(commit=nothing)

    monkeypatch.setattr(audit_logger, "should_log_event", yes)
    monkeypatch.setattr(audit_logger, "send_audit_log", nothing)
    monkeypatch.setattr(audit_logger, "get_db_session_context", no_session)
    monkeypatch.setattr(audit_logger.mod_action_ops, "create_action", create_action)

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
    after = {row.action_type: row for row in await world.rows()}
    assert len(after) == len(before)
    assert after["ban"].audit_entry_id == "9001"
    assert after["timeout"].audit_entry_id == "9000"


async def test_a_moderators_ban_keeps_their_name_and_entry(world, monkeypatch):
    async def yes(*args, **kwargs):
        return True

    async def nothing(*args, **kwargs):
        return None

    created: list = []

    async def create_action(session, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(**kwargs)

    @contextlib.asynccontextmanager
    async def no_session():
        yield SimpleNamespace(commit=nothing)

    monkeypatch.setattr(audit_logger, "should_log_event", yes)
    monkeypatch.setattr(audit_logger, "send_audit_log", nothing)
    monkeypatch.setattr(audit_logger, "get_db_session_context", no_session)
    monkeypatch.setattr(audit_logger.mod_action_ops, "create_action", create_action)
    monkeypatch.setattr(audit_logger, "dispatch_mod_action", nothing)

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
