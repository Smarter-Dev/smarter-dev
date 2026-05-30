"""add authoring_pipeline_runs + Smarter Dev agent user

Stage 2 of the blogging-agent pipeline. The per-event audit log piggybacks
on Skrift's native agent event log; this table just carries the
bookkeeping (status, lineage of stage session ids, the resulting page).

Also inserts a 'Smarter Dev' Skrift user that Synthesis attributes
agent-authored posts to, plus a matching `author_profiles` row with
`is_agent=true`. Idempotent via ``ON CONFLICT DO NOTHING``.

Revision ID: 1a4b4df89a32
Revises: d1a42e5f8ac6
Create Date: 2026-05-22 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "1a4b4df89a32"
down_revision: Union[str, None] = "d1a42e5f8ac6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SMARTER_DEV_AGENT_EMAIL = "agent@smarter.dev"
SMARTER_DEV_AGENT_NAME = "Smarter Dev"


def upgrade() -> None:
    op.create_table(
        "authoring_pipeline_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("kicked_off_by_user_id", sa.UUID(), nullable=True),
        sa.Column("root_session_id", sa.UUID(), nullable=True),
        sa.Column(
            "stage_session_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("result_page_id", sa.UUID(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_authoring_pipeline_runs")),
    )
    op.create_index(
        "ix_authoring_pipeline_runs_status_created",
        "authoring_pipeline_runs",
        ["status", "created_at"],
        unique=False,
    )
    op.execute(
        "ALTER TABLE authoring_pipeline_runs "
        "ADD CONSTRAINT fk_authoring_pipeline_runs_kicked_off_by_user_id_users "
        "FOREIGN KEY (kicked_off_by_user_id) REFERENCES users(id) "
        "ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE authoring_pipeline_runs "
        "ADD CONSTRAINT fk_authoring_pipeline_runs_result_page_id_pages "
        "FOREIGN KEY (result_page_id) REFERENCES pages(id) "
        "ON DELETE SET NULL"
    )

    # Bootstrap the Smarter Dev agent user (idempotent). Synthesis sets
    # `pages.user_id` to this row so agent-authored posts attribute to
    # 'Smarter Dev' in the byline.
    op.execute(
        sa.text(
            """
            INSERT INTO users (id, email, name, is_active, created_at, updated_at)
            VALUES (gen_random_uuid(), :email, :name, true, NOW(), NOW())
            ON CONFLICT (email) DO NOTHING
            """
        ).bindparams(
            email=SMARTER_DEV_AGENT_EMAIL,
            name=SMARTER_DEV_AGENT_NAME,
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO author_profiles (id, user_id, is_agent, created_at, updated_at)
            SELECT gen_random_uuid(), u.id, true, NOW(), NOW()
              FROM users u
             WHERE u.email = :email
            ON CONFLICT (user_id) DO UPDATE SET is_agent = true, updated_at = NOW()
            """
        ).bindparams(email=SMARTER_DEV_AGENT_EMAIL)
    )


def downgrade() -> None:
    # Leave the agent user/profile in place on downgrade — they may be
    # referenced by published pages and removing them would 500 the blog.
    op.drop_index(
        "ix_authoring_pipeline_runs_status_created",
        table_name="authoring_pipeline_runs",
    )
    op.drop_table("authoring_pipeline_runs")
