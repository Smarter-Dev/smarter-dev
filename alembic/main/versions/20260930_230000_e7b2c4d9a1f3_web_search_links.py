"""Search links a user adds to their browser as a search engine

Each user has at most one link, a secret token in a URL, with an option to
open requests that Jev judges to be web addresses. The table is new, so the
build before this one never reads it.

Revision ID: e7b2c4d9a1f3
Revises: d4a9c7e2f5b1
Create Date: 2026-09-30 23:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e7b2c4d9a1f3"
down_revision: str | None = "d4a9c7e2f5b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_search_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token", sa.String(32), nullable=False),
        sa.Column(
            "open_addresses", sa.Boolean(), server_default=sa.false(), nullable=False
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_web_search_links")),
        sa.UniqueConstraint(
            "owner_user_id", name=op.f("uq_web_search_links_owner_user_id")
        ),
        sa.UniqueConstraint("token", name=op.f("uq_web_search_links_token")),
    )


def downgrade() -> None:
    op.drop_table("web_search_links")
