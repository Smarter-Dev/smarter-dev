"""add agent conversations

Creates the two tables that back persisted agent conversations rendered at
``/ai/answer/{id}``. ``owner_user_id`` references Skrift's ``users.id``; the
FK is declared manually (not via SQLAlchemy ForeignKey) because the Skrift
User model lives on a separate Base metadata that this migration set doesn't
own.

Revision ID: 53c59fcec17b
Revises: f3c4200ddcde
Create Date: 2026-05-13 17:56:08.275294

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '53c59fcec17b'
down_revision: Union[str, None] = 'f3c4200ddcde'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_conversations',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('agent_type', sa.String(length=32), nullable=False),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('owner_user_id', sa.UUID(), nullable=False),
        sa.Column('meta', sa.JSON(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_conversations')),
    )
    op.create_index(
        'ix_agent_conversations_owner_created',
        'agent_conversations',
        ['owner_user_id', 'created_at'],
        unique=False,
    )
    # FK to skrift.users(id); declared raw because the Skrift User model is on
    # a separate metadata Alembic can't reach.
    op.execute(
        "ALTER TABLE agent_conversations "
        "ADD CONSTRAINT fk_agent_conversations_owner_user_id_users "
        "FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE"
    )

    op.create_table(
        'agent_messages',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('conversation_id', sa.UUID(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('citations', sa.JSON(), nullable=False),
        sa.Column('usage', sa.JSON(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['conversation_id'],
            ['agent_conversations.id'],
            name=op.f('fk_agent_messages_conversation_id_agent_conversations'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_messages')),
        sa.UniqueConstraint('conversation_id', 'sequence', name='uq_agent_messages_conv_seq'),
    )


def downgrade() -> None:
    op.drop_table('agent_messages')
    op.execute(
        "ALTER TABLE agent_conversations "
        "DROP CONSTRAINT IF EXISTS fk_agent_conversations_owner_user_id_users"
    )
    op.drop_index('ix_agent_conversations_owner_created', table_name='agent_conversations')
    op.drop_table('agent_conversations')
