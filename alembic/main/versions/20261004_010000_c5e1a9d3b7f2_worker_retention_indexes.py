"""Index the ages the hourly worker-table prune reads

The retention sweep (smarter_dev.web.worker_retention) deletes Skrift's event,
archive and snapshot rows by age. Skrift indexes those tables only by stream
or key, so each sweep would scan them whole. These are Skrift's tables, so the
index names carry a project prefix that a Skrift migration will not reuse.
Indexes only: the build before this one runs unchanged on the new schema.

Revision ID: c5e1a9d3b7f2
Revises: e7b2c4d9a1f3
Create Date: 2026-10-04 01:00:00.000000

"""

from __future__ import annotations

from alembic import op

revision: str = "c5e1a9d3b7f2"
down_revision: str | None = "e7b2c4d9a1f3"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_sd_worker_events_created_at", "worker_events", "created_at"),
    ("ix_sd_worker_archive_events_created_at", "worker_archive_events", "created_at"),
    ("ix_sd_worker_archive_snapshots_snapshot_at", "worker_archive_snapshots", "snapshot_at"),
    ("ix_sd_worker_queue_dead_lettered_updated_at", "worker_queue", "dead_lettered, updated_at"),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, [c.strip() for c in columns.split(",")], if_not_exists=True)


def downgrade() -> None:
    for name, table, _ in reversed(_INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)
