"""mark proactive settings rows that switch on a whole forum

A whole forum switched on (#248) is one row, the forum's, and its posts run
as channels of their own. ``is_forum`` marks that row so the settings list
can leave it out for a proactive-agent worker that never asked for forum
rows: such a worker would take the forum for a channel and its posts for
none. Existing rows were all switched on before forums could be, so they
default to false.

Revision ID: e3c7a9b1f4d6
Revises: 0a337101a0e8
Create Date: 2026-10-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3c7a9b1f4d6"
down_revision: str | None = "0a337101a0e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proactive_channel_settings",
        sa.Column(
            "is_forum",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("proactive_channel_settings", "is_forum")
