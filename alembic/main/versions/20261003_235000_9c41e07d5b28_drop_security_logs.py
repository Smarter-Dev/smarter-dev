"""Drop security_logs

Security events are structured logs since #81 (Logfire, or the standard
logger), so nothing writes or reads this table. Its rows go with it: one per
bytes API call before #81, with the Discord ids in request paths, plus auth
and admin events.

Deploy this only after a build that no longer writes the table is live: the
builds before #81 count these rows to rate limit the bot API and would fail
those requests while still serving. Downgrade recreates the empty table.

Revision ID: 9c41e07d5b28
Revises: f3a8b1c6d2e9
Create Date: 2026-10-03 23:50:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "9c41e07d5b28"
down_revision: str | None = "f3a8b1c6d2e9"
branch_labels = None
depends_on = None

_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_security_logs_action", ["action"]),
    ("ix_security_logs_action_timestamp", ["action", "timestamp"]),
    ("ix_security_logs_api_key_id", ["api_key_id"]),
    ("ix_security_logs_api_key_timestamp", ["api_key_id", "timestamp"]),
    ("ix_security_logs_ip_address", ["ip_address"]),
    ("ix_security_logs_ip_timestamp", ["ip_address", "timestamp"]),
    ("ix_security_logs_request_id", ["request_id"]),
    ("ix_security_logs_success", ["success"]),
    ("ix_security_logs_success_timestamp", ["success", "timestamp"]),
    ("ix_security_logs_timestamp", ["timestamp"]),
    ("ix_security_logs_user_identifier", ["user_identifier"]),
    ("ix_security_logs_user_timestamp", ["user_identifier", "timestamp"]),
)


def upgrade() -> None:
    op.drop_table("security_logs")


def downgrade() -> None:
    op.create_table(
        "security_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("api_key_id", sa.UUID(), nullable=True),
        sa.Column("user_identifier", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("event_metadata", sa.JSON(), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_security_logs"),
    )
    for name, columns in _INDEXES:
        op.create_index(name, "security_logs", columns, unique=False)
