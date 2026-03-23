"""merge moderation and research heads

Revision ID: 396a7c3e5fd9
Revises: j1k2l3m4n5o6, 13ffec03089b
Create Date: 2026-03-13 14:01:45.201812

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '396a7c3e5fd9'
down_revision = ('j1k2l3m4n5o6', '13ffec03089b')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass