"""Tests for the ModerationFilterConfig model: one row per guild, the content
filter's own settings only (the rest moved to the moderation policy, #242)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from smarter_dev.web.models import ModerationFilterConfig

# The list the scam-link migration seeded, spelled out here so an edit to that
# historical migration fails this test.
_SEEDED_SCAM_LINK_DOMAINS = [
    "t.me",
    "telegram.me",
    "telegram.dog",
    "tlgrm.me",
    "wa.me",
    "chat.whatsapp.com",
    "signal.me",
    "signal.group",
    "join.skype.com",
    "kik.me",
    "line.me",
    "matrix.to",
    "icq.im",
]


#: The columns #242 dropped from this table; the model must not carry them.
DROPPED_COLUMNS = (
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
)


def test_the_table_holds_only_the_content_filters_columns():
    assert set(ModerationFilterConfig.__table__.columns.keys()) == {
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
    for column in DROPPED_COLUMNS:
        assert not hasattr(ModerationFilterConfig, column), column


async def test_defaults_are_every_filter_off(db_session):
    db_session.add(ModerationFilterConfig(guild_id="G1"))
    await db_session.commit()

    stored = (await db_session.execute(select(ModerationFilterConfig))).scalar_one()

    assert stored.guild_id == "G1"

    # Content filters default off with empty lists
    assert stored.content_filter_enabled is False
    assert stored.blocked_tlds == []
    assert stored.invite_filter_enabled is False
    assert stored.invite_filter_exempt_category_ids == []
    assert stored.webhook_killer_enabled is False
    assert stored.mod_log_channel_id is None

    assert stored.created_at is not None
    assert stored.updated_at is not None


def test_the_scam_link_migration_seed_is_unchanged():
    """The migration froze its own copy of the seed (the column was dropped
    in #242, and its downgrade restores this server default)."""
    migration_path = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "main"
        / "versions"
        / "20260725_120000_e7a1c4d9b3f2_scam_link_domains.py"
    )
    migration_ast = ast.parse(migration_path.read_text())
    seeded_json = next(
        ast.literal_eval(node.value)
        for node in migration_ast.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_SEEDED_SCAM_LINK_DOMAINS_JSON"
            for target in node.targets
        )
    )

    assert json.loads(seeded_json) == _SEEDED_SCAM_LINK_DOMAINS


async def test_json_list_columns_round_trip(db_session):
    db_session.add(
        ModerationFilterConfig(
            guild_id="G1",
            content_filter_enabled=True,
            blocked_tlds=[".gay", ".xxx"],
            invite_filter_enabled=True,
            invite_filter_exempt_category_ids=["555", "666"],
            webhook_killer_enabled=True,
            mod_log_channel_id="888",
        )
    )
    await db_session.commit()

    stored = (await db_session.execute(select(ModerationFilterConfig))).scalar_one()

    assert stored.content_filter_enabled is True
    assert stored.blocked_tlds == [".gay", ".xxx"]
    assert stored.invite_filter_enabled is True
    assert stored.invite_filter_exempt_category_ids == ["555", "666"]
    assert stored.webhook_killer_enabled is True
    assert stored.mod_log_channel_id == "888"


async def test_guild_id_is_unique_primary_key(db_session):
    db_session.add(ModerationFilterConfig(guild_id="G1"))
    await db_session.commit()

    db_session.add(ModerationFilterConfig(guild_id="G1"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


def test_get_defaults_returns_unsaved_config_with_every_filter_off():
    config = ModerationFilterConfig.get_defaults("G1")

    assert config.guild_id == "G1"
    assert config.content_filter_enabled is False
    assert config.invite_filter_enabled is False
    assert config.webhook_killer_enabled is False
    assert config.blocked_tlds == []


def test_get_defaults_returns_independent_list_instances():
    first = ModerationFilterConfig.get_defaults("G1")
    second = ModerationFilterConfig.get_defaults("G2")

    first.blocked_tlds.append(".xxx")

    assert second.blocked_tlds == []


def test_repr_identifies_guild_and_toggles():
    config = ModerationFilterConfig.get_defaults("G1")

    assert "G1" in repr(config)


def test_spam_ring_migration_turns_existing_rows_on():
    migration = next(
        Path(__file__).resolve().parents[3].glob(
            "alembic/main/versions/*_spam_ring_detector.py"
        )
    ).read_text()
    enabled_column = migration.split('"spam_ring_enabled"', 1)[1].split("op.add_column", 1)[0]
    assert 'server_default=sa.text("true")' in enabled_column


def test_new_member_days_default_migration_changes_only_the_server_default():
    import importlib.util
    from pathlib import Path

    path = next(Path("alembic/main/versions").glob("*_c5e1a8d3f7b2_*.py"))
    spec = importlib.util.spec_from_file_location("c5e1a8d3f7b2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls: list = []

    class _Op:
        @staticmethod
        def alter_column(table, column, **kwargs):
            calls.append((table, column, str(kwargs["server_default"])))

    module.op = _Op
    module.upgrade()
    module.downgrade()

    assert module.down_revision == "f2b7d4e9c1a6"
    assert calls == [
        ("moderation_filter_configs", "spam_ring_new_member_days", "2"),
        ("moderation_filter_configs", "spam_ring_new_member_days", "7"),
    ]
