"""replace sudo_subscriptions with sudo_memberships

The founder tier model shifted from recurring Stripe subscriptions to
one-time, one-year purchases (Stripe Checkout in ``mode=payment``). The
columns we tracked for subscription state (``stripe_subscription_id``,
``status``, ``current_period_end``, ``cancel_at_period_end``) no longer
apply; the new shape tracks the checkout session, the payment intent,
the purchase amount, an explicit ``expires_at`` (purchase + 1 year), and
an optional ``refunded_at``.

There are no existing rows in ``sudo_subscriptions`` (no Stripe IDs were
ever configured), so this migration drops the old table and creates the
new one from scratch.

Revision ID: b7a35a2c0f81
Revises: 3be0f056a01d
Create Date: 2026-05-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7a35a2c0f81'
down_revision: Union[str, None] = '3be0f056a01d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old subscription-shape table outright (no data to preserve).
    op.execute(
        "ALTER TABLE sudo_subscriptions "
        "DROP CONSTRAINT IF EXISTS fk_sudo_subscriptions_user_id_users"
    )
    op.drop_index(
        'ix_sudo_subscriptions_stripe_customer_id',
        table_name='sudo_subscriptions',
    )
    op.drop_table('sudo_subscriptions')

    op.create_table(
        'sudo_memberships',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('tier', sa.String(length=32), nullable=False),
        sa.Column('stripe_customer_id', sa.String(length=64), nullable=False),
        sa.Column('stripe_checkout_session_id', sa.String(length=128), nullable=False),
        sa.Column('stripe_payment_intent_id', sa.String(length=128), nullable=True),
        sa.Column('stripe_price_id', sa.String(length=64), nullable=False),
        sa.Column('amount_paid_cents', sa.Integer(), nullable=False),
        sa.Column(
            'purchased_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
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
            "tier IN ('read', 'write', 'execute')",
            name='ck_sudo_memberships_tier',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_sudo_memberships')),
        sa.UniqueConstraint(
            'user_id',
            name=op.f('uq_sudo_memberships_user_id'),
        ),
        sa.UniqueConstraint(
            'stripe_checkout_session_id',
            name=op.f('uq_sudo_memberships_stripe_checkout_session_id'),
        ),
        sa.UniqueConstraint(
            'stripe_payment_intent_id',
            name=op.f('uq_sudo_memberships_stripe_payment_intent_id'),
        ),
        sa.UniqueConstraint(
            'founder_seat_number',
            name=op.f('uq_sudo_memberships_founder_seat_number'),
        ),
    )
    op.create_index(
        'ix_sudo_memberships_stripe_customer_id',
        'sudo_memberships',
        ['stripe_customer_id'],
    )
    op.create_index(
        'ix_sudo_memberships_expires_at',
        'sudo_memberships',
        ['expires_at'],
    )
    op.execute(
        "ALTER TABLE sudo_memberships "
        "ADD CONSTRAINT fk_sudo_memberships_user_id_users "
        "FOREIGN KEY (user_id) REFERENCES skrift.users(id) ON DELETE CASCADE"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE sudo_memberships "
        "DROP CONSTRAINT IF EXISTS fk_sudo_memberships_user_id_users"
    )
    op.drop_index('ix_sudo_memberships_expires_at', table_name='sudo_memberships')
    op.drop_index(
        'ix_sudo_memberships_stripe_customer_id',
        table_name='sudo_memberships',
    )
    op.drop_table('sudo_memberships')

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
            server_default=sa.text('false'),
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
    )
    op.execute(
        "ALTER TABLE sudo_subscriptions "
        "ADD CONSTRAINT fk_sudo_subscriptions_user_id_users "
        "FOREIGN KEY (user_id) REFERENCES skrift.users(id) ON DELETE CASCADE"
    )
