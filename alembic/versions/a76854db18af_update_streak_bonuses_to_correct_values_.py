"""Update streak bonuses to correct values (8,16,32,64)

Revision ID: a76854db18af
Revises: 4f4c4fe587ed
Create Date: 2025-07-31 05:57:57.832063

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a76854db18af'
down_revision = '4f4c4fe587ed'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Update existing bytes_configs records to use correct streak bonus values
    # Change from {7: 2, 14: 4, 30: 10, 60: 20} to {8: 2, 16: 4, 32: 8, 64: 16}
    connection = op.get_bind()
    
    # Update all records that have the old default values
    connection.execute(
        sa.text("""
            UPDATE bytes_configs 
            SET streak_bonuses = '{"8": 2, "16": 4, "32": 8, "64": 16}'::json
            WHERE streak_bonuses::text = '{"7": 2, "14": 4, "30": 10, "60": 20}'::text
        """)
    )


def downgrade() -> None:
    # Revert back to old streak bonus values if needed
    connection = op.get_bind()
    
    connection.execute(
        sa.text("""
            UPDATE bytes_configs 
            SET streak_bonuses = '{"7": 2, "14": 4, "30": 10, "60": 20}'::json
            WHERE streak_bonuses::text = '{"8": 2, "16": 4, "32": 8, "64": 16}'::text
        """)
    )