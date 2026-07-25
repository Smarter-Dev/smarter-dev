"""Add the per-guild scam-link domain list to ``moderation_filter_configs``.

The scam-link heuristic used to carry its messaging-platform hostnames in bot
code, so widening the list needed a deploy. ``scam_link_domains`` moves that
list into config, seeded with the platforms most used as the landing page of a
Discord scam lure. Entries are bare lowercase hostnames (no scheme, no path, no
leading dot); a link matches when its host equals an entry or is a subdomain of
one. Matching is on the full host, not the registrable domain — that is what
makes the multi-label seeds (``chat.whatsapp.com``, ``join.skype.com``) mean
what they say. The server default backfills every existing row.

The seed list is spelled out here rather than imported from the model: a
migration is a frozen historical snapshot and must not change meaning when the
model constant is later edited.

Revision ID: e7a1c4d9b3f2
Revises: d4f6a8c2e5b1
Create Date: 2026-07-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7a1c4d9b3f2"
down_revision: str | None = "d4f6a8c2e5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEEDED_SCAM_LINK_DOMAINS_JSON = (
    '["t.me", "telegram.me", "telegram.dog", "tlgrm.me", "wa.me", '
    '"chat.whatsapp.com", "signal.me", "signal.group", "join.skype.com", '
    '"kik.me", "line.me", "matrix.to", "icq.im"]'
)


def upgrade() -> None:
    op.add_column(
        "moderation_filter_configs",
        sa.Column(
            "scam_link_domains",
            sa.JSON(),
            server_default=_SEEDED_SCAM_LINK_DOMAINS_JSON,
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("moderation_filter_configs", "scam_link_domains")
