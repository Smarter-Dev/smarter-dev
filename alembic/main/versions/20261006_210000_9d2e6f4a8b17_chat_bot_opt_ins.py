"""AI assistant opt-in times (#92)

Opting out of the AI assistant from ``/privacy`` puts the person on the
existing block list (``chat_bot_blocked_users``, ``source='opt_out'``). Opting
back in removes that row and records the moment here, because it applies to
new messages only: both runtimes keep hiding what the person wrote before it.
Only the Discord user id and the time are stored.

The table is new, so the build before this one never reads it.

Revision ID: 9d2e6f4a8b17
Revises: c1a9cd91cadd
Create Date: 2026-10-06 21:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "9d2e6f4a8b17"
down_revision: str | None = "c1a9cd91cadd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_bot_opt_ins",
        sa.Column("discord_user_id", sa.String(22), nullable=False),
        sa.Column("read_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("discord_user_id", name=op.f("pk_chat_bot_opt_ins")),
    )


def downgrade() -> None:
    # Dropping the times would let both runtimes read back what people wrote
    # before they opted in again. Refuse while anyone has one.
    opted_in = op.get_bind().execute(
        sa.text("SELECT count(*) FROM chat_bot_opt_ins")
    ).scalar()
    if opted_in:
        raise RuntimeError(
            f"Refusing to downgrade: chat_bot_opt_ins has {opted_in} row(s). "
            "Dropping it exposes what those people wrote before opting back in."
        )
    op.drop_table("chat_bot_opt_ins")
