"""Tests for scripts/copy_legacy_data.py (phase 02 — DB consolidation).

Unit tests pin the copy set (23 tables, ``api_keys`` excluded) and its
FK-dependency order. Integration tests run the copy end-to-end against a
throwaway podman postgres with two databases (``copy_source`` playing
bc_websites ``public``, ``copy_target`` playing the main DB ``skrift``
schema), seeded with the harness's representative legacy rows.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import AsyncGenerator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

import smarter_dev.web.models  # noqa: F401  -- registers all models with Base.metadata
from scripts.copy_legacy_data import (
    COPY_EXCLUDED_TABLE_NAMES,
    COPY_TABLE_NAMES,
    LEGACY_SOURCE_TABLE_NAMES,
    CopyVerificationError,
    fk_ordered_copy_tables,
    run_copy,
)
from scripts.local_harness import config as harness_config
from scripts.local_harness.seed import _adopted_bot_rows
from smarter_dev.shared.database import Base
from smarter_dev.web.models import APIKey, BytesConfig, Squad, SquadMembership
from smarter_dev.web.security import hash_api_key

# The FK-dependency-ordered copy list (parents before children). Derived
# programmatically in the script from Base.metadata.sorted_tables; pinned
# here so an accidental model/FK change is caught explicitly.
EXPECTED_COPY_ORDER = [
    "advent_of_code_configs",
    "attachment_filter_configs",
    "audit_log_configs",
    "bytes_balances",
    "bytes_configs",
    "bytes_transactions",
    "campaigns",
    "channel_model_overrides",
    "forum_agents",
    "forum_notification_topics",
    "forum_user_subscriptions",
    "help_conversations",
    "repeating_messages",
    "security_logs",
    "squad_sale_events",
    "squads",
    "advent_of_code_threads",
    "challenges",
    "forum_agent_responses",
    "scheduled_messages",
    "squad_memberships",
    "challenge_inputs",
    "challenge_submissions",
]


class TestCopySetDefinition:
    def test_copy_order_matches_expected_fk_order(self) -> None:
        assert [table.name for table in fk_ordered_copy_tables()] == EXPECTED_COPY_ORDER

    def test_api_keys_is_excluded_from_the_copy_set(self) -> None:
        """Legacy ``public.api_keys`` must never be copied: the ``api_keys``
        name in the skrift schema belongs to Skrift's own table (phase 01)."""
        assert "api_keys" in LEGACY_SOURCE_TABLE_NAMES
        assert COPY_EXCLUDED_TABLE_NAMES == frozenset({"api_keys"})
        assert "api_keys" not in COPY_TABLE_NAMES
        assert "api_keys" not in {table.name for table in fk_ordered_copy_tables()}

    def test_copy_set_covers_the_23_adopted_tables(self) -> None:
        assert len(LEGACY_SOURCE_TABLE_NAMES) == 24
        assert COPY_TABLE_NAMES == LEGACY_SOURCE_TABLE_NAMES - {"api_keys"}
        assert len(COPY_TABLE_NAMES) == 23

    async def test_refuses_identical_source_and_target_urls(self) -> None:
        same_url = "postgresql+asyncpg://user:pass@localhost:5432/one_db"
        with pytest.raises(ValueError, match="identical"):
            await run_copy(same_url, same_url, execute=False)


# ---------------------------------------------------------------------------
# Integration: real copy between two throwaway podman postgres databases
# ---------------------------------------------------------------------------

POSTGRES_CONTAINER = "smarter_dev_copy_legacy_test_postgres"
POSTGRES_PORT = 55433  # harness uses 55432; keep clear of it and dev compose
POSTGRES_IMAGE = "postgres:15-alpine"
DB_USER = "copy_test"
DB_PASSWORD = "copy_test_password"
SOURCE_DB_NAME = "copy_source"
TARGET_DB_NAME = "copy_target"

SOURCE_URL = (
    f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@localhost:{POSTGRES_PORT}/{SOURCE_DB_NAME}"
)
TARGET_URL = (
    f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@localhost:{POSTGRES_PORT}/{TARGET_DB_NAME}"
)

LEGACY_TABLE_OBJECTS = [
    table for table in Base.metadata.sorted_tables if table.name in LEGACY_SOURCE_TABLE_NAMES
]
COPY_TABLE_OBJECTS = [
    table for table in Base.metadata.sorted_tables if table.name in COPY_TABLE_NAMES
]


def _podman(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["podman", *args], check=check, capture_output=True, text=True)


@pytest.fixture(scope="module")
def copy_test_postgres() -> None:
    if shutil.which("podman") is None:
        pytest.skip("podman not available")
    _podman(["rm", "-f", POSTGRES_CONTAINER], check=False)
    _podman(
        [
            "run", "-d", "--rm",
            "--name", POSTGRES_CONTAINER,
            "-e", f"POSTGRES_DB={SOURCE_DB_NAME}",
            "-e", f"POSTGRES_USER={DB_USER}",
            "-e", f"POSTGRES_PASSWORD={DB_PASSWORD}",
            "-p", f"{POSTGRES_PORT}:5432",
            POSTGRES_IMAGE,
        ]
    )
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        ready = _podman(
            ["exec", POSTGRES_CONTAINER, "psql", "-U", DB_USER, "-d", SOURCE_DB_NAME, "-c", "SELECT 1"],
            check=False,
        )
        if ready.returncode == 0:
            break
        time.sleep(0.5)
    else:
        _podman(["rm", "-f", POSTGRES_CONTAINER], check=False)
        pytest.fail(f"podman postgres ({POSTGRES_CONTAINER}) not ready in 60s")
    _podman(
        ["exec", POSTGRES_CONTAINER, "psql", "-U", DB_USER, "-d", SOURCE_DB_NAME,
         "-c", f"CREATE DATABASE {TARGET_DB_NAME}"]
    )
    yield
    _podman(["rm", "-f", POSTGRES_CONTAINER], check=False)


@pytest.fixture
async def source_engine(copy_test_postgres) -> AsyncGenerator[AsyncEngine, None]:
    """Fresh source DB (public schema) with all 24 legacy tables per test."""
    engine = create_async_engine(SOURCE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.drop_all(
                sync_connection, tables=LEGACY_TABLE_OBJECTS
            )
        )
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=LEGACY_TABLE_OBJECTS
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def target_engine(copy_test_postgres) -> AsyncGenerator[AsyncEngine, None]:
    """Fresh target DB with the 23 adopted tables in the skrift schema.

    ``api_keys`` is deliberately NOT created in the target: in prod that name
    is Skrift's own (different-shaped) table, so a copy attempt would fail
    loudly here if the exclusion ever regressed.
    """
    engine = create_async_engine(TARGET_URL).execution_options(
        schema_translate_map={None: "skrift"}
    )
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA IF EXISTS skrift CASCADE"))
        await connection.execute(text("CREATE SCHEMA skrift"))
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=COPY_TABLE_OBJECTS
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_source(source_engine: AsyncEngine) -> None:
    """Seed the harness's representative bot-table rows plus an api_keys row.

    The legacy api_keys row is added here (the harness no longer seeds one)
    so the copy's api_keys exclusion stays exercised against real data.
    """
    legacy_api_key_row = APIKey(
        name="Copy Test Legacy Key",
        description="Source-only row: must never be copied",
        key_hash=hash_api_key(harness_config.LEGACY_BOT_API_KEY),
        key_prefix=harness_config.LEGACY_BOT_API_KEY[:12],
        scopes=["bot:read"],
        is_active=True,
        created_by="copy-test",
    )
    session_maker = async_sessionmaker(source_engine, expire_on_commit=False)
    async with session_maker() as session:
        session.add_all([*_adopted_bot_rows(), legacy_api_key_row])
        await session.commit()


async def _count_rows(engine: AsyncEngine, table) -> int:
    async with engine.connect() as connection:
        return (
            await connection.execute(select(func.count()).select_from(table))
        ).scalar_one()


@pytest.mark.integration
class TestCopyLegacyDataIntegration:
    async def test_dry_run_writes_nothing(self, source_engine, target_engine) -> None:
        await _seed_source(source_engine)

        results = await run_copy(SOURCE_URL, TARGET_URL, execute=False)

        results_by_table = {result.table_name: result for result in results}
        assert list(results_by_table) == EXPECTED_COPY_ORDER
        assert results_by_table["squads"].source_row_count == 1
        assert results_by_table["bytes_balances"].source_row_count == 3
        for result in results:
            assert result.rows_inserted == 0
            assert result.target_rows_after == 0
        assert await _count_rows(target_engine, Squad.__table__) == 0

    async def test_execute_copies_all_rows_and_preserves_pks(
        self, source_engine, target_engine
    ) -> None:
        await _seed_source(source_engine)

        results = await run_copy(SOURCE_URL, TARGET_URL, execute=True)

        for result in results:
            assert result.target_rows_after == result.source_row_count
        assert sum(result.rows_inserted for result in results) == sum(
            result.source_row_count for result in results
        )

        async with target_engine.connect() as connection:
            copied_squad_id = (
                await connection.execute(select(Squad.__table__.c.id))
            ).scalar_one()
            assert copied_squad_id == UUID(harness_config.SQUAD_ID)

            membership = (
                await connection.execute(select(SquadMembership.__table__))
            ).one()
            assert membership.squad_id == UUID(harness_config.SQUAD_ID)
            assert membership.user_id == harness_config.USER_ID

            # api_keys must be untouched: the table does not even exist in the
            # target skrift schema of this test.
            api_keys_regclass = (
                await connection.execute(
                    text("SELECT to_regclass('skrift.api_keys')")
                )
            ).scalar_one()
            assert api_keys_regclass is None

    async def test_rerun_is_a_noop(self, source_engine, target_engine) -> None:
        await _seed_source(source_engine)

        first_run = await run_copy(SOURCE_URL, TARGET_URL, execute=True)
        second_run = await run_copy(SOURCE_URL, TARGET_URL, execute=True)

        first_by_table = {result.table_name: result for result in first_run}
        for result in second_run:
            assert result.rows_inserted == 0
            assert result.target_rows_after == first_by_table[result.table_name].target_rows_after

    async def test_partial_target_state_is_resumed(
        self, source_engine, target_engine
    ) -> None:
        """Simulate an interrupted run: rows already copied stay, missing rows land."""
        await _seed_source(source_engine)
        await run_copy(SOURCE_URL, TARGET_URL, execute=True)

        async with target_engine.begin() as connection:
            await connection.execute(SquadMembership.__table__.delete())
        assert await _count_rows(target_engine, SquadMembership.__table__) == 0

        resumed_run = await run_copy(SOURCE_URL, TARGET_URL, execute=True)

        resumed_by_table = {result.table_name: result for result in resumed_run}
        assert resumed_by_table["squad_memberships"].rows_inserted == 1
        assert await _count_rows(target_engine, SquadMembership.__table__) == 1

    async def test_verification_fails_on_target_only_rows(
        self, source_engine, target_engine
    ) -> None:
        """Extra target rows mean writes landed outside the copy — fail loudly."""
        await _seed_source(source_engine)

        session_maker = async_sessionmaker(target_engine, expire_on_commit=False)
        async with session_maker() as session:
            session.add(
                BytesConfig(guild_id=str(uuid4().int)[:18], starting_balance=1, daily_amount=1)
            )
            await session.commit()

        with pytest.raises(CopyVerificationError, match="bytes_configs"):
            await run_copy(SOURCE_URL, TARGET_URL, execute=True)
