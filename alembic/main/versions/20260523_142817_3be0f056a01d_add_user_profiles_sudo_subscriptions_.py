"""add user_profiles, sudo_subscriptions, feature_flags

Adds the project-side profile fields, the sudo membership subscription
records, and a generic DB-backed feature flag store used (initially) to gate
the sudo launch page.

`user_profiles.user_id` and `sudo_subscriptions.user_id` reference
Skrift's `users.id`; the FKs are declared via raw SQL because the Skrift User
model lives on a separate Base metadata that this migration set doesn't own
(see also alembic/main/versions/20260513_175608_53c59fcec17b_add_agent_conversations.py).

Revision ID: 3be0f056a01d
Revises: 0d0a8f9e6096
Create Date: 2026-05-23 14:28:17.742794

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3be0f056a01d'
down_revision: Union[str, None] = '0d0a8f9e6096'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_profiles',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('handle', sa.String(length=40), nullable=True),
        sa.Column('bio', sa.String(length=500), nullable=True),
        sa.Column('timezone', sa.String(length=64), nullable=True),
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
        sa.PrimaryKeyConstraint('id', name=op.f('pk_user_profiles')),
        sa.UniqueConstraint('handle', name=op.f('uq_user_profiles_handle')),
    )
    op.create_index(
        op.f('ix_user_profiles_user_id'),
        'user_profiles',
        ['user_id'],
        unique=True,
    )
    op.execute(
        "ALTER TABLE user_profiles "
        "ADD CONSTRAINT fk_user_profiles_user_id_users "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
    )

    op.create_table(
        'sudo_subscriptions',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('tier', sa.String(length=32), nullable=False),
        sa.Column('stripe_customer_id', sa.String(length=64), nullable=False),
        sa.Column('stripe_subscription_id', sa.String(length=64), nullable=False),
        sa.Column('stripe_price_id', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('current_period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'cancel_at_period_end',
            sa.Boolean(),
            server_default='false',
            nullable=False,
        ),
        sa.Column('founder_seat_number', sa.Integer(), nullable=True),
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
        sa.CheckConstraint(
            "tier IN ('founder', 'read', 'write', 'execute')",
            name='ck_sudo_subscriptions_tier',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_sudo_subscriptions')),
        sa.UniqueConstraint(
            'founder_seat_number',
            name=op.f('uq_sudo_subscriptions_founder_seat_number'),
        ),
        sa.UniqueConstraint(
            'stripe_subscription_id',
            name=op.f('uq_sudo_subscriptions_stripe_subscription_id'),
        ),
        sa.UniqueConstraint(
            'user_id',
            name=op.f('uq_sudo_subscriptions_user_id'),
        ),
    )
    op.create_index(
        'ix_sudo_subscriptions_stripe_customer_id',
        'sudo_subscriptions',
        ['stripe_customer_id'],
        unique=False,
    )
    op.execute(
        "ALTER TABLE sudo_subscriptions "
        "ADD CONSTRAINT fk_sudo_subscriptions_user_id_users "
        "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
    )

    op.create_table(
        'feature_flags',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column(
            'mode',
            sa.String(length=16),
            server_default='disabled',
            nullable=False,
        ),
        sa.Column('description', sa.String(length=255), nullable=True),
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
        sa.CheckConstraint(
            "mode IN ('enabled', 'admin_only', 'disabled')",
            name='ck_feature_flags_mode',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_feature_flags')),
        sa.UniqueConstraint('key', name=op.f('uq_feature_flags_key')),
    )


def downgrade() -> None:
    op.drop_table('feature_flags')
    op.execute(
        "ALTER TABLE sudo_subscriptions "
        "DROP CONSTRAINT IF EXISTS fk_sudo_subscriptions_user_id_users"
    )
    op.drop_index(
        'ix_sudo_subscriptions_stripe_customer_id',
        table_name='sudo_subscriptions',
    )
    op.drop_table('sudo_subscriptions')
    op.execute(
        "ALTER TABLE user_profiles "
        "DROP CONSTRAINT IF EXISTS fk_user_profiles_user_id_users"
    )
    op.drop_index(op.f('ix_user_profiles_user_id'), table_name='user_profiles')
    op.drop_table('user_profiles')
