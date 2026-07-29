"""Add short-lived agent search-result previews.

Revision ID: d7f2a9c4e6b1
Revises: c6e9a1b3d5f7
Create Date: 2026-07-29 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7f2a9c4e6b1"
down_revision: str | None = "c6e9a1b3d5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_result_previews",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("access_token_hash", sa.String(length=64), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            "status IN ('pending', 'ready', 'failed')",
            name=op.f("ck_search_result_previews_search_result_previews_status"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_search_result_previews"),
    )
    op.create_index(
        "ix_search_result_previews_access_token_hash",
        "search_result_previews",
        ["access_token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_search_result_previews_expires_at",
        "search_result_previews",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_search_result_previews_expires_at",
        table_name="search_result_previews",
    )
    op.drop_index(
        "ix_search_result_previews_access_token_hash",
        table_name="search_result_previews",
    )
    op.drop_table("search_result_previews")
