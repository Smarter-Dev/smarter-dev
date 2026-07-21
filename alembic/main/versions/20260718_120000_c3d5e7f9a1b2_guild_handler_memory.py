"""Guild-scoped shared handler memory.

Per-key rows of a guild's shared admin-handler memory store: unlike the private
per-handler ``memory`` blob, every admin handler in the guild reads/writes this
one store, so state can cross handler rows (the DM-relay auto-bind target).
Per-key rows with UNIQUE(guild_id, key) so concurrent fires writing different
keys never clobber each other and same-key writes upsert last-write-wins.

Revision ID: c3d5e7f9a1b2
Revises: f1a2b3c4d5e6
Create Date: 2026-07-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "c3d5e7f9a1b2"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "guild_handler_memory",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("guild_id", sa.String(length=20), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
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
            "guild_id", "key", name="uq_guild_handler_memory_guild_key"
        ),
    )
    op.create_index(
        "ix_guild_handler_memory_guild_id",
        "guild_handler_memory",
        ["guild_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_guild_handler_memory_guild_id", table_name="guild_handler_memory"
    )
    op.drop_table("guild_handler_memory")
