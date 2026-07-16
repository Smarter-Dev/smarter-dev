"""drop security_logs.api_key_id foreign key

During the key-system migration (docs/v2/legacy-sunset/01-skrift-api-keys.md)
security_logs.api_key_id may hold Skrift-native key IDs that live in the main
DB's skrift.api_keys table, not in legacy public.api_keys. The column becomes
a plain correlation column (kept indexed for rate-limit window counting).

Revision ID: 3f8d2c5b9a41
Revises: 2b7c9e4a1f83
Create Date: 2026-07-16 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '3f8d2c5b9a41'
down_revision: str | None = '2b7c9e4a1f83'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        'fk_security_logs_api_key_id_api_keys',
        'security_logs',
        type_='foreignkey',
    )


def downgrade() -> None:
    # NOTE: fails if security_logs rows reference Skrift-native key IDs that
    # do not exist in legacy public.api_keys; null those rows first.
    op.create_foreign_key(
        'fk_security_logs_api_key_id_api_keys',
        'security_logs',
        'api_keys',
        ['api_key_id'],
        ['id'],
        ondelete='SET NULL',
    )
