"""return the one web conversation 7a18ff6495e1 moved off 3.5 Flash Lite

479f5fca562d put 3.5 Flash Lite back in the catalog but rewrote no selection,
because a conversation that 7a18ff6495e1 moved to 3.8 Flash cannot in general
be told apart from one whose owner later chose 3.8 Flash. In production one
conversation can: its selection is 3.8 Flash, it has no confirmed model change
(the only in-app way to change an existing conversation's model, and those rows
go only with the conversation), and its latest turn ran on 3.1 Flash Lite. A
turn records the conversation's selection when it is submitted, never a
fallback, so the owner last chose 3.1 Flash Lite; a7c2e5f1b8d4 moved that to
3.5 Flash Lite and 7a18ff6495e1 moved it on to 3.8 Flash.

Only that conversation, by id, and only while all three still hold: if its
owner has since confirmed a change, sent a turn on another model, or it has
been deleted, this does nothing. Its reasoning level stays if 3.5 Flash Lite
offers it and becomes "medium", Lite's default, if not.

This corrects one past mistake; it is not how a retirement works. Retirements
do not migrate chats: a conversation keeps a retired selection and its page
asks the owner to choose an available model (``RETIRED_SUCCESSORS`` in
``smarter_dev/shared/model_catalog.py``).

The downgrade does nothing. The previous build reads 3.5 Flash Lite as a live
key, and moving the conversation back to 3.8 Flash could overwrite a choice
its owner made after the upgrade.

Revision ID: c3e8a1d5f7b2
Revises: 479f5fca562d
Create Date: 2026-09-25 20:00:00.000000

"""
from typing import Sequence, Union
from uuid import UUID

from alembic import op
import sqlalchemy as sa


revision: str = "c3e8a1d5f7b2"
down_revision: Union[str, None] = "479f5fca562d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONVERSATION_ID = UUID("458f2c0e-493c-4f6c-bb54-60d4c022fb14")
_FROM_KEY = "gemini-3-8-flash"
_TO_KEY = "gemini-3-5-flash-lite"
_LAST_TURN_KEY = "gemini-3-1-flash-lite"
# 3.5 Flash Lite's reasoning levels and default; must match ``MODEL_CATALOG``.
_TO_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high")
_TO_DEFAULT = "medium"


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE web_chat_conversations"
            " SET selected_model_key = :to_key,"
            " reasoning_level = CASE"
            " WHEN reasoning_level IS NULL OR reasoning_level IN :to_levels"
            " THEN reasoning_level ELSE :to_default END"
            " WHERE id = :conversation_id"
            " AND selected_model_key = :from_key"
            " AND NOT EXISTS (SELECT 1 FROM web_chat_model_changes m"
            " WHERE m.conversation_id = web_chat_conversations.id"
            " AND m.confirmed_at IS NOT NULL)"
            " AND (SELECT t.model_key FROM web_chat_turns t"
            " WHERE t.conversation_id = web_chat_conversations.id"
            " ORDER BY t.sequence DESC LIMIT 1) = :last_turn_key"
        ).bindparams(
            sa.bindparam("conversation_id", _CONVERSATION_ID, type_=sa.Uuid()),
            sa.bindparam("to_levels", _TO_LEVELS, expanding=True),
            to_key=_TO_KEY,
            to_default=_TO_DEFAULT,
            from_key=_FROM_KEY,
            last_turn_key=_LAST_TURN_KEY,
        )
    )


def downgrade() -> None:
    pass
