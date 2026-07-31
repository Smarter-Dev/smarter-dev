"""Add durable Smarter Dev web Chat product.

Revision ID: e8a1c2d3f4b5
Revises: d7f2a9c4e6b1
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e8a1c2d3f4b5"
down_revision = "d7f2a9c4e6b1"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSON = sa.JSON()


def stamps():
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.create_table(
        "chat_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("default_model_key", sa.String(100), nullable=False),
        sa.Column("default_reasoning", sa.String(16)),
        sa.Column("default_intelligence_mode", sa.String(40), nullable=False),
        sa.Column("summarizer_model_key", sa.String(100), nullable=False),
        sa.Column("summarizer_fallback_model_key", sa.String(100)),
        sa.Column("compaction_model_key", sa.String(100), nullable=False),
        sa.Column("compaction_fallback_model_key", sa.String(100)),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_by_user_id", UUID),
        *stamps(),
        sa.CheckConstraint("id = 1", name="ck_chat_settings_chat_settings_singleton"),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
    )
    op.create_table(
        "chat_catalog_models",
        sa.Column("model_key", sa.String(100), primary_key=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("cost_tier", sa.String(16), server_default="medium", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        *stamps(),
        sa.CheckConstraint(
            "cost_tier IN ('low','medium','high','ultra')",
            name="ck_chat_catalog_models_chat_catalog_cost_tier",
        ),
    )
    op.create_table(
        "chat_spend_limits",
        sa.Column("tier", sa.String(16), primary_key=True),
        sa.Column("four_hour_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("daily_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("weekly_usd", sa.Numeric(12, 6), nullable=False),
        *stamps(),
        sa.CheckConstraint(
            "tier IN ('hacker','r','rw','rwx')",
            name="ck_chat_spend_limits_chat_spend_tier",
        ),
        sa.CheckConstraint(
            "four_hour_usd >= 0 AND daily_usd >= 0 AND weekly_usd >= 0",
            name="ck_chat_spend_limits_chat_spend_nonnegative",
        ),
    )
    op.create_table(
        "web_chat_conversations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("owner_user_id", UUID, nullable=False),
        sa.Column("intelligence_mode", sa.String(40), nullable=False),
        sa.Column("selected_model_key", sa.String(100), nullable=False),
        sa.Column("reasoning_level", sa.String(16)),
        sa.Column("title", sa.Text()),
        sa.Column("status", sa.String(24), server_default="idle", nullable=False),
        sa.Column("next_sequence", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "current_context_tokens", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("context_state", JSON, server_default="[]", nullable=False),
        sa.Column("context_revision", sa.Integer(), server_default="0", nullable=False),
        *stamps(),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "intelligence_mode IN ('maximize_efficiency','efficient','intelligence','maximize_intelligence','ultra_intelligence')",
            name="ck_web_chat_conversations_web_chat_intelligence_mode",
        ),
    )
    op.create_index(
        "ix_web_chat_conversations_owner_updated",
        "web_chat_conversations",
        ["owner_user_id", "updated_at"],
    )
    op.create_table(
        "chat_spend_windows",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "overage_cost_usd", sa.Numeric(14, 8), server_default="0", nullable=False
        ),
        *stamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "ends_at > starts_at", name="ck_chat_spend_windows_chat_spend_window_bounds"
        ),
    )
    op.create_index(
        "ix_chat_spend_windows_user_starts",
        "chat_spend_windows",
        ["user_id", "starts_at"],
    )
    op.create_table(
        "web_chat_turns",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("submission_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(20), server_default="message", nullable=False),
        sa.Column("regenerates_turn_id", UUID),
        sa.Column("response_version_group", UUID, nullable=False),
        sa.Column("response_sequence", sa.Integer(), nullable=False),
        sa.Column("model_key", sa.String(100), nullable=False),
        sa.Column("reasoning_level", sa.String(16)),
        sa.Column("status", sa.String(24), server_default="submitted", nullable=False),
        sa.Column("stop_requested_at", sa.DateTime(timezone=True)),
        sa.Column("cutoff_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "final_response_used",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("searches", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "subagent_attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("four_hour_window_id", UUID),
        sa.Column("worker_lease_token", sa.String(64)),
        sa.Column("worker_lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        *stamps(),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["web_chat_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["regenerates_turn_id"], ["web_chat_turns.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["four_hour_window_id"], ["chat_spend_windows.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_web_chat_turn_sequence"
        ),
        sa.UniqueConstraint(
            "conversation_id", "submission_key", name="uq_web_chat_turn_submission"
        ),
    )
    op.create_index(
        "uq_web_chat_turn_one_active",
        "web_chat_turns",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('submitted','queued','running','stopping')"
        ),
    )
    op.create_table(
        "web_chat_messages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("turn_id", UUID, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("model_message", JSON),
        sa.Column("tool_call_id", sa.String(200)),
        sa.Column("tool_name", sa.String(100)),
        sa.Column("version_group", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("stopped", sa.Boolean(), server_default=sa.false(), nullable=False),
        *stamps(),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["web_chat_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["web_chat_turns.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "version_group", "version_number", name="uq_web_chat_message_version"
        ),
    )
    op.create_index(
        "ix_web_chat_messages_conversation_sequence",
        "web_chat_messages",
        ["conversation_id", "sequence"],
    )
    op.create_index(
        "uq_web_chat_message_active_version",
        "web_chat_messages",
        ["version_group"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "web_chat_model_changes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("owner_user_id", UUID, nullable=False),
        sa.Column("from_model_key", sa.String(100), nullable=False),
        sa.Column("to_model_key", sa.String(100), nullable=False),
        sa.Column("warning", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *stamps(),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["web_chat_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "web_chat_attachments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("owner_user_id", UUID, nullable=False),
        sa.Column("turn_id", UUID),
        sa.Column("storage_key", sa.String(64), nullable=False, unique=True),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("summarization_instruction", sa.Text()),
        sa.Column("status", sa.String(24), server_default="ready", nullable=False),
        *stamps(),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["web_chat_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["web_chat_turns.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "web_chat_compactions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("through_sequence", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("original_messages", JSON, server_default="[]", nullable=False),
        sa.Column("compacted_messages", JSON, server_default="[]", nullable=False),
        sa.Column("version_fingerprint", sa.String(64), nullable=False),
        sa.Column("model_key", sa.String(100), nullable=False),
        sa.Column("reasoning_level", sa.String(16)),
        sa.Column("status", sa.String(20), server_default="complete", nullable=False),
        sa.Column("context_revision", sa.Integer(), nullable=False),
        *stamps(),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["web_chat_conversations.id"], ondelete="CASCADE"
        ),
    )
    op.create_table(
        "web_chat_subagents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("root_turn_id", UUID, nullable=False),
        sa.Column("parent_subagent_id", UUID),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("lineage", JSON, server_default="[]", nullable=False),
        sa.Column("spawn_ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reasoning_level", sa.String(16)),
        sa.Column("job_id", sa.String(200)),
        sa.Column("session_id", sa.String(200)),
        sa.Column("result", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 8), server_default="0", nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("lease_fence", sa.Integer()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        *stamps(),
        sa.ForeignKeyConstraint(
            ["root_turn_id"], ["web_chat_turns.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_subagent_id"], ["web_chat_subagents.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("root_turn_id", "name", name="uq_web_chat_subagent_name"),
    )
    op.create_table(
        "chat_spend_reservations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("operation_key", sa.String(200), nullable=False, unique=True),
        sa.Column("operation_type", sa.String(40), nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("conversation_id", UUID),
        sa.Column("root_turn_id", UUID),
        sa.Column("window_id", UUID, nullable=False),
        sa.Column("intelligence_mode", sa.String(40), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(14, 8), nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        *stamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["window_id"], ["chat_spend_windows.id"], ondelete="CASCADE"
        ),
    )
    op.create_table(
        "usage_cost_rows",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("operation_key", sa.String(200), nullable=False, unique=True),
        sa.Column("product_mode", sa.String(16), nullable=False),
        sa.Column("operation_type", sa.String(40), nullable=False),
        sa.Column("user_id", UUID),
        sa.Column("discord_user_id", sa.String(32)),
        sa.Column("conversation_id", UUID),
        sa.Column("root_turn_id", UUID),
        sa.Column("subagent_id", UUID),
        sa.Column("provider_key", sa.String(40), nullable=False),
        sa.Column("catalog_model_key", sa.String(100), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("reasoning_level", sa.String(16)),
        sa.Column("intelligence_mode", sa.String(40)),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cache_read_tokens", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "cache_write_tokens", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("cost_usd", sa.Numeric(14, 8), server_default="0", nullable=False),
        sa.Column(
            "overage_cost_usd", sa.Numeric(14, 8), server_default="0", nullable=False
        ),
        sa.Column("four_hour_window_id", UUID),
        sa.Column(
            "metered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("details", JSON, server_default="{}", nullable=False),
        *stamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["four_hour_window_id"], ["chat_spend_windows.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "product_mode IN ('resources','chat','discord')",
            name="ck_usage_cost_rows_usage_product_mode",
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 AND cache_write_tokens >= 0",
            name="ck_usage_cost_rows_usage_tokens_nonnegative",
        ),
    )
    op.create_index(
        "ix_usage_cost_rows_product_metered",
        "usage_cost_rows",
        ["product_mode", "metered_at"],
    )
    op.create_index(
        "ix_usage_cost_rows_user_metered", "usage_cost_rows", ["user_id", "metered_at"]
    )
    op.create_table(
        "work_dispatches",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("job_type", sa.String(80), nullable=False),
        sa.Column("aggregate_id", UUID, nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("queue", sa.String(40), server_default="agents", nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("worker_job_id", sa.String(200)),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        *stamps(),
        sa.UniqueConstraint(
            "job_type", "aggregate_id", name="uq_work_dispatch_aggregate"
        ),
    )
    op.create_index(
        "ix_work_dispatch_pending", "work_dispatches", ["status", "next_attempt_at"]
    )
    op.create_table(
        "resource_agent_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("owner_user_id", UUID, nullable=False),
        sa.Column("user_sequence", sa.Integer(), nullable=False),
        sa.Column("submission_key", sa.String(128), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), server_default="submitted", nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("worker_lease_token", sa.String(64)),
        sa.Column("worker_lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *stamps(),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "conversation_id", "user_sequence", name="uq_resource_agent_run_turn"
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "submission_key",
            name="uq_resource_agent_run_submission",
        ),
    )
    op.create_table(
        "account_deletion_requests",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, nullable=False, unique=True),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("subscription_ids", JSON, server_default="[]", nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *stamps(),
    )
    op.create_table(
        "web_chat_runtime_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("turn_id", UUID, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("payload", JSON, server_default="{}", nullable=False),
        *stamps(),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["web_chat_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["web_chat_turns.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "turn_id", "sequence", name="uq_web_chat_runtime_event_sequence"
        ),
    )
    # Enforce immutable product mode at the durable boundary.
    op.execute("""
      CREATE FUNCTION prevent_web_chat_mode_change() RETURNS trigger AS $$
      BEGIN
        IF NEW.intelligence_mode IS DISTINCT FROM OLD.intelligence_mode THEN
          RAISE EXCEPTION 'web chat intelligence mode is immutable';
        END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql;
    """)
    op.execute("""
      CREATE TRIGGER trg_web_chat_mode_immutable BEFORE UPDATE ON web_chat_conversations
      FOR EACH ROW EXECUTE FUNCTION prevent_web_chat_mode_change();
    """)
    # Defaults are conservative and immediately operable; admins can enable more.
    op.execute("""
      INSERT INTO chat_settings
        (id, default_model_key, default_reasoning, default_intelligence_mode,
         summarizer_model_key, summarizer_fallback_model_key,
         compaction_model_key, compaction_fallback_model_key, revision)
      VALUES (1, 'gemini-3-1-flash-lite', 'medium', 'efficient',
              'poolside-laguna-s-2-1', 'gemini-3-1-flash-lite',
              'gemini-3-1-flash-lite', NULL, 1)
    """)
    op.execute("""
      INSERT INTO chat_catalog_models (model_key, enabled, cost_tier, sort_order) VALUES
        ('gemini-3-1-flash-lite', true, 'low', 0),
        ('poolside-laguna-s-2-1', false, 'low', 1)
    """)
    op.execute("""
      INSERT INTO chat_spend_limits (tier, four_hour_usd, daily_usd, weekly_usd) VALUES
        ('hacker', 1.000000, 2.000000, 8.000000),
        ('r',      2.000000, 4.000000, 16.000000),
        ('rw',     4.000000, 8.000000, 32.000000),
        ('rwx',    8.000000, 16.000000, 64.000000)
    """)
    # Normalize existing Discord costs without changing Discord retention or
    # operational tables. Source-derived keys make this rerunnable/idempotent.
    op.execute(r"""
      INSERT INTO usage_cost_rows
        (id, operation_key, product_mode, operation_type, discord_user_id,
         conversation_id, root_turn_id, provider_key, catalog_model_key,
         model_id, reasoning_level, input_tokens, output_tokens,
         cache_read_tokens, cache_write_tokens, cost_usd, overage_cost_usd,
         metered_at, details)
      SELECT gen_random_uuid(), 'discord\:turn\:' || t.id || '\:primary',
         'discord', 'primary', e.activation_user_id, t.engagement_id, t.id,
         'unknown', COALESCE(t.chat_model_name, 'unknown'),
         COALESCE(t.chat_model_name, 'unknown'), t.chat_reasoning_level,
         t.chat_tokens_input, t.chat_tokens_output,
         COALESCE(t.chat_cache_read_tokens, 0), COALESCE(t.chat_cache_write_tokens, 0),
         t.chat_cost_usd, 0, t.started_at, '{}'::jsonb
      FROM chat_agent_turns t
      JOIN chat_agent_engagements e ON e.id = t.engagement_id
      WHERE t.chat_model_name IS NOT NULL
      ON CONFLICT (operation_key) DO NOTHING
    """)
    op.execute(r"""
      INSERT INTO usage_cost_rows
        (id, operation_key, product_mode, operation_type, discord_user_id,
         conversation_id, root_turn_id, provider_key, catalog_model_key,
         model_id, input_tokens, output_tokens, cache_read_tokens,
         cache_write_tokens, cost_usd, overage_cost_usd, metered_at, details)
      SELECT gen_random_uuid(), 'discord\:turn\:' || t.id || '\:voice',
         'discord', 'voice', e.activation_user_id, t.engagement_id, t.id,
         'google', COALESCE(t.voice_model_name, 'unknown'),
         COALESCE(t.voice_model_name, 'unknown'), t.voice_tokens_input,
         t.voice_tokens_output, 0, 0, t.voice_cost_usd, 0, t.started_at,
         '{}'::jsonb
      FROM chat_agent_turns t
      JOIN chat_agent_engagements e ON e.id = t.engagement_id
      WHERE t.voice_model_name IS NOT NULL
      ON CONFLICT (operation_key) DO NOTHING
    """)
    op.execute(r"""
      INSERT INTO usage_cost_rows
        (id, operation_key, product_mode, operation_type, discord_user_id,
         conversation_id, root_turn_id, provider_key, catalog_model_key,
         model_id, reasoning_level, input_tokens, output_tokens,
         cache_read_tokens, cache_write_tokens, cost_usd, overage_cost_usd,
         metered_at, details)
      SELECT gen_random_uuid(), 'discord\:turn\:' || t.id || '\:compaction\:' || c.id,
         'discord', 'compaction', e.activation_user_id, t.engagement_id, t.id,
         'unknown', COALESCE(c.summarizer_model_name, 'unknown'),
         COALESCE(c.summarizer_model_name, 'unknown'), c.summarizer_reasoning_level,
         c.summarizer_tokens_input, c.summarizer_tokens_output,
         COALESCE(c.summarizer_cache_read_tokens, 0), COALESCE(c.summarizer_cache_write_tokens, 0),
         c.summarizer_cost_usd, 0, t.started_at,
         jsonb_build_object('lineage_event_id', c.id)
      FROM chat_agent_compaction_events c
      JOIN chat_agent_turns t ON t.id = c.turn_id
      JOIN chat_agent_engagements e ON e.id = t.engagement_id
      WHERE c.summarizer_model_name IS NOT NULL
      ON CONFLICT (operation_key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_web_chat_mode_immutable ON web_chat_conversations"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_web_chat_mode_change()")
    for name in (
        "web_chat_runtime_events",
        "account_deletion_requests",
        "resource_agent_runs",
        "work_dispatches",
        "usage_cost_rows",
        "chat_spend_reservations",
        "web_chat_subagents",
        "web_chat_compactions",
        "web_chat_attachments",
        "web_chat_model_changes",
        "web_chat_messages",
        "web_chat_turns",
        "chat_spend_windows",
        "web_chat_conversations",
        "chat_spend_limits",
        "chat_catalog_models",
        "chat_settings",
    ):
        op.drop_table(name)
