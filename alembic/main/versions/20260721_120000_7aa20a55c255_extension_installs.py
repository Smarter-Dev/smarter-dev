"""Extension installs + admin_handlers extension-ownership columns.

Adds ``extension_installs`` (one row per guild+extension, recording slug,
installed version, config, enabled, and installer identity) and two nullable
columns on ``admin_handlers`` (``extension_install_id`` /
``extension_handler_key``) so materialised handler rows are traceable to their
install. No ForeignKey constraints by house style (cf. handler_runs.handler_id,
guild_handler_memory) — integrity is owned by the install service.

Revision ID: 7aa20a55c255
Revises: b7d9f1a3c5e2
Create Date: 2026-07-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "7aa20a55c255"
down_revision: Union[str, None] = "b7d9f1a3c5e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "extension_installs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("guild_id", sa.String(length=20), nullable=False),
        sa.Column("extension_slug", sa.String(length=64), nullable=False),
        sa.Column("installed_version", sa.Integer(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("installed_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "guild_id", "extension_slug", name="uq_extension_installs_guild_slug"
        ),
    )
    op.create_index(
        "ix_extension_installs_guild_id", "extension_installs", ["guild_id"]
    )
    op.add_column(
        "admin_handlers",
        sa.Column("extension_install_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "admin_handlers",
        sa.Column("extension_handler_key", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_admin_handlers_extension_install_id",
        "admin_handlers",
        ["extension_install_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_admin_handlers_extension_install_id", table_name="admin_handlers"
    )
    op.drop_column("admin_handlers", "extension_handler_key")
    op.drop_column("admin_handlers", "extension_install_id")
    op.drop_index(
        "ix_extension_installs_guild_id", table_name="extension_installs"
    )
    op.drop_table("extension_installs")
