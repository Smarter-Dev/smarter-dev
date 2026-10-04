"""Index the ages the hourly worker-table prune reads

The retention sweep (smarter_dev.web.worker_retention) deletes Skrift's event,
archive and snapshot rows by age. Skrift indexes those tables only by stream
or key, so each sweep would scan them whole. These are Skrift's tables, so the
index names carry a project prefix that a Skrift migration will not reuse.
Indexes only: the build before this one runs unchanged on the new schema.

On Postgres each index is built ``CONCURRENTLY``, outside the migration's
transaction, so the workers keep writing to these tables, which have never
been pruned, while it builds. A build interrupted part-way leaves an invalid
index under the same name, which ``IF NOT EXISTS`` would keep; it is dropped
and rebuilt.

Revision ID: c5e1a9d3b7f2
Revises: e7b2c4d9a1f3
Create Date: 2026-10-04 01:00:00.000000

"""

from __future__ import annotations

from typing import NamedTuple

import sqlalchemy as sa

from alembic import op

revision: str = "c5e1a9d3b7f2"
down_revision: str | None = "e7b2c4d9a1f3"
branch_labels = None
depends_on = None

class _Index(NamedTuple):
    name: str
    table: str
    columns: tuple[str, ...]
    create: str
    drop: str


# Each statement is written out whole: nothing is formatted into SQL.
_INDEXES = (
    _Index(
        "ix_sd_worker_events_created_at",
        "worker_events",
        ("created_at",),
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sd_worker_events_created_at"
        " ON worker_events (created_at)",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_sd_worker_events_created_at",
    ),
    _Index(
        "ix_sd_worker_archive_events_created_at",
        "worker_archive_events",
        ("created_at",),
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sd_worker_archive_events_created_at"
        " ON worker_archive_events (created_at)",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_sd_worker_archive_events_created_at",
    ),
    _Index(
        "ix_sd_worker_archive_snapshots_snapshot_at",
        "worker_archive_snapshots",
        ("snapshot_at",),
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sd_worker_archive_snapshots_snapshot_at"
        " ON worker_archive_snapshots (snapshot_at)",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_sd_worker_archive_snapshots_snapshot_at",
    ),
    _Index(
        "ix_sd_worker_queue_dead_lettered_updated_at",
        "worker_queue",
        ("dead_lettered", "updated_at"),
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sd_worker_queue_dead_lettered_updated_at"
        " ON worker_queue (dead_lettered, updated_at)",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_sd_worker_queue_dead_lettered_updated_at",
    ),
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_invalid(index: _Index) -> bool:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i"
                " JOIN pg_class c ON c.oid = i.indexrelid"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE c.relname = :name AND n.nspname = current_schema()"
                " AND NOT i.indisvalid"
            ),
            {"name": index.name},
        )
        .first()
        is not None
    )


def upgrade() -> None:
    if not _is_postgres():
        for index in _INDEXES:
            op.create_index(index.name, index.table, list(index.columns), if_not_exists=True)
        return
    with op.get_context().autocommit_block():
        for index in _INDEXES:
            if _is_invalid(index):
                op.execute(index.drop)
            op.execute(index.create)


def downgrade() -> None:
    if not _is_postgres():
        for index in reversed(_INDEXES):
            op.drop_index(index.name, table_name=index.table, if_exists=True)
        return
    with op.get_context().autocommit_block():
        for index in reversed(_INDEXES):
            op.execute(index.drop)
