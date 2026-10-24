"""Add ``web_search_forgotten_keys``: site search keys asked to be forgotten.

A site chat turn starts a member's search with one key. When the start's
reply is lost the site never learns the run's id, so it forgets the run by
its key (``DELETE /api/site/search/users/{id}/keys/{key}``): the run goes if
there is one, and a row here makes a start with that key refuse (410). Purged
with the account. A new table only; each statement still waits at most 5 s
for a lock.

Revision ID: b7e3c1d9a5f2
Revises: f2b6d9c4a1e7
Create Date: 2026-10-24 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7e3c1d9a5f2"
down_revision: str | None = "f2b6d9c4a1e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "web_search_forgotten_keys"


def _bound_lock_waits() -> None:
    # As in e4c8b2f6a9d1: a lock wait fails the migration, never stalls a table.
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def upgrade() -> None:
    _bound_lock_waits()
    op.create_table(
        TABLE,
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submission_key", sa.String(length=64), nullable=False),
        # Base gives every model these two.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "owner_user_id", "submission_key", name="pk_web_search_forgotten_keys"
        ),
    )


def downgrade() -> None:
    _bound_lock_waits()
    op.drop_table(TABLE)
