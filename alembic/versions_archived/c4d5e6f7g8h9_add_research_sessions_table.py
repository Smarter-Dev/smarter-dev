"""Add research_sessions table

Revision ID: c4d5e6f7g8h9
Revises: b3c4d5e6f7g8
Create Date: 2026-03-06

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d5e6f7g8h9'
down_revision = 'b3c4d5e6f7g8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('research_sessions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('query', sa.Text(), nullable=False),
        sa.Column('user_id', sa.String(length=100), nullable=False),
        sa.Column('guild_id', sa.String(length=20), nullable=True),
        sa.Column('channel_id', sa.String(length=20), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('response', sa.Text(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('sources', sa.JSON(), nullable=True),
        sa.Column('tool_log', sa.JSON(), nullable=True),
        sa.Column('followups', sa.JSON(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('context', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_research_sessions')),
    )
    with op.batch_alter_table('research_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_research_sessions_user_id'), ['user_id'], unique=False)
        batch_op.create_index('ix_research_sessions_status', ['status'], unique=False)
        batch_op.create_index('ix_research_sessions_created_at', ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('research_sessions', schema=None) as batch_op:
        batch_op.drop_index('ix_research_sessions_created_at')
        batch_op.drop_index('ix_research_sessions_status')
        batch_op.drop_index(batch_op.f('ix_research_sessions_user_id'))
    op.drop_table('research_sessions')
