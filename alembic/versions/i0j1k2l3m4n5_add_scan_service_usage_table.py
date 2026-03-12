"""Add scan_service_usage table for internal service cost tracking

Revision ID: i0j1k2l3m4n5
Revises: h9i0j1k2l3m4
Create Date: 2026-03-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID

# revision identifiers, used by Alembic.
revision: str = "i0j1k2l3m4n5"
down_revision: str = "h9i0j1k2l3m4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scan_service_usage",
        sa.Column("id", PostgresUUID(as_uuid=True), primary_key=True),
        sa.Column("task_type", sa.String(50), nullable=False, index=True),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer, server_default="0"),
        sa.Column("output_tokens", sa.Integer, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("user_id", sa.String(100), nullable=True),
        sa.Column("session_id", PostgresUUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("scan_service_usage")
