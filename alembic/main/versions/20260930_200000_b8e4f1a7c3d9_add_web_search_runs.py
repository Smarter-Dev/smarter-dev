"""Add web_search_runs for the dashboard's web search

Each row is one search a signed-in user ran from /dashboard: GPT-6 Luna's five
queries, the Brave results they found and Jev's ranking of them. The
agent-worker writes every stage here before it notifies the browser, so a page
that reloads mid-search paints from the row.

The usage ledger also gains a 'search' product, so each search's Luna, Brave
and Jev spend reaches the admin invoices. Both changes only add, so the build
before this one keeps working against them.

Revision ID: b8e4f1a7c3d9
Revises: a3f6d2c8e1b7
Create Date: 2026-09-30 20:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b8e4f1a7c3d9"
down_revision: str | None = "a3f6d2c8e1b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_search_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submission_key", sa.String(64), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("queries", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("ranked", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_web_search_runs")),
        sa.UniqueConstraint(
            "owner_user_id", "submission_key", name="uq_web_search_run_submission"
        ),
    )
    op.create_index(
        "ix_web_search_runs_owner_created",
        "web_search_runs",
        ["owner_user_id", "created_at"],
    )
    op.drop_constraint(
        "ck_usage_cost_rows_usage_product_mode", "usage_cost_rows", type_="check"
    )
    op.create_check_constraint(
        "ck_usage_cost_rows_usage_product_mode",
        "usage_cost_rows",
        "product_mode IN ('resources','chat','discord','search')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM usage_cost_rows WHERE product_mode = 'search'")
    op.drop_constraint(
        "ck_usage_cost_rows_usage_product_mode", "usage_cost_rows", type_="check"
    )
    op.create_check_constraint(
        "ck_usage_cost_rows_usage_product_mode",
        "usage_cost_rows",
        "product_mode IN ('resources','chat','discord')",
    )
    op.drop_index("ix_web_search_runs_owner_created", table_name="web_search_runs")
    op.drop_table("web_search_runs")
