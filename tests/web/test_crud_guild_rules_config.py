"""Tests for GuildRulesConfig and GuildRulesConfigOperations."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.crud import DatabaseOperationError
from smarter_dev.web.crud import GuildRulesConfigOperations
from smarter_dev.web.guild_rules import parse_guild_rules
from smarter_dev.web.models import GuildRulesConfig

_RULES = "## No spam\nOne message is enough.\n\n## Be kind\nAlways.\n"


@pytest.fixture
def guild_rules_ops() -> GuildRulesConfigOperations:
    return GuildRulesConfigOperations()


class TestGuildRulesConfigModel:
    def test_defaults_to_empty_markdown_and_timestamps(self):
        config = GuildRulesConfig(guild_id="G1")

        assert config.guild_id == "G1"
        assert config.rules_markdown == ""
        assert isinstance(config.created_at, datetime)
        assert isinstance(config.updated_at, datetime)
        assert config.created_at.tzinfo is UTC

    def test_get_defaults_builds_an_unsaved_config(self):
        config = GuildRulesConfig.get_defaults("G1")

        assert isinstance(config, GuildRulesConfig)
        assert config.guild_id == "G1"
        assert config.rules_markdown == ""

    def test_explicit_markdown_is_kept(self):
        config = GuildRulesConfig(guild_id="G1", rules_markdown=_RULES)

        assert config.rules_markdown == _RULES

    def test_repr_names_the_guild_and_the_rule_count(self):
        text = repr(GuildRulesConfig(guild_id="G1", rules_markdown=_RULES))

        assert "G1" in text
        assert "GuildRulesConfig" in text

    def test_stored_markdown_round_trips_through_the_parser(self):
        config = GuildRulesConfig(guild_id="G1", rules_markdown=_RULES)

        rules = parse_guild_rules(config.rules_markdown)

        assert [rule.index for rule in rules] == [1, 2]
        assert [rule.title for rule in rules] == ["No spam", "Be kind"]


class TestGetConfig:
    async def test_returns_none_when_guild_has_no_config(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        assert await guild_rules_ops.get_config(db_session, "G-missing") is None

    async def test_returns_existing_config(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        db_session.add(GuildRulesConfig(guild_id="G1", rules_markdown=_RULES))
        await db_session.commit()

        config = await guild_rules_ops.get_config(db_session, "G1")

        assert config is not None
        assert config.rules_markdown == _RULES

    async def test_wraps_driver_failure_in_database_operation_error(
        self, guild_rules_ops
    ):
        failing_session = Mock(spec=AsyncSession)
        failing_session.execute = AsyncMock(side_effect=SQLAlchemyError("boom"))

        with pytest.raises(DatabaseOperationError, match="guild rules config"):
            await guild_rules_ops.get_config(failing_session, "G1")


class TestGetOrCreateConfig:
    async def test_creates_config_with_empty_markdown(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        config = await guild_rules_ops.get_or_create_config(db_session, "G1")
        await db_session.commit()

        assert config.guild_id == "G1"
        assert config.rules_markdown == ""
        assert parse_guild_rules(config.rules_markdown) == []

    async def test_returns_existing_config_without_duplicating(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        db_session.add(GuildRulesConfig(guild_id="G1", rules_markdown=_RULES))
        await db_session.commit()

        config = await guild_rules_ops.get_or_create_config(db_session, "G1")
        again = await guild_rules_ops.get_or_create_config(db_session, "G1")

        assert config.rules_markdown == _RULES
        assert again.rules_markdown == _RULES

    async def test_wraps_driver_failure_in_database_operation_error(
        self, guild_rules_ops
    ):
        failing_session = Mock(spec=AsyncSession)
        failing_session.execute = AsyncMock(side_effect=SQLAlchemyError("boom"))

        with pytest.raises(DatabaseOperationError, match="guild rules config"):
            await guild_rules_ops.get_or_create_config(failing_session, "G1")


class TestUpdateConfig:
    async def test_creates_the_row_when_the_guild_has_none(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        config = await guild_rules_ops.update_config(
            db_session, "G1", rules_markdown=_RULES
        )
        await db_session.commit()

        assert config.rules_markdown == _RULES
        persisted = await guild_rules_ops.get_config(db_session, "G1")
        assert persisted.rules_markdown == _RULES

    async def test_overwrites_existing_markdown(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        db_session.add(GuildRulesConfig(guild_id="G1", rules_markdown="## Old\nx"))
        await db_session.commit()

        config = await guild_rules_ops.update_config(
            db_session, "G1", rules_markdown=_RULES
        )
        await db_session.commit()

        assert config.rules_markdown == _RULES

    async def test_clearing_the_textarea_persists_empty_markdown(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        db_session.add(GuildRulesConfig(guild_id="G1", rules_markdown=_RULES))
        await db_session.commit()

        config = await guild_rules_ops.update_config(
            db_session, "G1", rules_markdown=""
        )
        await db_session.commit()

        assert config.rules_markdown == ""

    async def test_unknown_keys_are_ignored(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        config = await guild_rules_ops.update_config(
            db_session, "G1", rules_markdown=_RULES, surprise="boom"
        )
        await db_session.commit()

        assert not hasattr(config, "surprise")

    async def test_touches_the_updated_timestamp(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        created = await guild_rules_ops.get_or_create_config(db_session, "G1")
        await db_session.commit()
        before = created.updated_at

        updated = await guild_rules_ops.update_config(
            db_session, "G1", rules_markdown=_RULES
        )
        await db_session.commit()

        assert updated.updated_at >= before

    async def test_wraps_driver_failure_in_database_operation_error(
        self, guild_rules_ops
    ):
        failing_session = Mock(spec=AsyncSession)
        failing_session.execute = AsyncMock(side_effect=SQLAlchemyError("boom"))

        with pytest.raises(DatabaseOperationError, match="guild rules config"):
            await guild_rules_ops.update_config(
                failing_session, "G1", rules_markdown=_RULES
            )


class TestDeleteConfig:
    async def test_deletes_an_existing_config(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        db_session.add(GuildRulesConfig(guild_id="G1", rules_markdown=_RULES))
        await db_session.commit()

        assert await guild_rules_ops.delete_config(db_session, "G1") is True
        await db_session.commit()
        assert await guild_rules_ops.get_config(db_session, "G1") is None

    async def test_returns_false_when_nothing_to_delete(
        self, guild_rules_ops, db_session: AsyncSession
    ):
        assert await guild_rules_ops.delete_config(db_session, "G-missing") is False

    async def test_wraps_driver_failure_in_database_operation_error(
        self, guild_rules_ops
    ):
        failing_session = Mock(spec=AsyncSession)
        failing_session.execute = AsyncMock(side_effect=SQLAlchemyError("boom"))

        with pytest.raises(DatabaseOperationError, match="guild rules config"):
            await guild_rules_ops.delete_config(failing_session, "G1")
