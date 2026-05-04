"""Add technologies column to scan_user_profiles

Revision ID: h9i0j1k2l3m4
Revises: g8h9i0j1k2l3
Create Date: 2026-03-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "h9i0j1k2l3m4"
down_revision: str = "g8h9i0j1k2l3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "scan_user_profiles",
        sa.Column("technologies", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scan_user_profiles", "technologies")
