"""fix_challenge_inputs_column_name_from_inputs_data_to_input_data

Revision ID: 5221f410cacb
Revises: 9e3841cb0b06
Create Date: 2025-08-14 20:07:41.510469

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5221f410cacb'
down_revision = '9e3841cb0b06'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename inputs_data column to input_data if it exists
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'challenge_inputs' 
                AND column_name = 'inputs_data'
                AND table_schema = 'public'
            ) THEN
                ALTER TABLE challenge_inputs RENAME COLUMN inputs_data TO input_data;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    # Rename input_data column back to inputs_data if needed
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'challenge_inputs' 
                AND column_name = 'input_data'
                AND table_schema = 'public'
            ) THEN
                ALTER TABLE challenge_inputs RENAME COLUMN input_data TO inputs_data;
            END IF;
        END $$;
    """)