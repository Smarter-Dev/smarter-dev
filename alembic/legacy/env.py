"""Alembic env for the legacy DB (bc-websites).

Owns the legacy bot/admin tables that live in the `public` schema. Skrift's
own tables (in this DB's `skrift` schema, used by the legacy admin's auth)
are managed separately by the Skrift package and run via `scripts/migrate.py`.

Slated for deletion once the legacy admin is sunset.
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

LEGACY_TABLES: frozenset[str] = frozenset({
    "advent_of_code_configs",
    "advent_of_code_threads",
    "api_keys",
    "attachment_filter_configs",
    "audit_log_configs",
    "bytes_balances",
    "bytes_configs",
    "bytes_transactions",
    "campaigns",
    "challenge_inputs",
    "challenge_submissions",
    "challenges",
    "forum_agent_responses",
    "forum_agents",
    "forum_notification_topics",
    "forum_user_subscriptions",
    "help_conversations",
    "repeating_messages",
    "scheduled_messages",
    "security_logs",
    "squad_memberships",
    "squad_sale_events",
    "squads",
})

SCHEMA = "public"
# Distinct from Skrift's `alembic_version` (which lives in this DB's `skrift` schema)
# so the two migration sets don't trample each other.
VERSION_TABLE = "alembic_version_legacy"

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.effective_legacy_database_url)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in LEGACY_TABLES
    if type_ in ("index", "unique_constraint", "foreign_key_constraint", "column", "check_constraint"):
        parent_table = getattr(obj, "table", None)
        if parent_table is not None and parent_table.name not in LEGACY_TABLES:
            return False
    return True


def do_run_migrations(connection: Connection) -> None:
    connection.execute(text(f"SET search_path TO {SCHEMA}"))
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
    cfg["sqlalchemy.url"] = settings.effective_legacy_database_url
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
