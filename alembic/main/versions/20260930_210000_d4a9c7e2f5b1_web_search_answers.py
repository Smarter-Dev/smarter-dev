"""Web searches can carry a written answer

GPT-6 Luna now decides while planning whether a request needs a written
answer; when it does, it reads result pages and writes one after the ranking.
The run records that decision and the answer. Both columns are additive, so the build before this one
keeps working against them.

Revision ID: d4a9c7e2f5b1
Revises: b8e4f1a7c3d9
Create Date: 2026-09-30 21:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "d4a9c7e2f5b1"
down_revision: str | None = "b8e4f1a7c3d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_search_runs",
        sa.Column(
            "needs_answer", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column("web_search_runs", sa.Column("answer", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("web_search_runs", "answer")
    op.drop_column("web_search_runs", "needs_answer")
