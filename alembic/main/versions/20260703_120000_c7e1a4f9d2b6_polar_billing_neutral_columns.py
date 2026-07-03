"""Rename Stripe-specific billing columns to provider-neutral names (Polar).

Billing moved from Stripe to Polar. The ``sudo_memberships`` Stripe ID columns
are renamed to provider-neutral names, and the ``stripe_events_processed``
dedupe ledger becomes ``webhook_events_processed``:

- ``stripe_customer_id``          -> ``customer_id``
- ``stripe_checkout_session_id``  -> ``checkout_id``
- ``stripe_payment_intent_id``    -> ``order_id``
- ``stripe_subscription_id``      -> ``subscription_id``
- ``stripe_price_id``             -> ``price_id``

Constraints/indexes named after the old columns are renamed to match. This is
a pure rename — no data is transformed (billing is greenfield / not yet public).

Revision ID: c7e1a4f9d2b6
Revises: b3d5f8a1c2e4
Create Date: 2026-07-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "c7e1a4f9d2b6"
down_revision: Union[str, None] = "b3d5f8a1c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (old_name, new_name) for sudo_memberships columns.
_COLUMN_RENAMES = [
    ("stripe_customer_id", "customer_id"),
    ("stripe_checkout_session_id", "checkout_id"),
    ("stripe_payment_intent_id", "order_id"),
    ("stripe_subscription_id", "subscription_id"),
    ("stripe_price_id", "price_id"),
]

# (old_name, new_name) for named unique constraints on sudo_memberships.
_CONSTRAINT_RENAMES = [
    ("uq_sudo_memberships_stripe_checkout_session_id", "uq_sudo_memberships_checkout_id"),
    ("uq_sudo_memberships_stripe_payment_intent_id", "uq_sudo_memberships_order_id"),
    ("uq_sudo_memberships_stripe_subscription_id", "uq_sudo_memberships_subscription_id"),
]


def upgrade() -> None:
    for old, new in _COLUMN_RENAMES:
        op.alter_column("sudo_memberships", old, new_column_name=new)
    for old, new in _CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE sudo_memberships RENAME CONSTRAINT {old} TO {new}")
    op.execute(
        "ALTER INDEX ix_sudo_memberships_stripe_customer_id "
        "RENAME TO ix_sudo_memberships_customer_id"
    )

    # stripe_events_processed -> webhook_events_processed (table, PK, index).
    op.rename_table("stripe_events_processed", "webhook_events_processed")
    op.execute(
        "ALTER TABLE webhook_events_processed "
        "RENAME CONSTRAINT pk_stripe_events_processed TO pk_webhook_events_processed"
    )
    op.execute(
        "ALTER INDEX ix_stripe_events_processed_processed_at "
        "RENAME TO ix_webhook_events_processed_processed_at"
    )


def downgrade() -> None:
    op.execute(
        "ALTER INDEX ix_webhook_events_processed_processed_at "
        "RENAME TO ix_stripe_events_processed_processed_at"
    )
    op.execute(
        "ALTER TABLE webhook_events_processed "
        "RENAME CONSTRAINT pk_webhook_events_processed TO pk_stripe_events_processed"
    )
    op.rename_table("webhook_events_processed", "stripe_events_processed")

    op.execute(
        "ALTER INDEX ix_sudo_memberships_customer_id "
        "RENAME TO ix_sudo_memberships_stripe_customer_id"
    )
    for old, new in _CONSTRAINT_RENAMES:
        op.execute(f"ALTER TABLE sudo_memberships RENAME CONSTRAINT {new} TO {old}")
    for old, new in _COLUMN_RENAMES:
        op.alter_column("sudo_memberships", new, new_column_name=old)
