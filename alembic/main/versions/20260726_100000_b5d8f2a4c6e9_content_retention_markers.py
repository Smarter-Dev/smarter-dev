"""Add ``content_purged_at`` markers for the 48-hour content retention sweep.

Every table that passively captures Discord message text gets a nullable,
indexed ``content_purged_at``. ``smarter_dev.web.retention`` scrubs the human
text out of rows older than 48 hours and stamps this column, which both makes
the sweep idempotent (a stamped row is skipped) and lets the admin views tell
"no content" apart from "content purged".

Revision ID: b5d8f2a4c6e9
Revises: a2c5e8f1d3b7
Create Date: 2026-07-26 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b5d8f2a4c6e9"
down_revision: str | None = "a2c5e8f1d3b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables swept by smarter_dev.web.retention.SCRUBBERS.
_TABLES: tuple[str, ...] = (
    "help_conversations",
    "forum_agent_responses",
    "moderation_actions",
    "chat_agent_engagements",
    "chat_agent_turns",
    "chat_agent_compaction_events",
    "chat_agent_errors",
    "handler_runs",
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("content_purged_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            f"ix_{table}_content_purged_at",
            table,
            ["content_purged_at"],
            unique=False,
        )


    # help_conversations carried a per-policy 7/30/90-day expiry from before
    # there was a sweep to enforce it. There is one window now, so pull every
    # existing row onto it — otherwise the admin cleanup page keeps advertising
    # a retention promise the sweep no longer honours.
    op.execute(
        "UPDATE help_conversations "
        "SET expires_at = created_at + INTERVAL '48 hours'"
    )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_index(f"ix_{table}_content_purged_at", table_name=table)
        op.drop_column(table, "content_purged_at")
