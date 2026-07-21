"""add prompt-cache token split to chat usage records

Adds nullable prompt-cache token counts to each chat usage row so the
operator dashboard can attribute cached vs. uncached input spend:

- ``chat_agent_turns.chat_cache_read_tokens`` /
  ``chat_agent_turns.chat_cache_write_tokens`` — the cache split for the chat
  model call. Both a SUBSET of ``chat_tokens_input``.
- ``chat_agent_compaction_events.summarizer_cache_read_tokens`` /
  ``chat_agent_compaction_events.summarizer_cache_write_tokens`` — the cache
  split for the summarizer call.

All four nullable, no backfill — the historical split is unknowable.

Revision ID: c4a1d9f2e6b8
Revises: b7d4e2f8c1a5
Create Date: 2026-07-21 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4a1d9f2e6b8"
down_revision: Union[str, None] = "b7d4e2f8c1a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_agent_turns",
        sa.Column("chat_cache_read_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "chat_agent_turns",
        sa.Column("chat_cache_write_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "chat_agent_compaction_events",
        sa.Column("summarizer_cache_read_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "chat_agent_compaction_events",
        sa.Column("summarizer_cache_write_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(
        "chat_agent_compaction_events", "summarizer_cache_write_tokens"
    )
    op.drop_column(
        "chat_agent_compaction_events", "summarizer_cache_read_tokens"
    )
    op.drop_column("chat_agent_turns", "chat_cache_write_tokens")
    op.drop_column("chat_agent_turns", "chat_cache_read_tokens")
