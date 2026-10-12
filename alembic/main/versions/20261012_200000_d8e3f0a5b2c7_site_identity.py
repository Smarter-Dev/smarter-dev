"""Site identity revisions and keyed deletion fences.

Revision ID: d8e3f0a5b2c7
Revises: c7d2e9f4a1b6
"""

import sqlalchemy as sa

from alembic import op

revision = "d8e3f0a5b2c7"
down_revision = "c7d2e9f4a1b6"
branch_labels = None
depends_on = None


def audit():
    return [
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
    ]


def upgrade():
    op.create_table(
        "site_identity_key",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("verifier", sa.String(64), nullable=False),
        *audit(),
    )
    op.create_table(
        "site_identity_fences",
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        *audit(),
    )
    op.create_table(
        "site_identity_states",
        sa.Column("user_id", sa.UUID(), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("owned_accounts", sa.JSON(), nullable=False),
        *audit(),
    )


def downgrade():
    # A downgrade must never silently discard irreversible deletion fences.
    raise RuntimeError("Retain site identity fences; roll back application code only")
