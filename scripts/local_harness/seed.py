"""Seed representative data into the harness databases.

Run as a subprocess by ``run.py`` with DATABASE_URL / LEGACY_DATABASE_URL
pointing at the harness postgres. Seeds:

- legacy DB (public schema): the bot-economy/admin tables plus a
  known-plaintext ``sk-`` API key the checks authenticate with.
- main DB (skrift schema): quests, feature flags, chat-agent engagements,
  member activity, and a channel handler.

The Skrift admin user is NOT seeded here — the checks log in through the dev
dummy auth provider, which creates the user and grants the admin role itself.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scripts.local_harness import config
from skrift.db.models.setting import Setting
from skrift.db.services.setting_service import (
    SETUP_COMPLETED_AT_KEY,
    SITE_NAME_KEY,
    SITE_THEME_KEY,
)
from smarter_dev.web.models import (
    AdventOfCodeConfig,
    CampaignSignup,
    AdventOfCodeThread,
    APIKey,
    AttachmentFilterConfig,
    AuditLogConfig,
    BytesBalance,
    BytesConfig,
    BytesTransaction,
    Campaign,
    Challenge,
    ChannelHandler,
    ChannelModelOverride,
    ChatAgentEngagement,
    DailyQuest,
    FeatureFlag,
    ForumAgent,
    ForumAgentResponse,
    ForumNotificationTopic,
    ForumUserSubscription,
    HelpConversation,
    MemberActivity,
    Quest,
    RepeatingMessage,
    ScheduledMessage,
    Squad,
    SquadMembership,
    SquadSaleEvent,
)
from smarter_dev.web.security import hash_api_key


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _legacy_rows() -> list[object]:
    now = _now()
    return [
        BytesConfig(
            guild_id=config.GUILD_ID,
            starting_balance=100,
            daily_amount=10,
        ),
        BytesBalance(
            guild_id=config.GUILD_ID,
            user_id=config.USER_ID,
            balance=1000,
            total_received=1000,
            streak_count=3,
            last_daily=date.today() - timedelta(days=1),
        ),
        BytesBalance(
            guild_id=config.GUILD_ID,
            user_id=config.JOINER_USER_ID,
            balance=500,
            total_received=500,
        ),
        BytesBalance(
            guild_id=config.GUILD_ID,
            user_id=config.DELETABLE_USER_ID,
            balance=10,
            total_received=10,
        ),
        BytesTransaction(
            guild_id=config.GUILD_ID,
            giver_id=config.USER_ID,
            giver_username=config.USER_NAME,
            receiver_id=config.JOINER_USER_ID,
            receiver_username=config.JOINER_USER_NAME,
            amount=25,
            reason="harness seed transfer",
        ),
        Squad(
            id=UUID(config.SQUAD_ID),
            guild_id=config.GUILD_ID,
            role_id=config.SQUAD_ROLE_ID,
            name="Harness Squad",
            description="Seeded squad for smoke checks",
            switch_cost=0,
            is_active=True,
        ),
        SquadMembership(
            squad_id=UUID(config.SQUAD_ID),
            user_id=config.USER_ID,
            guild_id=config.GUILD_ID,
        ),
        APIKey(
            name="Harness Bot Key",
            description="Known-plaintext key for local smoke checks",
            key_hash=hash_api_key(config.BOT_API_KEY),
            key_prefix=config.BOT_API_KEY[:12],
            scopes=["bot:read", "bot:write", "admin:read", "admin:write"],
            is_active=True,
            created_by="local-harness",
        ),
        HelpConversation(
            id=UUID(config.HELP_CONVERSATION_ID),
            session_id="harness-session-1",
            guild_id=config.GUILD_ID,
            channel_id=config.TEXT_CHANNEL_ID,
            user_id=config.USER_ID,
            user_username=config.USER_NAME,
            interaction_type="slash_command",
            user_question="How do bytes work?",
            bot_response="You earn bytes daily.",
            tokens_used=42,
        ),
        ForumAgent(
            id=UUID(config.FORUM_AGENT_ID),
            guild_id=config.GUILD_ID,
            name="Harness Forum Agent",
            system_prompt="Answer forum posts helpfully.",
            created_by="local-harness",
            is_active=True,
        ),
        ForumAgentResponse(
            agent_id=UUID(config.FORUM_AGENT_ID),
            guild_id=config.GUILD_ID,
            channel_id=config.FORUM_CHANNEL_ID,
            thread_id="666600000000000001",
            post_title="Seeded post",
            post_content="Seeded post content",
            author_display_name=config.USER_NAME,
            decision_reason="seeded",
            confidence_score=0.9,
        ),
        ForumNotificationTopic(
            guild_id=config.GUILD_ID,
            forum_channel_id=config.FORUM_CHANNEL_ID,
            topic_name="general-help",
        ),
        ForumUserSubscription(
            guild_id=config.GUILD_ID,
            user_id=config.USER_ID,
            username=config.USER_NAME,
            forum_channel_id=config.FORUM_CHANNEL_ID,
            notification_hours=24,
        ),
        Campaign(
            id=UUID(config.CAMPAIGN_ID),
            guild_id=config.GUILD_ID,
            title="Harness Campaign",
            description="Seeded campaign",
            start_time=now - timedelta(days=2),
            created_by="local-harness",
            is_active=True,
            announcement_channels=[config.TEXT_CHANNEL_ID],
        ),
        Challenge(
            id=UUID(config.CHALLENGE_ID),
            campaign_id=UUID(config.CAMPAIGN_ID),
            title="Harness Challenge",
            description="Seeded challenge",
            order_position=1,
            is_released=True,
            released_at=now - timedelta(hours=1),
            is_announced=False,
        ),
        ScheduledMessage(
            id=UUID(config.SCHEDULED_MESSAGE_ID),
            campaign_id=UUID(config.CAMPAIGN_ID),
            title="Harness Scheduled Message",
            description="Seeded scheduled message",
            scheduled_time=now - timedelta(minutes=10),
            created_by="local-harness",
        ),
        RepeatingMessage(
            id=UUID(config.REPEATING_MESSAGE_ID),
            guild_id=config.GUILD_ID,
            channel_id=config.TEXT_CHANNEL_ID,
            message_content="Harness repeating message",
            start_time=now - timedelta(days=1),
            # 1-minute cadence: /repeating-messages/due only returns messages
            # whose *current* schedule slot is within +/-60s of now, so a
            # 1-minute interval keeps the seeded message reliably "due"
            # regardless of how long boot takes.
            interval_minutes=1,
            next_send_time=now - timedelta(minutes=5),
            created_by="local-harness",
            is_active=True,
        ),
        AuditLogConfig(guild_id=config.GUILD_ID),
        AdventOfCodeConfig(
            guild_id=config.GUILD_ID,
            forum_channel_id=config.FORUM_CHANNEL_ID,
            is_active=True,
        ),
        AdventOfCodeThread(
            guild_id=config.GUILD_ID,
            year=config.AOC_YEAR,
            day=config.AOC_DAY,
            thread_id=config.AOC_THREAD_ID,
            thread_title=f"AoC {config.AOC_YEAR} Day {config.AOC_DAY}",
        ),
        AttachmentFilterConfig(guild_id=config.GUILD_ID),
        SquadSaleEvent(
            guild_id=config.GUILD_ID,
            name="Harness Sale",
            start_time=now - timedelta(hours=1),
            duration_hours=24,
            created_by="local-harness",
        ),
        ChannelModelOverride(
            guild_id=config.GUILD_ID,
            channel_id=config.TEXT_CHANNEL_ID,
            model_key=config.MODEL_OVERRIDE_MODEL_KEY,
        ),
    ]


def _main_rows() -> list[object]:
    now = _now()
    return [
        # Mark the Skrift setup wizard complete so requests aren't redirected
        # to /setup on first boot.
        Setting(key=SETUP_COMPLETED_AT_KEY, value=now.isoformat()),
        Setting(key=SITE_NAME_KEY, value="Smarter Dev Harness"),
        Setting(key=SITE_THEME_KEY, value="smarterdev"),
        Quest(
            id=UUID(config.QUEST_ID),
            guild_id=config.GUILD_ID,
            title="Harness Quest",
            prompt="Solve the seeded quest.",
        ),
        DailyQuest(
            id=UUID(config.DAILY_QUEST_ID),
            guild_id=config.GUILD_ID,
            quest_id=UUID(config.QUEST_ID),
            active_date=date.today(),
            expires_at=now + timedelta(days=1),
            is_active=True,
        ),
        FeatureFlag(key="harness-demo-flag"),
        ChatAgentEngagement(
            id=UUID(config.CHAT_ENGAGEMENT_ID),
            guild_id=config.GUILD_ID,
            channel_id=config.TEXT_CHANNEL_ID,
            activation_user_id=config.USER_ID,
            activation_username=config.USER_NAME,
            activation_message_id="777700000000000001",
        ),
        MemberActivity(
            guild_id=config.GUILD_ID,
            user_id=config.USER_ID,
            first_message_at=now - timedelta(days=7),
            last_message_at=now - timedelta(hours=1),
        ),
        ChannelHandler(
            guild_id=config.GUILD_ID,
            channel_id=config.TEXT_CHANNEL_ID,
            name="harness-handler",
            trigger_type="message",
            description="Seeded handler",
            script="async def handle(event):\n    pass\n",
            created_by=config.USER_ID,
        ),
    ]


async def _create_legacy_leftover_tables(database_url: str) -> None:
    """Mirror prod's bc_websites leftovers that migrations no longer create.

    ``campaign_signups`` moved to the main DB (skrift schema) but the legacy
    /bot-admin page still reads it from the legacy DB, where prod retains the
    historical table. Create it here so the harness matches prod's layout.
    """
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: CampaignSignup.__table__.create(
                sync_connection, checkfirst=True
            )
        )
    await engine.dispose()


async def _insert_all(database_url: str, rows: list[object], *, skrift_schema: bool) -> None:
    engine = create_async_engine(database_url)
    if skrift_schema:
        engine = engine.execution_options(schema_translate_map={None: "skrift"})
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        session.add_all(rows)
        await session.commit()
    await engine.dispose()


async def seed() -> None:
    await _create_legacy_leftover_tables(config.LEGACY_DATABASE_URL)
    await _insert_all(config.LEGACY_DATABASE_URL, _legacy_rows(), skrift_schema=False)
    await _insert_all(config.MAIN_DATABASE_URL, _main_rows(), skrift_schema=True)


if __name__ == "__main__":
    asyncio.run(seed())
    print("seeded harness data into legacy + main databases")
