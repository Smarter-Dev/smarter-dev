"""break out chat agent cost per bucket (chat / voice / compaction)

Adds three aggregate cost columns to ``chat_agent_engagements`` alongside
the existing ``total_cost_usd`` so the dashboard can show per-bucket
spend without recomputing from individual turns.

Backfills the new columns from existing per-turn rows.

Revision ID: b7e9d2c4f1a8
Revises: a1b2c3d4e5f6
Create Date: 2026-05-19 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e9d2c4f1a8"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_agent_engagements",
        sa.Column(
            "total_chat_cost_usd",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "chat_agent_engagements",
        sa.Column(
            "total_voice_cost_usd",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "chat_agent_engagements",
        sa.Column(
            "total_compaction_cost_usd",
            sa.Numeric(precision=10, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    # Backfill from existing per-turn / per-event rows.
    op.execute(
        """
        UPDATE chat_agent_engagements eng
        SET total_chat_cost_usd  = COALESCE(t.chat_sum, 0),
            total_voice_cost_usd = COALESCE(t.voice_sum, 0)
        FROM (
            SELECT engagement_id,
                   SUM(chat_cost_usd) AS chat_sum,
                   SUM(voice_cost_usd) AS voice_sum
            FROM chat_agent_turns
            GROUP BY engagement_id
        ) t
        WHERE t.engagement_id = eng.id
        """
    )
    op.execute(
        """
        UPDATE chat_agent_engagements eng
        SET total_compaction_cost_usd = COALESCE(c.comp_sum, 0)
        FROM (
            SELECT t.engagement_id,
                   SUM(c.summarizer_cost_usd) AS comp_sum
            FROM chat_agent_compaction_events c
            JOIN chat_agent_turns t ON t.id = c.turn_id
            GROUP BY t.engagement_id
        ) c
        WHERE c.engagement_id = eng.id
        """
    )


def downgrade() -> None:
    op.drop_column("chat_agent_engagements", "total_compaction_cost_usd")
    op.drop_column("chat_agent_engagements", "total_voice_cost_usd")
    op.drop_column("chat_agent_engagements", "total_chat_cost_usd")
