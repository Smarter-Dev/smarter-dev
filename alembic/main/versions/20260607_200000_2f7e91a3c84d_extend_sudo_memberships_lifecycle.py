"""Extend sudo_memberships for full lifecycle + add stripe event dedupe.

Phase 1+2 of bringing the sudo membership flow into alignment with the
lifecycle spec:

- Adds the lifecycle columns ``source`` (one_time / subscription / comp),
  ``stripe_subscription_id`` (nullable, unique when set), ``will_renew``
  (subscriptions only), and ``revoked_reason`` (refund / dispute / admin).
- Backfills ``source='one_time'`` for every existing row, and copies
  ``refunded_at`` into ``revoked_reason='refund'`` so support can tell
  refund vs dispute vs admin clamp going forward.
- Drops the ``UNIQUE(user_id)`` constraint so a user can hold a history
  of memberships (renewal, resubscribe-after-lapse, comps). Active-row
  uniqueness is enforced in app code now; the table is allowed to be
  append-only. Adds a plain index on ``user_id`` for the lookup query.
- Creates ``stripe_events_processed`` (event_id PK, type, processed_at)
  so the webhook router can dedupe by ``event.id`` before dispatching.
  Stripe delivers at least once.

Revision ID: 2f7e91a3c84d
Revises: b7a35a2c0f81
Create Date: 2026-06-07 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2f7e91a3c84d"
down_revision: Union[str, None] = "b7a35a2c0f81"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- sudo_memberships lifecycle columns --
    op.add_column(
        "sudo_memberships",
        sa.Column("source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "sudo_memberships",
        sa.Column("stripe_subscription_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "sudo_memberships",
        sa.Column("will_renew", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "sudo_memberships",
        sa.Column("revoked_reason", sa.String(length=16), nullable=True),
    )

    # Backfill: every row that exists today is a one-time founder purchase,
    # and any row with refunded_at set was clamped by a refund.
    op.execute("UPDATE sudo_memberships SET source = 'one_time' WHERE source IS NULL")
    op.execute(
        "UPDATE sudo_memberships SET revoked_reason = 'refund' "
        "WHERE refunded_at IS NOT NULL AND revoked_reason IS NULL"
    )

    op.alter_column("sudo_memberships", "source", nullable=False)

    op.create_check_constraint(
        "ck_sudo_memberships_source",
        "sudo_memberships",
        "source IN ('one_time', 'subscription', 'comp')",
    )
    op.create_check_constraint(
        "ck_sudo_memberships_revoked_reason",
        "sudo_memberships",
        "revoked_reason IS NULL OR revoked_reason IN ('refund', 'dispute', 'admin')",
    )

    op.create_unique_constraint(
        "uq_sudo_memberships_stripe_subscription_id",
        "sudo_memberships",
        ["stripe_subscription_id"],
    )

    # Drop the unique constraint on user_id (one row per user) so history
    # rows are allowed. The active-row invariant moves to app code.
    op.drop_constraint(
        "uq_sudo_memberships_user_id",
        "sudo_memberships",
        type_="unique",
    )
    op.create_index(
        "ix_sudo_memberships_user_id",
        "sudo_memberships",
        ["user_id"],
    )

    # -- stripe_events_processed (Phase 2 dedupe) --
    op.create_table(
        "stripe_events_processed",
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_stripe_events_processed")),
    )
    op.create_index(
        "ix_stripe_events_processed_processed_at",
        "stripe_events_processed",
        ["processed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stripe_events_processed_processed_at",
        table_name="stripe_events_processed",
    )
    op.drop_table("stripe_events_processed")

    op.drop_index("ix_sudo_memberships_user_id", table_name="sudo_memberships")
    op.create_unique_constraint(
        "uq_sudo_memberships_user_id",
        "sudo_memberships",
        ["user_id"],
    )

    op.drop_constraint(
        "uq_sudo_memberships_stripe_subscription_id",
        "sudo_memberships",
        type_="unique",
    )
    op.drop_constraint(
        "ck_sudo_memberships_revoked_reason",
        "sudo_memberships",
        type_="check",
    )
    op.drop_constraint(
        "ck_sudo_memberships_source",
        "sudo_memberships",
        type_="check",
    )

    op.drop_column("sudo_memberships", "revoked_reason")
    op.drop_column("sudo_memberships", "will_renew")
    op.drop_column("sudo_memberships", "stripe_subscription_id")
    op.drop_column("sudo_memberships", "source")
