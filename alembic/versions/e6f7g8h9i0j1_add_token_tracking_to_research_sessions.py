"""Add token tracking columns to research_sessions

Revision ID: e6f7g8h9i0j1
Revises: d5e6f7g8h9i0
Create Date: 2026-03-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e6f7g8h9i0j1"
down_revision: str = "d5e6f7g8h9i0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "research_sessions",
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "research_sessions",
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "research_sessions",
        sa.Column(
            "pipeline_mode",
            sa.String(20),
            nullable=False,
            server_default="lite",
        ),
    )
    with op.batch_alter_table("research_sessions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_research_sessions_user_pipeline",
            ["user_id", "pipeline_mode"],
        )


def downgrade() -> None:
    with op.batch_alter_table("research_sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_research_sessions_user_pipeline")
    op.drop_column("research_sessions", "pipeline_mode")
    op.drop_column("research_sessions", "output_tokens")
    op.drop_column("research_sessions", "input_tokens")
