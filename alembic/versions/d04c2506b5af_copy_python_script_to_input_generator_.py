"""Copy python_script to input_generator_script for existing challenges

Revision ID: d04c2506b5af
Revises: f7ca13df2ecc
Create Date: 2025-08-14 17:23:29.245813

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd04c2506b5af'
down_revision = 'f7ca13df2ecc'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Copy python_script to input_generator_script for existing challenges
    # where input_generator_script is null but python_script exists
    op.execute('''
        UPDATE challenges 
        SET input_generator_script = python_script 
        WHERE input_generator_script IS NULL 
        AND python_script IS NOT NULL
    ''')


def downgrade() -> None:
    # Reverse operation - clear input_generator_script where it matches python_script
    op.execute('''
        UPDATE challenges 
        SET input_generator_script = NULL 
        WHERE input_generator_script = python_script
    ''')