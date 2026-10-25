"""#242: the migration that drops the old per-actor moderation config.

Run everywhere: what the upgrade drops (exactly the two tables and 21
columns, nothing of the moderation history), and that the downgrade puts each
one back as the migrations that made it left it (type, nullability, server
default, primary key, index). On Postgres (``POSTGRES_MIGRATION_TEST_URL``,
see ``moderation_gateway_migration_test``): upgrade to head, downgrade one
step, upgrade again, reading the schema each time.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text

from tests.web.moderation_gateway_migration_test import _SETTINGS_PLACEHOLDERS
from tests.web.moderation_gateway_migration_test import REPO_ROOT
from tests.web.moderation_gateway_migration_test import _column_differences
from tests.web.moderation_gateway_migration_test import _run
from tests.web.moderation_gateway_migration_test import migrated  # noqa: F401
from tests.web.moderation_gateway_migration_test import needs_postgres

VERSIONS = REPO_ROOT / "alembic/main/versions"
DROP = VERSIONS / "20261025_120000_201beb972a35_drop_old_moderation_config.py"
FILTER = "moderation_filter_configs"

#: The task's list (#242), spelled out so the migration cannot drift from it.
DROPPED_COLUMNS = [
    "scam_link_domains",
    "staff_exempt_role_ids",
    "spam_engine_enabled",
    "message_rate_threshold",
    "message_rate_window_seconds",
    "channel_spread_threshold",
    "channel_spread_window_seconds",
    "duplicate_message_min_length",
    "duplicate_message_window_seconds",
    "mass_mention_window_seconds",
    "warning_reoffense_window_seconds",
    "mute_duration_seconds",
    "mod_alert_channel_id",
    "mod_ping_role_id",
    "scam_log_channel_id",
    "spam_ring_enabled",
    "spam_ring_new_member_days",
    "spam_ring_timeout_seconds",
    "spam_ring_lookback_minutes",
    "spam_ring_followup_minutes",
    "spam_ring_report_channel_id",
]
KEPT_COLUMNS = {
    "guild_id",
    "content_filter_enabled",
    "blocked_tlds",
    "invite_filter_enabled",
    "invite_filter_exempt_category_ids",
    "webhook_killer_enabled",
    "mod_log_channel_id",
    "created_at",
    "updated_at",
}
DROPPED_TABLES = {"attachment_filter_configs", "moderation_configs"}


def _migration(revision: str) -> Path:
    [path] = VERSIONS.glob(f"*_{revision}_*.py")
    return path


def _shape(column: sa.Column) -> tuple:
    default = column.server_default
    if default is not None:
        default = getattr(default.arg, "text", default.arg)
        default = str(default)
    return (type(column.type).__name__, bool(column.nullable), default)


def _as_made() -> dict[str, tuple]:
    """Each dropped column as its own migrations left it."""
    created = _run(_migration("d4f6a8c2e5b1")).created[FILTER]
    added = {
        **_run(_migration("e7a1c4d9b3f2")).added[FILTER],
        **_run(_migration("f2b7d4e9c1a6")).added[FILTER],
    }
    shapes = {name: _shape(column) for name, column in {**created, **added}.items()}
    # c5e1a8d3f7b2 changed only this server default, 7 -> 2.
    kind, nullable, _ = shapes["spam_ring_new_member_days"]
    shapes["spam_ring_new_member_days"] = (kind, nullable, "2")
    return shapes


def test_the_upgrade_drops_exactly_the_old_config():
    recorded = _run(DROP)
    dropped_tables = {args[0] for name, args in recorded.calls if name == "drop_table"}
    dropped_columns = [args for name, args in recorded.calls if name == "drop_column"]
    assert dropped_tables == DROPPED_TABLES
    assert dropped_columns == [(FILTER, column) for column in DROPPED_COLUMNS]
    assert [args for name, args in recorded.calls if name == "drop_index"] == [
        ("ix_attachment_filter_configs_guild_id",)
    ]
    # Nothing else is touched: no history table, no kept column.
    touched = {
        name: args
        for name, args in recorded.calls
        if name not in ("execute", "drop_table", "drop_column", "drop_index")
    }
    assert touched == {}
    assert not KEPT_COLUMNS & {column for _, column in dropped_columns}


def test_the_downgrade_restores_every_column_as_it_was_made():
    recorded = _run(DROP, "downgrade")
    restored = recorded.added[FILTER]
    assert list(restored) == DROPPED_COLUMNS
    as_made = _as_made()
    for name, column in restored.items():
        assert _shape(column) == as_made[name], name


def test_the_downgrade_restores_both_tables_as_they_were_made():
    recorded = _run(DROP, "downgrade")
    originals = {
        "moderation_configs": _run(_migration("6a448f2c9bb7")).created[
            "moderation_configs"
        ],
        "attachment_filter_configs": _run(_migration("cfcaa2cbf2b0")).created[
            "attachment_filter_configs"
        ],
    }
    assert set(recorded.created) == DROPPED_TABLES
    for table, columns in originals.items():
        assert {n: _shape(c) for n, c in recorded.created[table].items()} == {
            n: _shape(c) for n, c in columns.items()
        }, table
    assert (
        "create_index",
        (
            "ix_attachment_filter_configs_guild_id",
            "attachment_filter_configs",
            ["guild_id"],
        ),
    ) in recorded.calls


def test_lock_waits_are_bounded_both_ways():
    for step in ("upgrade", "downgrade"):
        calls = _run(DROP, step, dialect="postgresql").calls
        assert calls[0] == ("execute", ("SET LOCAL lock_timeout = '5s'",))


# ---------------------------------------------------------------------------
# On Postgres
# ---------------------------------------------------------------------------


def _alembic(database_url: str, call: str) -> None:
    env = {
        **os.environ,
        **_SETTINGS_PLACEHOLDERS,
        "DATABASE_URL": database_url,
        "ENVIRONMENT": "development",
        "REDIS_URL": "redis://127.0.0.1:1/0",
    }
    env.pop("TEST_DATABASE_URL", None)
    script = (
        "from alembic.config import Config\n"
        "from alembic import command\n"
        "cfg = Config('alembic/main/alembic.ini')\n"
        f"command.{call}\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, f"{call}:\n{done.stdout}\n{done.stderr}"


async def _schema(engine) -> dict:
    async with engine.connect() as conn:
        tables = set(
            (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'skrift'"
                    )
                )
            ).scalars()
        )
        columns = {
            row.column_name: (row.data_type, row.is_nullable, row.column_default)
            for row in await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_schema = 'skrift' "
                    "AND table_name = 'moderation_filter_configs'"
                )
            )
        }
        version = (
            await conn.execute(
                text("SELECT version_num FROM skrift.alembic_version_app")
            )
        ).scalar_one()
        indexes = set(
            (
                await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = 'skrift'")
                )
            ).scalars()
        )
    return {
        "tables": tables,
        "columns": columns,
        "version": version,
        "indexes": indexes,
    }


@needs_postgres
async def test_the_migration_round_trips_on_postgres(migrated):  # noqa: F811
    url = migrated.url.render_as_string(hide_password=False)
    head = await _schema(migrated)
    assert head["version"] == "201beb972a35"
    assert not DROPPED_TABLES & head["tables"]
    assert set(head["columns"]) == KEPT_COLUMNS
    # The history the migration must not touch.
    assert {
        "moderation_actions",
        "moderation_incidents",
        "moderation_operations",
        "moderation_outbox",
        "moderation_shadow_decisions",
        "spam_ring_incidents",
        "moderation_ownership",
        "moderation_policies",
    } <= head["tables"]
    async with migrated.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO skrift.moderation_filter_configs (guild_id, "
                "content_filter_enabled, blocked_tlds, invite_filter_enabled, "
                "invite_filter_exempt_category_ids, webhook_killer_enabled) "
                "VALUES ('g1', true, '[\".zip\"]', false, '[]', false)"
            )
        )

    await asyncio.to_thread(_alembic, url, "downgrade(cfg, '-1')")
    down = await _schema(migrated)
    assert down["version"] == "d3a8f5c2e9b1"
    assert DROPPED_TABLES <= down["tables"]
    assert "ix_attachment_filter_configs_guild_id" in down["indexes"]
    assert set(down["columns"]) == KEPT_COLUMNS | set(DROPPED_COLUMNS)
    # The kept row is still there; the restored columns hold their defaults.
    async with migrated.connect() as conn:
        row = (
            (await conn.execute(text("SELECT * FROM skrift.moderation_filter_configs")))
            .one()
            ._mapping
        )
    row = {
        key: json.loads(value) if isinstance(value, str) and value[:1] == "[" else value
        for key, value in row.items()
    }
    assert row["blocked_tlds"] == [".zip"]
    assert row["spam_ring_enabled"] is True and row["spam_engine_enabled"] is False
    assert row["spam_ring_new_member_days"] == 2
    assert row["mute_duration_seconds"] == 86400
    assert row["staff_exempt_role_ids"] == []
    assert row["scam_link_domains"][0] == "t.me"
    assert row["mod_alert_channel_id"] is None
    assert down["columns"]["mod_ping_role_id"][1] == "YES"
    assert down["columns"]["message_rate_threshold"][1] == "NO"

    await asyncio.to_thread(_alembic, url, "upgrade(cfg, 'head')")
    again = await _schema(migrated)
    assert again["version"] == "201beb972a35"
    assert not DROPPED_TABLES & again["tables"]
    assert set(again["columns"]) == KEPT_COLUMNS
    async with migrated.connect() as conn:
        assert await conn.run_sync(_column_differences) == []
