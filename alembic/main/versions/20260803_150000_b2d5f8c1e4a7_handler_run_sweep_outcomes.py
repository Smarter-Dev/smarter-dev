"""allow 'skipped' and 'rearmed' handler_runs outcomes

Two new audit outcomes, neither of which is a normal fire:

- ``skipped`` — a fire job retried after an earlier attempt had already entered
  the script, so the retry declined to re-run it rather than duplicate its
  emits (see ``handler_caps.claim_fire_attempt``). Fire jobs only started
  retrying at all in this change; before it, ``max_attempts=1`` meant a single
  transient failure dead-lettered the fire outright.

- ``rearmed`` — not a fire: the schedule sweep recording that it revived a
  recurring chain that had stopped firing (see ``handler_sweep``). Written
  against the handler so the re-arm shows up in the admin's per-handler log
  next to the failures that preceded it.

Both land in the existing CHECK constraint rather than a new column, so the
admin error log picks them up for free — it selects on ``outcome != 'ok'``.

Revision ID: b2d5f8c1e4a7
Revises: a1c4e7b9d2f3
Create Date: 2026-08-03 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2d5f8c1e4a7"
down_revision: Union[str, None] = "a1c4e7b9d2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "ck_handler_runs_outcome"
_TABLE = "handler_runs"

_OLD = "outcome IN ('ok', 'cap_exceeded', 'error', 'rejected')"
_NEW = "outcome IN ('ok', 'cap_exceeded', 'error', 'rejected', 'skipped', 'rearmed')"


def _replace_check(condition: str) -> None:
    """Swap the outcome CHECK for one allowing ``condition``.

    Raw SQL on purpose. ``op.drop_constraint``/``create_check_constraint`` run
    the name through the metadata naming convention, which prefixes
    ``ck_%(table_name)s_`` and turns the real constraint name into
    ``ck_handler_runs_ck_handler_runs_outcome`` — an object that doesn't exist.
    """
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CONSTRAINT}"))
    op.execute(
        sa.text(
            f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} CHECK ({condition})"
        )
    )


def upgrade() -> None:
    _replace_check(_NEW)


def downgrade() -> None:
    # Rows written under the new outcomes would violate the old constraint, so
    # fold them into the nearest legacy value rather than letting the downgrade
    # fail on live data. Both are non-fires, and 'rejected' is the existing
    # "this handler did not run" outcome.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET outcome = 'rejected'"
            " WHERE outcome IN ('skipped', 'rearmed')"
        )
    )
    _replace_check(_OLD)
