"""Add created_at/updated_at to webhook_events_processed.

Every model inherits ``created_at`` / ``updated_at`` from ``Base`` (with a
``server_default`` of now()), so SQLAlchemy emits them in the INSERT ... RETURNING
for the dedupe ledger. The original ``stripe_events_processed`` migration never
added these columns — a latent mismatch that never surfaced because prod never
processed a real Stripe webhook. Now that Polar delivers events, the insert
fails without them. Bring the table in line with Base.

Revision ID: d8b2f1e6a3c9
Revises: c7e1a4f9d2b6
Create Date: 2026-07-03 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8b2f1e6a3c9"
down_revision: Union[str, None] = "c7e1a4f9d2b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "webhook_events_processed",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column(
        "webhook_events_processed",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column("webhook_events_processed", "updated_at")
    op.drop_column("webhook_events_processed", "created_at")
