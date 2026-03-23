"""rename pipeline_mode values to 4-mode system

Revision ID: b7c8d9e0f1a2
Revises: 396a7c3e5fd9
Create Date: 2026-03-22 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7c8d9e0f1a2'
down_revision = '396a7c3e5fd9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename pipeline_mode values: lite → quick_answer, experimental → standard, premium → deep
    op.execute(
        sa.text(
            "UPDATE research_sessions SET pipeline_mode = 'quick_answer' "
            "WHERE pipeline_mode = 'lite'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE research_sessions SET pipeline_mode = 'standard' "
            "WHERE pipeline_mode = 'experimental'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE research_sessions SET pipeline_mode = 'deep' "
            "WHERE pipeline_mode = 'premium'"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE research_sessions SET pipeline_mode = 'lite' "
            "WHERE pipeline_mode = 'quick_answer'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE research_sessions SET pipeline_mode = 'experimental' "
            "WHERE pipeline_mode = 'standard'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE research_sessions SET pipeline_mode = 'premium' "
            "WHERE pipeline_mode = 'deep'"
        )
    )
