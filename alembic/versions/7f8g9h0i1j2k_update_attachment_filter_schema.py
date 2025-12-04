"""update_attachment_filter_schema

Revision ID: 7f8g9h0i1j2k
Revises: 5d6e7f8g9h0i
Create Date: 2025-12-04 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON


# revision identifiers, used by Alembic.
revision = '7f8g9h0i1j2k'
down_revision = '5d6e7f8g9h0i'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Transform old schema to new three-tier schema
    # Old: allowed_extensions, action, warning_message
    # New: ignored_extensions, warn_extensions, warn_message, delete_message

    # Add new columns
    op.add_column('attachment_filter_configs',
                  sa.Column('ignored_extensions', JSON(), nullable=False, server_default='[]'))
    op.add_column('attachment_filter_configs',
                  sa.Column('warn_extensions', JSON(), nullable=False, server_default='[]'))
    op.add_column('attachment_filter_configs',
                  sa.Column('warn_message', sa.Text(), nullable=True))
    op.add_column('attachment_filter_configs',
                  sa.Column('delete_message', sa.Text(), nullable=True))

    # Copy warning_message to delete_message (since old schema used it for deletions)
    op.execute("""
        UPDATE attachment_filter_configs
        SET delete_message = warning_message
        WHERE warning_message IS NOT NULL
    """)

    # Copy allowed_extensions to ignored_extensions (old allowlist becomes new ignorelist)
    op.execute("""
        UPDATE attachment_filter_configs
        SET ignored_extensions = allowed_extensions
    """)

    # Drop old columns
    op.drop_column('attachment_filter_configs', 'allowed_extensions')
    op.drop_column('attachment_filter_configs', 'action')
    op.drop_column('attachment_filter_configs', 'warning_message')


def downgrade() -> None:
    # Add back old columns
    op.add_column('attachment_filter_configs',
                  sa.Column('allowed_extensions', JSON(), nullable=False, server_default='[]'))
    op.add_column('attachment_filter_configs',
                  sa.Column('action', sa.String(20), nullable=False, server_default='delete'))
    op.add_column('attachment_filter_configs',
                  sa.Column('warning_message', sa.Text(), nullable=True))

    # Copy data back
    op.execute("""
        UPDATE attachment_filter_configs
        SET allowed_extensions = ignored_extensions,
            warning_message = delete_message
    """)

    # Drop new columns
    op.drop_column('attachment_filter_configs', 'ignored_extensions')
    op.drop_column('attachment_filter_configs', 'warn_extensions')
    op.drop_column('attachment_filter_configs', 'warn_message')
    op.drop_column('attachment_filter_configs', 'delete_message')
