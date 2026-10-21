"""let a forum send its new posts straight to the proactive agent

A forum's row (#297) can skip the watcher's classifier for a new post's
opening message, which then wakes the agent as a mention does. Replies in
the post are still classified. Off for every existing row.

Revision ID: c5e1f7a3b9d2
Revises: d4a8f2c6e1b9
Create Date: 2026-10-21 19:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c5e1f7a3b9d2"
down_revision: str | None = "d4a8f2c6e1b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proactive_channel_settings",
        sa.Column(
            "new_posts_to_agent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("proactive_channel_settings", "new_posts_to_agent")
