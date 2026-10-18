"""move web Chat's configured compaction model from GPT-6 Luna to Claude Haiku 5.5

Zech moved every non-chat use of GPT-6 Luna to Claude Haiku 5.5 on 2026-10-17
(#199), chat compaction included; Chat itself stays on Luna. Discord's
compactor is a code default (``chat_compaction.DEFAULT_COMPACT_MODEL``), but web
Chat's lives in ``chat_settings.compaction_model_key``, which the admin settings
page writes, so a new default in ``web/chat/settings.py`` reaches only a fresh
database. This moves the live row.

Only a row still naming ``gpt-6-luna`` moves: any other choice an administrator
made stands. The rows it moves are recorded in ``_MOVED`` (a marker table no
model maps, so autogenerate never sees it), and the downgrade sends back to
Luna only those rows, and only while they still name Haiku. The ids recorded
are the ones the UPDATE itself returns (through a CTE on Postgres), so a row
changed by an admin save between a read and the write is never recorded as
moved: a database that had chosen Haiku itself before the upgrade keeps it,
and so does one an administrator has changed since. The fallback (Gemini 3.8
Flash) is left alone, and so are the modes that compact on the conversation's own model (``configured_compaction``
false). Haiku 5.5 needs no ``chat_catalog_models`` row to serve as an auxiliary
model; ``validate_settings_input`` asks only that it be priced.

Revision ID: d8a3f1c6e9b2
Revises: c5e1a8d3f7b2
Create Date: 2026-10-17 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8a3f1c6e9b2"
down_revision: str | None = "c5e1a8d3f7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LUNA = "gpt-6-luna"
_HAIKU = "claude-haiku-5-5"
_MOVED = "migration_d8a3f1c6e9b2_moved_settings"


def upgrade() -> None:
    moved = op.create_table(_MOVED, sa.Column("id", sa.Integer(), primary_key=True))
    # RETURNING names exactly the rows this UPDATE changed. On Postgres a
    # data-modifying CTE feeds them straight to the marker, one statement that
    # offline ``--sql`` generation can also write out. SQLite (the tests) has
    # no such CTE, so there the ids pass through here (RETURNING: 3.35+).
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            sa.text(
                "WITH moved AS ("
                "UPDATE chat_settings SET compaction_model_key = :new"
                " WHERE compaction_model_key = :old RETURNING id"
                ") INSERT INTO migration_d8a3f1c6e9b2_moved_settings (id)"
                " SELECT id FROM moved"
            ).bindparams(new=_HAIKU, old=_LUNA)
        )
        return
    ids = (
        op.get_bind()
        .execute(
            sa.text(
                "UPDATE chat_settings SET compaction_model_key = :new"
                " WHERE compaction_model_key = :old RETURNING id"
            ).bindparams(new=_HAIKU, old=_LUNA)
        )
        .scalars()
        .all()
    )
    if ids:
        op.bulk_insert(moved, [{"id": row_id} for row_id in ids])


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE chat_settings SET compaction_model_key = :old"
            " WHERE compaction_model_key = :new"
            " AND id IN (SELECT id FROM migration_d8a3f1c6e9b2_moved_settings)"
        ).bindparams(old=_LUNA, new=_HAIKU)
    )
    op.drop_table(_MOVED)
