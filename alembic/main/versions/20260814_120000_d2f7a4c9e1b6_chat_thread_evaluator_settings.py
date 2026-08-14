"""Administrator settings for the Quick chat idle evaluator.

When a Quick chat sits idle and the person comes back, a cheap model decides
whether their message continues the thread in progress or starts a new subject.
Which model that is, what backs it up, and how long "idle" means are all
administrator settings, exactly like the summarizer and the compaction model:
the person chatting neither chooses them nor sees them.

Both not-null columns carry a ``server_default`` so the existing singleton row
upgrades in place with no data step. ``thread_idle_minutes`` defaults to 15
because that is the number the feature was specified against; it is a column
rather than a constant because it will want tuning against real behaviour, and
the check constraint keeps a mistyped 0 from turning the evaluator into a tax on
every message.

The evaluator's model is stored as a catalog KEY, never a provider wire id. Two
DeepSeek Flash wire ids are still priced in this system ("deepseek-v4-flash" and
the retired "deepseek-4-flash"), and only the key is unambiguous.

Revision ID: d2f7a4c9e1b6
Revises: b1d6f9c4e8a2
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d2f7a4c9e1b6"
down_revision = "b1d6f9c4e8a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_settings",
        sa.Column(
            "thread_evaluator_model_key",
            sa.String(100),
            nullable=False,
            server_default=sa.text("'deepseek-v4'"),
        ),
    )
    op.add_column(
        "chat_settings",
        sa.Column("thread_evaluator_fallback_model_key", sa.String(100), nullable=True),
    )
    op.add_column(
        "chat_settings",
        sa.Column(
            "thread_idle_minutes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("15"),
        ),
    )
    op.create_check_constraint(
        "chat_settings_thread_idle_minutes",
        "chat_settings",
        "thread_idle_minutes > 0",
    )


def downgrade() -> None:
    # The logical name goes in at both ends; the metadata naming convention
    # renders it to ck_chat_settings_chat_settings_thread_idle_minutes on the
    # way to the server.
    op.drop_constraint(
        "chat_settings_thread_idle_minutes", "chat_settings", type_="check"
    )
    op.drop_column("chat_settings", "thread_idle_minutes")
    op.drop_column("chat_settings", "thread_evaluator_fallback_model_key")
    op.drop_column("chat_settings", "thread_evaluator_model_key")
