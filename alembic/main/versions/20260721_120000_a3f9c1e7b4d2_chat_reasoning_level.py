"""add reasoning-level tracking to chat usage records

Adds a nullable ReasoningLevel wire value to each chat usage row so the
operator dashboard can attribute reasoning spend:

- ``chat_agent_turns.chat_reasoning_level`` — the level in effect for the chat
  model call, or NULL when the model has no reasoning knob / an ad-hoc model.
- ``chat_agent_compaction_events.summarizer_reasoning_level`` — the level in
  effect for the summarizer call (the summarizer runs at a fixed level), or
  NULL when it has no reasoning knob configured.

Both nullable, no backfill.

Revision ID: a3f9c1e7b4d2
Revises: f1a2b3c4d5e6
Create Date: 2026-07-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f9c1e7b4d2"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_agent_turns",
        sa.Column("chat_reasoning_level", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "chat_agent_compaction_events",
        sa.Column(
            "summarizer_reasoning_level", sa.String(length=16), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "chat_agent_compaction_events", "summarizer_reasoning_level"
    )
    op.drop_column("chat_agent_turns", "chat_reasoning_level")
