"""Member activity tracking for handler context facts.

Per-(guild, user) first/last message timestamps, fed by the bot's batched
activity reports and read at handler dispatch to inject facts like
"first message ever" / "days since last message" into trigger contexts.

Revision ID: b3d5f8a1c2e4
Revises: a9c4e2f7b1d3
Create Date: 2026-07-02 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision: str = "b3d5f8a1c2e4"
down_revision: Union[str, None] = "a9c4e2f7b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "member_activity",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("guild_id", sa.String(length=20), nullable=False),
        sa.Column("user_id", sa.String(length=20), nullable=False),
        sa.Column("first_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_member_activity_guild_user",
        "member_activity",
        ["guild_id", "user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_member_activity_guild_user", table_name="member_activity")
    op.drop_table("member_activity")
