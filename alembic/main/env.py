"""Alembic env for the main app DB (smarter-dev).

Owns the new-app tables (post-Skrift migration) that live in the `skrift`
schema alongside Skrift's own tables. Skrift's own migrations are managed
separately by the Skrift package and run via `scripts/migrate.py`.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from smarter_dev.shared.database import Base
from smarter_dev.shared.config import get_settings
import smarter_dev.web.models  # noqa: F401  -- registers all models with Base.metadata

# Tables this alembic config owns. The single Base.metadata is shared with the
# legacy config; partition is enforced via `include_object` so each migration
# only touches its own tables.
MAIN_TABLES: frozenset[str] = frozenset({
    "campaign_signups",
    "daily_quests",
    "moderation_actions",
    "moderation_configs",
    "quest_inputs",
    "quest_progress",
    "quest_submissions",
    "quests",
    "research_sessions",
    "resource_categories",
    "resource_creators",
    "resource_directories",
    "resource_directory_spine",
    "resource_faqs",
    "resource_sources",
    "resource_tool_sources",
    "resource_tools",
    "scan_service_usage",
    "scan_user_profiles",
    "tracked_link_counters",
})

SCHEMA = "skrift"
# Distinct from Skrift's `alembic_version` so the two migration sets don't trample each other.
VERSION_TABLE = "alembic_version_app"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.effective_database_url)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in MAIN_TABLES
    if type_ in ("index", "unique_constraint", "foreign_key_constraint", "column", "check_constraint"):
        parent_table = getattr(obj, "table", None)
        if parent_table is not None and parent_table.name not in MAIN_TABLES:
            return False
    return True


def do_run_migrations(connection: Connection) -> None:
    connection.execute(text(f"SET search_path TO {SCHEMA}, public"))
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        include_schemas=False,
        version_table=VERSION_TABLE,
        version_table_schema=SCHEMA,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = settings.effective_database_url
    connectable = async_engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.begin() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        version_table=VERSION_TABLE,
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
