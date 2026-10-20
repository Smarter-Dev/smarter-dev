"""Add the ``updated_at`` column ``spam_ring_incidents`` was created without.

``SpamRingIncident`` inherits ``updated_at`` from ``Base``, but migration
f2b7d4e9c1a6 created the table with only ``created_at``. On Postgres the ORM
asks for ``updated_at`` back after every insert and in every select, so the
spam ring's transcript save and its follow-up appends fail. The detector
catches the error and carries on with the transcript in its report only.

Existing rows take the time of the upgrade.

Revision ID: 0a337101a0e8
Revises: e4c8b2f6a9d1
Create Date: 2026-10-19 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0a337101a0e8"
down_revision: str | None = "e4c8b2f6a9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _bound_lock_waits() -> None:
    # As in e4c8b2f6a9d1: a lock wait fails the migration, never stalls the table.
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _bound_lock_waits()
    op.add_column(
        "spam_ring_incidents",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    _bound_lock_waits()
    op.drop_column("spam_ring_incidents", "updated_at")
