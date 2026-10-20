"""Add the moderation action gateway's records.

Six tables for ``smarter_dev/web/moderation_gateway``:

* ``moderation_incidents`` — one per incident, with its report's identity;
* ``moderation_incident_links`` — message ids and aliases that find an
  incident, unique per guild;
* ``moderation_incident_entries`` — reasons, notes and evidence. Evidence is
  member content: the hourly retention sweep deletes it after 90 days;
* ``moderation_operations`` — every effect asked of Discord and its outcome,
  unique per guild by idempotency key;
* ``moderation_outbox`` — notices, DMs and announcements that follow an
  effect, committed with it;
* ``moderation_member_locks`` — the lease that serialises actions on one
  member across the bot and the handler workers.

``moderation_actions`` gains nullable columns linking a row to its incident
and operation, plus actor, rule, applied duration, evidence ids and the
Discord audit-log entry. Existing rows keep NULL in all of them. Every
statement waits at most ``LOCK_TIMEOUT`` for a lock.

Revision ID: e4c8b2f6a9d1
Revises: d8a3f1c6e9b2
Create Date: 2026-10-19 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e4c8b2f6a9d1"
down_revision: str | None = "d8a3f1c6e9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    """``created_at`` and ``updated_at``, which ``Base`` gives every model."""
    return tuple(
        sa.Column(
            name,
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        )
        for name in ("created_at", "updated_at")
    )


#: How long a statement here may wait for a table lock. The changes to
#: ``moderation_actions`` take ACCESS EXCLUSIVE until the migration commits;
#: queued behind a long transaction they would stall every read and write of
#: the table. Past this the migration fails and the deploy stops, to be rerun.
LOCK_TIMEOUT = "5s"


def _bound_lock_waits() -> None:
    if op.get_context().dialect.name == "postgresql":
        # SET LOCAL ends with the transaction the migrations run in.
        op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")


def upgrade() -> None:
    _bound_lock_waits()
    op.create_table(
        "moderation_incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("subject_user_id", sa.String(), nullable=True),
        sa.Column("channel_id", sa.String(), nullable=True),
        sa.Column("trigger_message_id", sa.String(), nullable=True),
        sa.Column(
            "opened_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("active_until", sa.Float(), nullable=False),
        sa.Column(
            "revision", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("report_channel_id", sa.String(), nullable=True),
        sa.Column("report_message_id", sa.String(), nullable=True),
        sa.Column(
            "report_revision", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "report_state", sa.String(length=20), server_default="none", nullable=False
        ),
        sa.Column("report_lease_until", sa.Float(), nullable=True),
        sa.Column("report_ping_role_id", sa.String(), nullable=True),
        sa.Column(
            "report_attachment_sent",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "report_pinged",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_moderation_incidents_guild_subject",
        "moderation_incidents",
        ["guild_id", "subject_user_id"],
    )
    op.create_index(
        "ix_moderation_incidents_opened_at", "moderation_incidents", ["opened_at"]
    )

    op.create_table(
        "moderation_incident_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("link_key", sa.String(length=200), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["moderation_incidents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id", "link_key", name="uq_moderation_incident_links_key"
        ),
    )
    op.create_index(
        "ix_moderation_incident_links_incident_id",
        "moderation_incident_links",
        ["incident_id"],
    )

    op.create_table(
        "moderation_incident_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("rule", sa.Text(), nullable=True),
        sa.Column("target_user_id", sa.String(), nullable=True),
        sa.Column("channel_id", sa.String(), nullable=True),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["moderation_incidents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_moderation_incident_entries_incident_id",
        "moderation_incident_entries",
        ["incident_id"],
    )
    op.create_index(
        "ix_moderation_incident_entries_created_at",
        "moderation_incident_entries",
        ["created_at"],
    )
    op.create_index(
        "ix_moderation_incident_entries_fingerprint",
        "moderation_incident_entries",
        ["guild_id", "target_user_id", "fingerprint"],
    )

    op.create_table(
        "moderation_operations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("target_user_id", sa.String(), nullable=False),
        sa.Column("target_username", sa.String(), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.String(), nullable=True),
        sa.Column("message_ids", sa.JSON(), nullable=True),
        sa.Column("requested", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("claim_key", sa.String(length=200), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.Column("released_at", sa.Float(), nullable=True),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("audit_entry_id", sa.String(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["moderation_incidents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "guild_id", "idempotency_key", name="uq_moderation_operations_key"
        ),
    )
    op.create_index(
        "ix_moderation_operations_incident_id",
        "moderation_operations",
        ["incident_id"],
    )
    op.create_index(
        "ix_moderation_operations_target",
        "moderation_operations",
        ["guild_id", "target_user_id", "kind", "state"],
    )
    op.create_index(
        "ix_moderation_operations_state", "moderation_operations", ["state"]
    )

    op.create_table(
        "moderation_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("target_user_id", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.Float(), nullable=True),
        *_timestamps(),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["moderation_incidents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", "dedupe_key", name="uq_moderation_outbox_key"),
    )
    op.create_index(
        "ix_moderation_outbox_incident_id", "moderation_outbox", ["incident_id"]
    )
    op.create_index("ix_moderation_outbox_state", "moderation_outbox", ["state"])
    op.create_index(
        "ix_moderation_outbox_created_at", "moderation_outbox", ["created_at"]
    )

    op.create_table(
        "moderation_member_locks",
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("guild_id", "user_id"),
    )

    op.add_column(
        "moderation_actions",
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "moderation_actions",
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "moderation_actions", sa.Column("actor", sa.String(length=100), nullable=True)
    )
    op.add_column("moderation_actions", sa.Column("rule", sa.Text(), nullable=True))
    op.add_column(
        "moderation_actions",
        sa.Column("applied_duration_seconds", sa.Integer(), nullable=True),
    )
    op.add_column("moderation_actions", sa.Column("evidence", sa.JSON(), nullable=True))
    op.add_column(
        "moderation_actions", sa.Column("audit_entry_id", sa.String(), nullable=True)
    )
    op.create_foreign_key(
        "fk_moderation_actions_incident_id_moderation_incidents",
        "moderation_actions",
        "moderation_incidents",
        ["incident_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_moderation_actions_operation_id_moderation_operations",
        "moderation_actions",
        "moderation_operations",
        ["operation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_moderation_actions_incident_id", "moderation_actions", ["incident_id"]
    )
    op.create_unique_constraint(
        "uq_moderation_actions_operation_id", "moderation_actions", ["operation_id"]
    )


def downgrade() -> None:
    _bound_lock_waits()
    op.drop_constraint(
        "uq_moderation_actions_operation_id", "moderation_actions", type_="unique"
    )
    op.drop_index("ix_moderation_actions_incident_id", table_name="moderation_actions")
    op.drop_constraint(
        "fk_moderation_actions_operation_id_moderation_operations",
        "moderation_actions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_moderation_actions_incident_id_moderation_incidents",
        "moderation_actions",
        type_="foreignkey",
    )
    for column in (
        "audit_entry_id",
        "evidence",
        "applied_duration_seconds",
        "rule",
        "actor",
        "operation_id",
        "incident_id",
    ):
        op.drop_column("moderation_actions", column)
    op.drop_table("moderation_member_locks")
    op.drop_index("ix_moderation_outbox_created_at", table_name="moderation_outbox")
    op.drop_index("ix_moderation_outbox_state", table_name="moderation_outbox")
    op.drop_index("ix_moderation_outbox_incident_id", table_name="moderation_outbox")
    op.drop_table("moderation_outbox")
    op.drop_index("ix_moderation_operations_state", table_name="moderation_operations")
    op.drop_index("ix_moderation_operations_target", table_name="moderation_operations")
    op.drop_index(
        "ix_moderation_operations_incident_id", table_name="moderation_operations"
    )
    op.drop_table("moderation_operations")
    op.drop_index(
        "ix_moderation_incident_entries_fingerprint",
        table_name="moderation_incident_entries",
    )
    op.drop_index(
        "ix_moderation_incident_entries_created_at",
        table_name="moderation_incident_entries",
    )
    op.drop_index(
        "ix_moderation_incident_entries_incident_id",
        table_name="moderation_incident_entries",
    )
    op.drop_table("moderation_incident_entries")
    op.drop_index(
        "ix_moderation_incident_links_incident_id",
        table_name="moderation_incident_links",
    )
    op.drop_table("moderation_incident_links")
    op.drop_index(
        "ix_moderation_incidents_opened_at", table_name="moderation_incidents"
    )
    op.drop_index(
        "ix_moderation_incidents_guild_subject", table_name="moderation_incidents"
    )
    op.drop_table("moderation_incidents")
