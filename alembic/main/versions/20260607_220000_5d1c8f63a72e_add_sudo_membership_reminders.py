"""Add sudo_membership_reminders dedupe table.

Tracks which renewal reminder emails (30 / 7 / 1 days before expiry)
have been sent for each membership. UNIQUE(membership_id, days_before)
means the daily sweep can blindly try to insert and rely on the DB to
enforce "send each threshold at most once per membership". On
re-purchase the new membership row gets its own reminder lifecycle.

Revision ID: 5d1c8f63a72e
Revises: 2f7e91a3c84d
Create Date: 2026-06-07 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5d1c8f63a72e"
down_revision: Union[str, None] = "2f7e91a3c84d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sudo_membership_reminders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("membership_id", sa.UUID(), nullable=False),
        sa.Column("days_before", sa.Integer(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sudo_membership_reminders")),
        sa.UniqueConstraint(
            "membership_id",
            "days_before",
            name=op.f("uq_sudo_membership_reminders_membership_id_days_before"),
        ),
    )
    op.execute(
        "ALTER TABLE sudo_membership_reminders "
        "ADD CONSTRAINT fk_sudo_membership_reminders_membership_id "
        "FOREIGN KEY (membership_id) REFERENCES sudo_memberships(id) "
        "ON DELETE CASCADE"
    )
    op.create_index(
        "ix_sudo_membership_reminders_membership_id",
        "sudo_membership_reminders",
        ["membership_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sudo_membership_reminders_membership_id",
        table_name="sudo_membership_reminders",
    )
    op.execute(
        "ALTER TABLE sudo_membership_reminders "
        "DROP CONSTRAINT IF EXISTS fk_sudo_membership_reminders_membership_id"
    )
    op.drop_table("sudo_membership_reminders")
