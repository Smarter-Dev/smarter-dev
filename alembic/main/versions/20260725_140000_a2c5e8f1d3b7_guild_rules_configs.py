"""Add the per-guild rules document table.

Creates ``guild_rules_configs``: one row per guild holding the rules an admin
authors as a single markdown document on ``/admin/bot``. The document is stored
verbatim; ``smarter_dev.web.guild_rules.parse_guild_rules`` turns it into the
numbered, individually addressable rules the ``/rule`` command cites.

Revision ID: a2c5e8f1d3b7
Revises: e7a1c4d9b3f2
Create Date: 2026-07-25 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a2c5e8f1d3b7"
down_revision: str | None = "e7a1c4d9b3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guild_rules_configs",
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column(
            "rules_markdown",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_guild_rules_configs")),
    )
    op.create_index(
        "ix_guild_rules_configs_guild_id",
        "guild_rules_configs",
        ["guild_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_guild_rules_configs_guild_id",
        table_name="guild_rules_configs",
    )
    op.drop_table("guild_rules_configs")
