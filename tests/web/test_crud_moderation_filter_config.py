"""Tests for ModerationFilterConfigOperations CRUD operations."""

from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.crud import DatabaseOperationError
from smarter_dev.web.crud import ModerationFilterConfigOperations
from smarter_dev.web.models import ModerationFilterConfig


@pytest.fixture
def moderation_filter_ops() -> ModerationFilterConfigOperations:
    return ModerationFilterConfigOperations()


class TestGetConfig:
    async def test_returns_none_when_guild_has_no_config(
        self, moderation_filter_ops, db_session: AsyncSession
    ):
        assert await moderation_filter_ops.get_config(db_session, "G-missing") is None

    async def test_returns_existing_config(
        self, moderation_filter_ops, db_session: AsyncSession
    ):
        db_session.add(
            ModerationFilterConfig(guild_id="G1", content_filter_enabled=True)
        )
        await db_session.commit()

        config = await moderation_filter_ops.get_config(db_session, "G1")

        assert config is not None
        assert config.content_filter_enabled is True

    async def test_wraps_driver_failure_in_database_operation_error(
        self, moderation_filter_ops
    ):
        failing_session = Mock(spec=AsyncSession)
        failing_session.execute = AsyncMock(side_effect=SQLAlchemyError("boom"))

        with pytest.raises(DatabaseOperationError, match="moderation filter config"):
            await moderation_filter_ops.get_config(failing_session, "G1")


class TestGetOrCreateConfig:
    async def test_creates_config_with_every_filter_off(
        self, moderation_filter_ops, db_session: AsyncSession
    ):
        config = await moderation_filter_ops.get_or_create_config(db_session, "G1")
        await db_session.commit()

        assert config.guild_id == "G1"
        assert config.content_filter_enabled is False
        assert config.invite_filter_enabled is False
        assert config.webhook_killer_enabled is False
        assert config.blocked_tlds == []

    async def test_returns_existing_config_without_duplicating(
        self, moderation_filter_ops, db_session: AsyncSession
    ):
        db_session.add(ModerationFilterConfig(guild_id="G1", blocked_tlds=[".xxx"]))
        await db_session.commit()

        config = await moderation_filter_ops.get_or_create_config(db_session, "G1")

        assert config.blocked_tlds == [".xxx"]

        all_configs = await moderation_filter_ops.get_active_configs(db_session)
        assert all_configs == []


class TestUpdateConfig:
    async def test_updates_supplied_fields_only(
        self, moderation_filter_ops, db_session: AsyncSession
    ):
        db_session.add(ModerationFilterConfig(guild_id="G1"))
        await db_session.commit()

        config = await moderation_filter_ops.update_config(
            db_session,
            "G1",
            content_filter_enabled=True,
            blocked_tlds=[".gay", ".xxx"],
            mod_log_channel_id="999",
        )
        await db_session.commit()

        assert config.content_filter_enabled is True
        assert config.blocked_tlds == [".gay", ".xxx"]
        assert config.mod_log_channel_id == "999"
        assert config.invite_filter_enabled is False

    async def test_creates_config_when_missing(
        self, moderation_filter_ops, db_session: AsyncSession
    ):
        config = await moderation_filter_ops.update_config(
            db_session, "G-new", webhook_killer_enabled=True
        )
        await db_session.commit()

        assert config.guild_id == "G-new"
        assert config.webhook_killer_enabled is True

    async def test_ignores_unknown_fields(
        self, moderation_filter_ops, db_session: AsyncSession
    ):
        config = await moderation_filter_ops.update_config(
            db_session, "G1", not_a_real_column=123, spam_engine_enabled=True
        )
        await db_session.commit()

        assert not hasattr(config, "not_a_real_column")
        assert not hasattr(config, "spam_engine_enabled")


class TestGetActiveConfigs:
    async def test_returns_configs_with_the_content_filter_enabled(
        self, moderation_filter_ops, db_session: AsyncSession
    ):
        db_session.add(ModerationFilterConfig(guild_id="G-off"))
        db_session.add(
            ModerationFilterConfig(guild_id="G-content", content_filter_enabled=True)
        )
        await db_session.commit()

        active = await moderation_filter_ops.get_active_configs(db_session)

        assert {config.guild_id for config in active} == {"G-content"}


class TestDeleteConfig:
    async def test_returns_true_when_config_removed(
        self, moderation_filter_ops, db_session: AsyncSession
    ):
        db_session.add(ModerationFilterConfig(guild_id="G1"))
        await db_session.commit()

        assert await moderation_filter_ops.delete_config(db_session, "G1") is True
        await db_session.commit()

        assert await moderation_filter_ops.get_config(db_session, "G1") is None

    async def test_returns_false_when_config_absent(
        self, moderation_filter_ops, db_session: AsyncSession
    ):
        assert await moderation_filter_ops.delete_config(db_session, "G-missing") is False
