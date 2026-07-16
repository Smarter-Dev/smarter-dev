"""Copy the legacy bot tables from bc_websites into the main DB's skrift schema.

Phase 02 of the legacy sunset (docs/v2/legacy-sunset/02-db-consolidation.md).
HUMAN-executed at deploy time — see runbooks/02-db-cutover.md. Agents must
never run this against a real database.

Behavior:

- Source: ``LEGACY_DATABASE_URL`` (bc_websites, ``public`` schema).
  Target: ``DATABASE_URL`` (main DB) with ``schema_translate_map
  {None: "skrift"}``. Both read via ``smarter_dev.shared.config.get_settings``.
- Dry-run by default: prints the FK-dependency copy order and per-table
  source/target row counts, writes nothing. Pass ``--execute`` to copy.
- Copies 23 of the 24 legacy tables, parents before children (order derived
  from ``Base.metadata.sorted_tables``). ``api_keys`` is EXCLUDED: that table
  name in the skrift schema belongs to Skrift's own key table (phase 01).
- Idempotency: ``INSERT ... ON CONFLICT (pk) DO NOTHING`` in batches, with a
  commit per table — an interrupted run can simply be re-run and resumes
  where it left off. Chosen over refuse-on-non-empty precisely so partial
  runs are recoverable; pre-existing target rows are never overwritten.
- Verification (after ``--execute``): per-table ``count(*)`` must match
  exactly and ``max(created_at)`` must be identical; any mismatch raises
  ``CopyVerificationError`` and the process exits non-zero. Run only while
  writes are paused (see the runbook) — target-only rows fail verification.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Table, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

import smarter_dev.web.models  # noqa: F401  -- registers all models with Base.metadata
from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import Base, convert_postgres_url_for_asyncpg

logger = logging.getLogger("copy_legacy_data")

TARGET_SCHEMA = "skrift"
DEFAULT_BATCH_SIZE = 1000

# The 24 tables that live in bc_websites ``public`` (alembic/legacy's former
# ownership set; that frozenset is now closed/empty, so the authoritative
# list lives here for the one-time copy).
LEGACY_SOURCE_TABLE_NAMES: frozenset[str] = frozenset({
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
    "channel_model_overrides",
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

# NEVER copied: ``skrift.api_keys`` is Skrift core's own (different-shaped)
# table — the authoritative bot keys after phase 01. Legacy key rows die with
# bc_websites in phase 05.
COPY_EXCLUDED_TABLE_NAMES: frozenset[str] = frozenset({"api_keys"})

COPY_TABLE_NAMES: frozenset[str] = LEGACY_SOURCE_TABLE_NAMES - COPY_EXCLUDED_TABLE_NAMES


class CopyVerificationError(RuntimeError):
    """Raised when post-copy row counts or created_at parity do not match."""


@dataclass(frozen=True)
class TableCopyResult:
    """Per-table outcome of one copy (or dry-run inspection) pass."""

    table_name: str
    source_row_count: int
    target_rows_before: int
    rows_inserted: int
    target_rows_after: int


def fk_ordered_copy_tables() -> list[Table]:
    """The copy set in FK-dependency order (parents before children).

    Derived from ``Base.metadata.sorted_tables`` so a model/FK change can
    never silently produce a wrong hardcoded order; the expected order is
    pinned in tests/scripts/test_copy_legacy_data.py.
    """
    ordered_tables = [
        table for table in Base.metadata.sorted_tables if table.name in COPY_TABLE_NAMES
    ]
    found_names = {table.name for table in ordered_tables}
    if found_names != COPY_TABLE_NAMES:
        missing = sorted(COPY_TABLE_NAMES - found_names)
        raise RuntimeError(
            f"legacy tables missing from Base.metadata (model renamed/removed?): {missing}"
        )
    excluded_present = found_names & COPY_EXCLUDED_TABLE_NAMES
    if excluded_present:
        raise RuntimeError(f"excluded tables leaked into the copy set: {sorted(excluded_present)}")
    return ordered_tables


async def _count_rows(connection: AsyncConnection, table: Table) -> int:
    return (await connection.execute(select(func.count()).select_from(table))).scalar_one()


async def _max_created_at(connection: AsyncConnection, table: Table) -> datetime | None:
    return (await connection.execute(select(func.max(table.c.created_at)))).scalar_one()


async def _copy_table_rows(
    source_engine: AsyncEngine,
    target_engine: AsyncEngine,
    table: Table,
    batch_size: int,
) -> None:
    """Stream source rows and upsert-skip them into the target, one transaction."""
    primary_key_column_names = [column.name for column in table.primary_key.columns]
    insert_statement = postgresql_insert(table).on_conflict_do_nothing(
        index_elements=primary_key_column_names
    )
    async with source_engine.connect() as source_connection:
        row_stream = await source_connection.stream(select(table))
        async with target_engine.begin() as target_connection:
            async for row_batch in row_stream.mappings().partitions(batch_size):
                await target_connection.execute(
                    insert_statement, [dict(row) for row in row_batch]
                )


async def _verify_table(
    source_connection: AsyncConnection,
    target_connection: AsyncConnection,
    table: Table,
    source_row_count: int,
    target_rows_after: int,
) -> None:
    if target_rows_after != source_row_count:
        raise CopyVerificationError(
            f"{table.name}: target has {target_rows_after} rows, source has "
            f"{source_row_count} — copy incomplete or target received writes "
            "outside this copy (were writes paused?)"
        )
    source_max_created_at = await _max_created_at(source_connection, table)
    target_max_created_at = await _max_created_at(target_connection, table)
    if source_max_created_at != target_max_created_at:
        raise CopyVerificationError(
            f"{table.name}: max(created_at) mismatch — source "
            f"{source_max_created_at}, target {target_max_created_at}"
        )


async def run_copy(
    source_url: str,
    target_url: str,
    *,
    execute: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[TableCopyResult]:
    """Copy (or, when ``execute`` is False, only inspect) all adopted tables.

    Raises CopyVerificationError on any post-copy count/parity mismatch.
    """
    if source_url == target_url:
        raise ValueError(
            "source and target database URLs are identical — refusing to copy a "
            "database onto itself (is LEGACY_DATABASE_URL set?)"
        )
    ordered_tables = fk_ordered_copy_tables()
    mode = "EXECUTE" if execute else "DRY-RUN"
    logger.info("%s: copying %d tables (api_keys excluded)", mode, len(ordered_tables))
    logger.info("planned order: %s", ", ".join(table.name for table in ordered_tables))

    source_engine = create_async_engine(convert_postgres_url_for_asyncpg(source_url))
    target_engine = create_async_engine(
        convert_postgres_url_for_asyncpg(target_url)
    ).execution_options(schema_translate_map={None: TARGET_SCHEMA})

    results: list[TableCopyResult] = []
    try:
        for table in ordered_tables:
            async with source_engine.connect() as source_connection:
                source_row_count = await _count_rows(source_connection, table)
            async with target_engine.connect() as target_connection:
                target_rows_before = await _count_rows(target_connection, table)

            if execute:
                await _copy_table_rows(source_engine, target_engine, table, batch_size)

            async with target_engine.connect() as target_connection:
                target_rows_after = await _count_rows(target_connection, table)
                if execute:
                    async with source_engine.connect() as source_connection:
                        await _verify_table(
                            source_connection,
                            target_connection,
                            table,
                            source_row_count,
                            target_rows_after,
                        )

            result = TableCopyResult(
                table_name=table.name,
                source_row_count=source_row_count,
                target_rows_before=target_rows_before,
                rows_inserted=target_rows_after - target_rows_before,
                target_rows_after=target_rows_after,
            )
            results.append(result)
            logger.info(
                "%s: source=%d target_before=%d inserted=%d target_after=%d%s",
                result.table_name,
                result.source_row_count,
                result.target_rows_before,
                result.rows_inserted,
                result.target_rows_after,
                " [verified]" if execute else "",
            )
    finally:
        await source_engine.dispose()
        await target_engine.dispose()
    return results


def _print_report(results: list[TableCopyResult], *, execute: bool) -> None:
    header = f"{'table':<28} {'source':>8} {'before':>8} {'inserted':>8} {'after':>8}"
    print(header)
    print("-" * len(header))
    for result in results:
        print(
            f"{result.table_name:<28} {result.source_row_count:>8} "
            f"{result.target_rows_before:>8} {result.rows_inserted:>8} "
            f"{result.target_rows_after:>8}"
        )
    total_source = sum(result.source_row_count for result in results)
    total_inserted = sum(result.rows_inserted for result in results)
    print("-" * len(header))
    print(f"{'TOTAL':<28} {total_source:>8} {'':>8} {total_inserted:>8}")
    if not execute:
        print("\nDRY RUN — nothing was written. Re-run with --execute to copy.")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Copy legacy bot tables from LEGACY_DATABASE_URL (public schema) to "
            "DATABASE_URL (skrift schema). Dry-run by default."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually copy rows (default is a read-only dry run)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"rows per insert batch (default {DEFAULT_BATCH_SIZE})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = build_argument_parser().parse_args(argv)
    settings = get_settings()
    source_url = settings.effective_legacy_database_url
    target_url = settings.effective_database_url
    try:
        results = asyncio.run(
            run_copy(source_url, target_url, execute=args.execute, batch_size=args.batch_size)
        )
    except CopyVerificationError as error:
        logger.error("VERIFICATION FAILED: %s", error)
        return 1
    _print_report(results, execute=args.execute)
    return 0


if __name__ == "__main__":
    sys.exit(main())
