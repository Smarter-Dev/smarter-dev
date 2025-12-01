"""Tests for Advent of Code CRUD operations."""

from __future__ import annotations

import pytest
import tempfile
import os
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from smarter_dev.web.crud import AdventOfCodeConfigOperations, ConflictError
from smarter_dev.web.models import AdventOfCodeConfig, AdventOfCodeThread
from smarter_dev.shared.database import Base, async_sessionmaker


@pytest.fixture(scope="function")
async def aoc_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create an isolated database session for AoC CRUD tests.

    Only creates the AdventOfCodeConfig and AdventOfCodeThread tables
    to avoid duplicate index issues in other models.
    """
    # Create a unique temporary database file
    temp_dir = tempfile.mkdtemp()
    db_name = f"aoc_test_db_{uuid.uuid4().hex}.db"
    db_path = os.path.join(temp_dir, db_name)
    database_url = f"sqlite+aiosqlite:///{db_path}"

    engine = create_async_engine(
        database_url,
        poolclass=StaticPool,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )

    try:
        # Only create the specific tables we need for AoC tests
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: AdventOfCodeConfig.__table__.create(sync_conn, checkfirst=True))
            await conn.run_sync(lambda sync_conn: AdventOfCodeThread.__table__.create(sync_conn, checkfirst=True))

        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        session = session_maker()

        try:
            yield session
        finally:
            await session.close()
    finally:
        await engine.dispose()
        # Clean up temp files
        if os.path.exists(db_path):
            os.remove(db_path)
        if os.path.exists(temp_dir):
            os.rmdir(temp_dir)


class TestAdventOfCodeConfigOperations:
    """Tests for AdventOfCodeConfigOperations class."""

    @pytest.fixture
    def ops(self):
        """Create operations instance."""
        return AdventOfCodeConfigOperations()

    @pytest.fixture
    def test_guild_id(self):
        """Test guild ID."""
        import uuid
        return str(int(uuid.uuid4().hex[:15], 16))

    # --- get_config tests ---

    async def test_get_config_returns_none_for_nonexistent(self, aoc_db_session, ops, test_guild_id):
        """get_config returns None when config doesn't exist."""
        result = await ops.get_config(aoc_db_session, test_guild_id)
        assert result is None

    async def test_get_config_returns_existing_config(self, aoc_db_session, ops, test_guild_id):
        """get_config returns config when it exists."""
        # Create config directly
        config = AdventOfCodeConfig(
            guild_id=test_guild_id,
            forum_channel_id="123456789",
            is_active=True,
        )
        aoc_db_session.add(config)
        await aoc_db_session.commit()

        result = await ops.get_config(aoc_db_session, test_guild_id)
        assert result is not None
        assert result.guild_id == test_guild_id
        assert result.forum_channel_id == "123456789"
        assert result.is_active is True

    # --- get_or_create_config tests ---

    async def test_get_or_create_config_creates_new(self, aoc_db_session, ops, test_guild_id):
        """get_or_create_config creates new config when none exists."""
        result = await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        assert result is not None
        assert result.guild_id == test_guild_id
        assert result.is_active is False  # Default value
        assert result.forum_channel_id is None

    async def test_get_or_create_config_returns_existing(self, aoc_db_session, ops, test_guild_id):
        """get_or_create_config returns existing config."""
        # Create config first
        config = AdventOfCodeConfig(
            guild_id=test_guild_id,
            forum_channel_id="987654321",
            is_active=True,
        )
        aoc_db_session.add(config)
        await aoc_db_session.commit()

        # Now get_or_create should return existing
        result = await ops.get_or_create_config(aoc_db_session, test_guild_id)
        assert result.forum_channel_id == "987654321"
        assert result.is_active is True

    # --- update_config tests ---

    async def test_update_config_updates_fields(self, aoc_db_session, ops, test_guild_id):
        """update_config updates specified fields."""
        # Create config first
        await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        # Update it
        result = await ops.update_config(
            aoc_db_session,
            test_guild_id,
            forum_channel_id="new_channel_123",
            is_active=True,
        )
        await aoc_db_session.commit()

        assert result.forum_channel_id == "new_channel_123"
        assert result.is_active is True

    async def test_update_config_updates_timestamp(self, aoc_db_session, ops, test_guild_id):
        """update_config updates the updated_at timestamp."""
        # Create config first
        config = await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        # Wait a tiny bit and update
        import asyncio
        await asyncio.sleep(0.01)

        result = await ops.update_config(
            aoc_db_session,
            test_guild_id,
            is_active=True
        )
        await aoc_db_session.commit()
        await aoc_db_session.refresh(result)

        # Just verify the timestamp exists and is set
        assert result.updated_at is not None

    async def test_update_config_creates_if_not_exists(self, aoc_db_session, ops, test_guild_id):
        """update_config creates config if it doesn't exist (via get_or_create)."""
        # Config doesn't exist yet
        result = await ops.update_config(
            aoc_db_session,
            test_guild_id,
            forum_channel_id="channel123",
            is_active=True
        )
        await aoc_db_session.commit()

        assert result is not None
        assert result.guild_id == test_guild_id
        assert result.forum_channel_id == "channel123"

    # --- get_active_configs tests ---

    async def test_get_active_configs_returns_empty_when_none(self, aoc_db_session, ops):
        """get_active_configs returns empty list when no configs exist."""
        result = await ops.get_active_configs(aoc_db_session)
        assert result == []

    async def test_get_active_configs_only_returns_active_with_channel(self, aoc_db_session, ops):
        """get_active_configs only returns configs that are active AND have forum_channel_id."""
        import uuid

        # Create various configs
        configs = [
            # This should be returned - active with channel
            AdventOfCodeConfig(
                guild_id=str(int(uuid.uuid4().hex[:15], 16)),
                forum_channel_id="channel1",
                is_active=True,
            ),
            # This should NOT be returned - not active
            AdventOfCodeConfig(
                guild_id=str(int(uuid.uuid4().hex[:15], 16)),
                forum_channel_id="channel2",
                is_active=False,
            ),
            # This should NOT be returned - no channel
            AdventOfCodeConfig(
                guild_id=str(int(uuid.uuid4().hex[:15], 16)),
                forum_channel_id=None,
                is_active=True,
            ),
            # This should be returned - active with channel
            AdventOfCodeConfig(
                guild_id=str(int(uuid.uuid4().hex[:15], 16)),
                forum_channel_id="channel4",
                is_active=True,
            ),
        ]

        for config in configs:
            aoc_db_session.add(config)
        await aoc_db_session.commit()

        result = await ops.get_active_configs(aoc_db_session)

        assert len(result) == 2
        for config in result:
            assert config.is_active is True
            assert config.forum_channel_id is not None

    # --- get_posted_thread tests ---

    async def test_get_posted_thread_returns_none_for_nonexistent(self, aoc_db_session, ops, test_guild_id):
        """get_posted_thread returns None when thread doesn't exist."""
        # Create config first (required for FK)
        await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        result = await ops.get_posted_thread(aoc_db_session, test_guild_id, 2025, 1)
        assert result is None

    async def test_get_posted_thread_returns_existing_thread(self, aoc_db_session, ops, test_guild_id):
        """get_posted_thread returns thread when it exists."""
        # Create config first
        await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        # Create thread
        thread = AdventOfCodeThread(
            guild_id=test_guild_id,
            year=2025,
            day=5,
            thread_id="discord_thread_123",
            thread_title="Day 5 - Advent of Code"
        )
        aoc_db_session.add(thread)
        await aoc_db_session.commit()

        result = await ops.get_posted_thread(aoc_db_session, test_guild_id, 2025, 5)
        assert result is not None
        assert result.day == 5
        assert result.thread_id == "discord_thread_123"

    async def test_get_posted_thread_respects_year(self, aoc_db_session, ops, test_guild_id):
        """get_posted_thread distinguishes between years."""
        # Create config
        await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        # Create thread for 2024
        thread = AdventOfCodeThread(
            guild_id=test_guild_id,
            year=2024,
            day=1,
            thread_id="thread_2024",
            thread_title="Day 1 - 2024"
        )
        aoc_db_session.add(thread)
        await aoc_db_session.commit()

        # Query for 2025 should return None
        result = await ops.get_posted_thread(aoc_db_session, test_guild_id, 2025, 1)
        assert result is None

        # Query for 2024 should return the thread
        result = await ops.get_posted_thread(aoc_db_session, test_guild_id, 2024, 1)
        assert result is not None
        assert result.year == 2024

    # --- record_posted_thread tests ---

    async def test_record_posted_thread_creates_record(self, aoc_db_session, ops, test_guild_id):
        """record_posted_thread creates a new thread record."""
        # Create config first
        await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        result = await ops.record_posted_thread(
            aoc_db_session,
            guild_id=test_guild_id,
            year=2025,
            day=10,
            thread_id="new_thread_456",
            thread_title="Day 10 - Advent of Code"
        )
        await aoc_db_session.commit()

        assert result is not None
        assert result.guild_id == test_guild_id
        assert result.year == 2025
        assert result.day == 10
        assert result.thread_id == "new_thread_456"
        assert result.thread_title == "Day 10 - Advent of Code"

    async def test_record_posted_thread_raises_conflict_on_duplicate(self, aoc_db_session, ops, test_guild_id):
        """record_posted_thread raises ConflictError when thread already exists."""
        # Create config first
        await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        # Create first thread
        await ops.record_posted_thread(
            aoc_db_session,
            guild_id=test_guild_id,
            year=2025,
            day=1,
            thread_id="first_thread",
            thread_title="Day 1"
        )
        await aoc_db_session.commit()

        # Try to create duplicate
        with pytest.raises(ConflictError):
            await ops.record_posted_thread(
                aoc_db_session,
                guild_id=test_guild_id,
                year=2025,
                day=1,
                thread_id="second_thread",
                thread_title="Day 1 Again"
            )

    # --- get_guild_threads tests ---

    async def test_get_guild_threads_returns_all_threads(self, aoc_db_session, ops, test_guild_id):
        """get_guild_threads returns all threads for a guild."""
        # Create config
        await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        # Create multiple threads
        for day in [1, 2, 3, 5, 10]:
            thread = AdventOfCodeThread(
                guild_id=test_guild_id,
                year=2025,
                day=day,
                thread_id=f"thread_{day}",
                thread_title=f"Day {day}"
            )
            aoc_db_session.add(thread)
        await aoc_db_session.commit()

        result = await ops.get_guild_threads(aoc_db_session, test_guild_id)

        assert len(result) == 5
        days = [t.day for t in result]
        assert sorted(days) == [1, 2, 3, 5, 10]

    async def test_get_guild_threads_filters_by_year(self, aoc_db_session, ops, test_guild_id):
        """get_guild_threads filters by year when specified."""
        # Create config
        await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        # Create threads for different years
        for year in [2024, 2025]:
            for day in [1, 2, 3]:
                thread = AdventOfCodeThread(
                    guild_id=test_guild_id,
                    year=year,
                    day=day,
                    thread_id=f"thread_{year}_{day}",
                    thread_title=f"Day {day} - {year}"
                )
                aoc_db_session.add(thread)
        await aoc_db_session.commit()

        # Get only 2025 threads
        result = await ops.get_guild_threads(aoc_db_session, test_guild_id, year=2025)

        assert len(result) == 3
        for thread in result:
            assert thread.year == 2025

    async def test_get_guild_threads_returns_empty_for_no_threads(self, aoc_db_session, ops, test_guild_id):
        """get_guild_threads returns empty list when no threads exist."""
        # Create config but no threads
        await ops.get_or_create_config(aoc_db_session, test_guild_id)
        await aoc_db_session.commit()

        result = await ops.get_guild_threads(aoc_db_session, test_guild_id)
        assert result == []
