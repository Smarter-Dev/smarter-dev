"""Index a member's web search runs by id

The site's Delete all searches pages through a member's runs in id order, 50
at a time, after a cursor (``user_content._search_ids``). The only owner index
is (owner, created_at), so each page sorted the member's whole history; this
one serves each page from the index. An index only: the build before this one
runs unchanged on the new schema.

On Postgres it is built ``CONCURRENTLY``, outside the migration's
transaction, so searches keep being written while it builds. A build
interrupted part-way leaves an invalid index under the same name, which
``IF NOT EXISTS`` would keep; it is dropped and rebuilt.

Revision ID: d4a8e2c6f1b3
Revises: b7e3c1d9a5f2
Create Date: 2026-10-25 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "d4a8e2c6f1b3"
down_revision: str | None = "b7e3c1d9a5f2"
branch_labels = None
depends_on = None

NAME = "ix_web_search_runs_owner_id"
TABLE = "web_search_runs"
COLUMNS = ["owner_user_id", "id"]
# Written out whole: nothing is formatted into SQL.
CREATE = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_web_search_runs_owner_id"
    " ON web_search_runs (owner_user_id, id)"
)
DROP = "DROP INDEX CONCURRENTLY IF EXISTS ix_web_search_runs_owner_id"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _is_invalid() -> bool:
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
            {"name": NAME},
        )
        .first()
        is not None
    )


def upgrade() -> None:
    if not _is_postgres():
        op.create_index(NAME, TABLE, COLUMNS, if_not_exists=True)
        return
    with op.get_context().autocommit_block():
        if _is_invalid():
            op.execute(DROP)
        op.execute(CREATE)


def downgrade() -> None:
    if not _is_postgres():
        op.drop_index(NAME, table_name=TABLE, if_exists=True)
        return
    with op.get_context().autocommit_block():
        op.execute(DROP)
