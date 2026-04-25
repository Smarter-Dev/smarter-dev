"""add_forum_user_tagging_tables

Revision ID: 0fb05458c189
Revises: 1f214f10f9ba
Create Date: 2025-08-25 16:31:49.822605

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0fb05458c189'
down_revision = '1f214f10f9ba'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create forum_notification_topics table
    op.create_table('forum_notification_topics',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('guild_id', sa.String(), nullable=False),
        sa.Column('forum_channel_id', sa.String(), nullable=False),
        sa.Column('topic_name', sa.String(100), nullable=False),
        sa.Column('topic_description', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_forum_notification_topics')),
        sa.UniqueConstraint('guild_id', 'forum_channel_id', 'topic_name', name='uq_forum_notification_topics_guild_forum_topic')
    )
    with op.batch_alter_table('forum_notification_topics', schema=None) as batch_op:
        batch_op.create_index('ix_forum_notification_topics_guild_id', ['guild_id'], unique=False)
        batch_op.create_index('ix_forum_notification_topics_forum_channel_id', ['forum_channel_id'], unique=False)
        batch_op.create_index('ix_forum_notification_topics_guild_forum', ['guild_id', 'forum_channel_id'], unique=False)

    # Create forum_user_subscriptions table
    op.create_table('forum_user_subscriptions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('guild_id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('username', sa.String(100), nullable=False),
        sa.Column('forum_channel_id', sa.String(), nullable=False),
        sa.Column('subscribed_topics', sa.JSON(), nullable=False),
        sa.Column('notification_hours', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('notification_hours = -1 OR (notification_hours >= 1 AND notification_hours <= 8760)', name=op.f('ck_forum_user_subscriptions_notification_hours_valid')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_forum_user_subscriptions')),
        sa.UniqueConstraint('guild_id', 'user_id', 'forum_channel_id', name='uq_forum_user_subscriptions_guild_user_forum')
    )
    with op.batch_alter_table('forum_user_subscriptions', schema=None) as batch_op:
        batch_op.create_index('ix_forum_user_subscriptions_guild_id', ['guild_id'], unique=False)
        batch_op.create_index('ix_forum_user_subscriptions_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_forum_user_subscriptions_forum_channel_id', ['forum_channel_id'], unique=False)
        batch_op.create_index('ix_forum_user_subscriptions_guild_forum', ['guild_id', 'forum_channel_id'], unique=False)

    # Add columns to existing forum_agents table
    with op.batch_alter_table('forum_agents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('enable_user_tagging', sa.Boolean(), nullable=False, server_default='false'))
        batch_op.add_column(sa.Column('enable_responses', sa.Boolean(), nullable=False, server_default='true'))
        batch_op.add_column(sa.Column('notification_topics', sa.JSON(), nullable=False, server_default='[]'))


def downgrade() -> None:
    # Remove columns from forum_agents table
    with op.batch_alter_table('forum_agents', schema=None) as batch_op:
        batch_op.drop_column('notification_topics')
        batch_op.drop_column('enable_responses')
        batch_op.drop_column('enable_user_tagging')

    # Drop forum_user_subscriptions table
    with op.batch_alter_table('forum_user_subscriptions', schema=None) as batch_op:
        batch_op.drop_index('ix_forum_user_subscriptions_guild_forum')
        batch_op.drop_index('ix_forum_user_subscriptions_forum_channel_id')
        batch_op.drop_index('ix_forum_user_subscriptions_user_id')
        batch_op.drop_index('ix_forum_user_subscriptions_guild_id')
    op.drop_table('forum_user_subscriptions')

    # Drop forum_notification_topics table
    with op.batch_alter_table('forum_notification_topics', schema=None) as batch_op:
        batch_op.drop_index('ix_forum_notification_topics_guild_forum')
        batch_op.drop_index('ix_forum_notification_topics_forum_channel_id')
        batch_op.drop_index('ix_forum_notification_topics_guild_id')
    op.drop_table('forum_notification_topics')