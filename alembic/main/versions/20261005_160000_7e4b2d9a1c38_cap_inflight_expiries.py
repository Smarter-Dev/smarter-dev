"""Cap stored expiries at the 5-hour content window

``help_conversations.expires_at`` and ``search_result_previews.expires_at``
were written as 48 hours after the row. The window is now 5 hours
(``CONTENT_RETENTION_WINDOW``), and the sweep and the preview page read the
stored expiry, so rows written before this change would keep a public
preview, and the admin cleanup page would keep counting a conversation as
live, for up to 48 hours. This moves every existing expiry in to 5 hours
after the row was written, never later than it was.

Downgrade leaves the expiries as they are: the old ones were only an upper
bound, and a row past 5 hours has already been swept.

Revision ID: 7e4b2d9a1c38
Revises: 2b028ca5a19f
Create Date: 2026-10-05 16:00:00.000000

"""

from __future__ import annotations

from alembic import op

revision: str = "7e4b2d9a1c38"
down_revision: str | None = "2b028ca5a19f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("help_conversations", "search_result_previews"):
        op.execute(
            f"UPDATE {table} "
            "SET expires_at = created_at + INTERVAL '5 hours' "
            "WHERE expires_at > created_at + INTERVAL '5 hours'"
        )


def downgrade() -> None:
    pass
