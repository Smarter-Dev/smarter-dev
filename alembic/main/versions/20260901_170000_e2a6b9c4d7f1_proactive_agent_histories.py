"""add durable proactive agent histories

Revision ID: e2a6b9c4d7f1
Revises: d1f5a8c3e7b2
Create Date: 2026-09-01 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e2a6b9c4d7f1"
down_revision: str | None = "d1f5a8c3e7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proactive_agent_histories",
        sa.Column("guild_id", sa.String(length=20), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("history", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
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
        sa.CheckConstraint(
            "schema_version = 1",
            name="ck_proactive_agent_histories_schema_version",
        ),
        sa.CheckConstraint(
            "revision > 0", name="ck_proactive_agent_histories_revision_positive"
        ),
    )


def downgrade() -> None:
    op.drop_table("proactive_agent_histories")
