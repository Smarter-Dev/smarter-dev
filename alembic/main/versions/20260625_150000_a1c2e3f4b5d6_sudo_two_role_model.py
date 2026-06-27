"""reshape sudo_memberships for the two-role model (hacker / founder)

The founder-tier ladder (r--/rw-/rwx, seat-capped, one-time annual) is gone.
The catalog is now two roles:

* ``hacker``  — $8/mo recurring subscription
* ``founder`` — one-time, pay-what-you-want ($256 minimum)

So ``tier`` becomes ``role`` (with a new check), and the founder-seat column +
its unique constraint are dropped. The table is empty (nothing launched), so
this is a straight reshape with no data migration.

Revision ID: a1c2e3f4b5d6
Revises: 5d1c8f63a72e
Create Date: 2026-06-25 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a1c2e3f4b5d6'
down_revision: Union[str, None] = '5d1c8f63a72e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # tier → role, with the new two-role check.
    op.drop_constraint('ck_sudo_memberships_tier', 'sudo_memberships', type_='check')
    op.alter_column('sudo_memberships', 'tier', new_column_name='role')
    op.create_check_constraint(
        'ck_sudo_memberships_role',
        'sudo_memberships',
        "role IN ('hacker', 'founder')",
    )

    # Founder seats no longer exist.
    op.drop_constraint(
        'uq_sudo_memberships_founder_seat_number',
        'sudo_memberships',
        type_='unique',
    )
    op.drop_column('sudo_memberships', 'founder_seat_number')


def downgrade() -> None:
    import sqlalchemy as sa

    op.add_column(
        'sudo_memberships',
        sa.Column('founder_seat_number', sa.Integer(), nullable=True),
    )
    op.create_unique_constraint(
        'uq_sudo_memberships_founder_seat_number',
        'sudo_memberships',
        ['founder_seat_number'],
    )
    op.drop_constraint('ck_sudo_memberships_role', 'sudo_memberships', type_='check')
    op.alter_column('sudo_memberships', 'role', new_column_name='tier')
    op.create_check_constraint(
        'ck_sudo_memberships_tier',
        'sudo_memberships',
        "tier IN ('read', 'write', 'execute')",
    )
