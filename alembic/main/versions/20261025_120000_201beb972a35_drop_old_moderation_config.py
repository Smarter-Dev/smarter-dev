"""Drop the old per-actor moderation config the policy replaced (#242).

Every moderation setting now lives in the guild's moderation policy
(``moderation_policies``, #241); nothing reads these any more:

* the table ``attachment_filter_configs`` (the attachment rule's tiers and
  notices; now ``rules.attachment_filter``);
* the table ``moderation_configs`` (the staff-ping triage; now ``triage``);
* 21 columns of ``moderation_filter_configs``: the scam-link domains, the
  staff-exempt roles, the spam engine's switch and thresholds, the mod alert,
  ping-role and scam-log settings, and the spam ring's switch, windows and
  report channel (now ``rules.spam_engine``, ``rules.spam_ring``,
  ``protected_role_ids`` and ``destinations``). The table keeps ``guild_id``,
  the content filter's own columns (``content_filter_enabled``,
  ``blocked_tlds``, ``invite_filter_enabled``,
  ``invite_filter_exempt_category_ids``, ``webhook_killer_enabled``,
  ``mod_log_channel_id``) and its timestamps.

Moderation history is not touched: ``moderation_actions``, the incidents,
operations, outbox and shadow decisions, ``spam_ring_incidents``,
``moderation_ownership`` and the policy tables stay as they are.

**This drops data.** ``downgrade`` recreates the tables and columns with
their original types, defaults, server defaults, nullability, primary keys
and indexes (as ``initial_main_schema``, ``adopt_legacy_bot_tables``,
``d4f6a8c2e5b1``, ``e7a1c4d9b3f2``, ``f2b7d4e9c1a6`` and ``c5e1a8d3f7b2``
left them), so an undo restores the schema; the rows come back empty, and
recreated columns hold their server defaults. The values themselves come
back only from a database backup taken before this migration.

Each statement waits at most 5 s for a lock.

Revision ID: 201beb972a35
Revises: d3a8f5c2e9b1
Create Date: 2026-10-25 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "201beb972a35"
down_revision: str | None = "d3a8f5c2e9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FILTER_TABLE = "moderation_filter_configs"

#: The seed ``e7a1c4d9b3f2`` gave ``scam_link_domains``, frozen here too.
_SEEDED_SCAM_LINK_DOMAINS_JSON = (
    '["t.me", "telegram.me", "telegram.dog", "tlgrm.me", "wa.me", '
    '"chat.whatsapp.com", "signal.me", "signal.group", "join.skype.com", '
    '"kik.me", "line.me", "matrix.to", "icq.im"]'
)


def _bound_lock_waits() -> None:
    # As in e4c8b2f6a9d1: a lock wait fails the migration, never stalls a table.
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")


def _integer(name: str, default: str) -> sa.Column:
    return sa.Column(
        name, sa.Integer(), server_default=sa.text(default), nullable=False
    )


def _dropped_filter_columns() -> list[sa.Column]:
    """The columns this drops, as the migrations that made them left them."""
    return [
        # e7a1c4d9b3f2
        sa.Column(
            "scam_link_domains",
            sa.JSON(),
            server_default=_SEEDED_SCAM_LINK_DOMAINS_JSON,
            nullable=False,
        ),
        # d4f6a8c2e5b1
        sa.Column(
            "staff_exempt_role_ids",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "spam_engine_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        _integer("message_rate_threshold", "5"),
        _integer("message_rate_window_seconds", "5"),
        _integer("channel_spread_threshold", "3"),
        _integer("channel_spread_window_seconds", "15"),
        _integer("duplicate_message_min_length", "15"),
        _integer("duplicate_message_window_seconds", "60"),
        _integer("mass_mention_window_seconds", "15"),
        _integer("warning_reoffense_window_seconds", "120"),
        _integer("mute_duration_seconds", "86400"),
        sa.Column("mod_alert_channel_id", sa.String(), nullable=True),
        sa.Column("mod_ping_role_id", sa.String(), nullable=True),
        sa.Column("scam_log_channel_id", sa.String(), nullable=True),
        # f2b7d4e9c1a6 (and c5e1a8d3f7b2: new_member_days defaults to 2)
        sa.Column(
            "spam_ring_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        _integer("spam_ring_new_member_days", "2"),
        _integer("spam_ring_timeout_seconds", "604800"),
        _integer("spam_ring_lookback_minutes", "60"),
        _integer("spam_ring_followup_minutes", "15"),
        sa.Column("spam_ring_report_channel_id", sa.String(), nullable=True),
    ]


#: Every column of ``moderation_filter_configs`` this drops.
DROPPED_FILTER_COLUMNS: tuple[str, ...] = tuple(
    column.name for column in _dropped_filter_columns()
)
DROPPED_TABLES: tuple[str, ...] = ("attachment_filter_configs", "moderation_configs")


def upgrade() -> None:
    _bound_lock_waits()
    op.drop_index(
        "ix_attachment_filter_configs_guild_id",
        table_name="attachment_filter_configs",
    )
    op.drop_table("attachment_filter_configs")
    op.drop_table("moderation_configs")
    for name in DROPPED_FILTER_COLUMNS:
        op.drop_column(FILTER_TABLE, name)


def downgrade() -> None:
    _bound_lock_waits()
    for column in _dropped_filter_columns():
        op.add_column(FILTER_TABLE, column)
    # initial_main_schema (6a448f2c9bb7)
    op.create_table(
        "moderation_configs",
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("monitored_role_ids", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column(
            "enabled_tools",
            sa.JSON(),
            server_default='["timeout", "purge", "delete"]',
            nullable=False,
        ),
        sa.Column("response_channel_id", sa.String(), nullable=True),
        sa.Column("context_message_limit", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_moderation_configs")),
    )
    # adopt_legacy_bot_tables_into_skrift (cfcaa2cbf2b0)
    op.create_table(
        "attachment_filter_configs",
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("ignored_extensions", sa.JSON(), nullable=False),
        sa.Column("warn_extensions", sa.JSON(), nullable=False),
        sa.Column("warn_message", sa.Text(), nullable=True),
        sa.Column("delete_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("guild_id", name=op.f("pk_attachment_filter_configs")),
    )
    op.create_index(
        "ix_attachment_filter_configs_guild_id",
        "attachment_filter_configs",
        ["guild_id"],
        unique=False,
    )
