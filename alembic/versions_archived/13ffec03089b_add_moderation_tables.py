"""Add moderation_configs and moderation_actions tables

Revision ID: 13ffec03089b
Revises: 1d910521045d
Create Date: 2026-03-13 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = '13ffec03089b'
down_revision = '1d910521045d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'moderation_configs',
        sa.Column('guild_id', sa.String(), primary_key=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column('monitored_role_ids', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('instructions', sa.Text(), nullable=True),
        sa.Column('enabled_tools', sa.JSON(), nullable=False, server_default='["warn"]'),
        sa.Column('response_channel_id', sa.String(), nullable=True),
        sa.Column('context_message_limit', sa.Integer(), nullable=False, server_default=sa.text("25")),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        'moderation_actions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('guild_id', sa.String(), nullable=False),
        sa.Column('target_user_id', sa.String(), nullable=False),
        sa.Column('target_username', sa.String(), nullable=False),
        sa.Column('moderator_user_id', sa.String(), nullable=True),
        sa.Column('moderator_username', sa.String(), nullable=True),
        sa.Column('action_type', sa.String(20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('source', sa.String(20), nullable=False, server_default='ai'),
        sa.Column('channel_id', sa.String(), nullable=True),
        sa.Column('trigger_message_id', sa.String(), nullable=True),
        sa.Column('ai_context_summary', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_mod_actions_guild_user', 'moderation_actions', ['guild_id', 'target_user_id'])
    op.create_index('ix_mod_actions_guild_type', 'moderation_actions', ['guild_id', 'action_type'])


def downgrade() -> None:
    op.drop_table('moderation_actions')
    op.drop_table('moderation_configs')
