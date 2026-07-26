"""Add the missing ``created_at`` / ``updated_at`` to ``chat_agent_errors``.

Every model inherits these two columns from ``smarter_dev.shared.database.Base``,
but the migration that created ``chat_agent_errors`` (``a6c8e1f3b5d7``) declared
neither. The ORM therefore emits them on every statement against a table that
does not have them, so **every insert into the chat-agent error log has failed
since it shipped** — silently, because error persistence is best-effort and
swallows its exceptions. The table has been empty the whole time.

Adding the columns realigns the table with the model. Existing rows (there are
none, by construction) would take ``now()`` from the server default.

Revision ID: c6e9a1b3d5f7
Revises: b5d8f2a4c6e9
Create Date: 2026-07-26 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c6e9a1b3d5f7"
down_revision: str | None = "b5d8f2a4c6e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("created_at", "updated_at"):
        op.add_column(
            "chat_agent_errors",
            sa.Column(
                column,
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
        )


def downgrade() -> None:
    op.drop_column("chat_agent_errors", "updated_at")
    op.drop_column("chat_agent_errors", "created_at")
