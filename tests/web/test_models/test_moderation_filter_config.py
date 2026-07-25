"""Tests for the ModerationFilterConfig model — one row per guild, legacy defaults."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from smarter_dev.web.models import DEFAULT_SCAM_LINK_DOMAINS
from smarter_dev.web.models import ModerationFilterConfig

# The shipped default list, spelled out here so a silent edit to the model
# constant fails this test rather than quietly changing every guild's defaults.
_EXPECTED_DEFAULT_SCAM_LINK_DOMAINS = [
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


async def test_defaults_match_legacy_thresholds(db_session):
    db_session.add(ModerationFilterConfig(guild_id="G1"))
    await db_session.commit()

    stored = (await db_session.execute(select(ModerationFilterConfig))).scalar_one()

    assert stored.guild_id == "G1"

    # Content filters default off with empty lists
    assert stored.content_filter_enabled is False
    assert stored.blocked_tlds == []
    assert stored.invite_filter_enabled is False
    assert stored.invite_filter_exempt_category_ids == []
    assert stored.staff_exempt_role_ids == []
    assert stored.webhook_killer_enabled is False
    assert stored.mod_log_channel_id is None

    # Spam engine defaults off but with the legacy thresholds pre-populated
    assert stored.spam_engine_enabled is False
    assert stored.message_rate_threshold == 5
    assert stored.message_rate_window_seconds == 5
    assert stored.channel_spread_threshold == 3
    assert stored.channel_spread_window_seconds == 15
    assert stored.duplicate_message_min_length == 15
    assert stored.duplicate_message_window_seconds == 60
    assert stored.mass_mention_window_seconds == 15
    assert stored.warning_reoffense_window_seconds == 120
    assert stored.mute_duration_seconds == 86400
    assert stored.mod_alert_channel_id is None
    assert stored.mod_ping_role_id is None
    assert stored.scam_log_channel_id is None

    assert stored.created_at is not None
    assert stored.updated_at is not None


async def test_scam_link_domains_default_to_messaging_platforms(db_session):
    db_session.add(ModerationFilterConfig(guild_id="G1"))
    await db_session.commit()

    stored = (await db_session.execute(select(ModerationFilterConfig))).scalar_one()

    assert stored.scam_link_domains == _EXPECTED_DEFAULT_SCAM_LINK_DOMAINS
    assert list(DEFAULT_SCAM_LINK_DOMAINS) == _EXPECTED_DEFAULT_SCAM_LINK_DOMAINS


async def test_scam_link_domains_round_trip(db_session):
    db_session.add(
        ModerationFilterConfig(guild_id="G1", scam_link_domains=["t.me", "evil.example"])
    )
    await db_session.commit()

    stored = (await db_session.execute(select(ModerationFilterConfig))).scalar_one()

    assert stored.scam_link_domains == ["t.me", "evil.example"]


def test_get_defaults_returns_independent_scam_link_domain_lists():
    first = ModerationFilterConfig.get_defaults("G1")
    second = ModerationFilterConfig.get_defaults("G2")

    first.scam_link_domains.append("evil.example")

    assert second.scam_link_domains == _EXPECTED_DEFAULT_SCAM_LINK_DOMAINS
    assert list(DEFAULT_SCAM_LINK_DOMAINS) == _EXPECTED_DEFAULT_SCAM_LINK_DOMAINS


def test_migration_seed_matches_the_model_default():
    """The migration freezes its own copy of the seed; it must not drift.

    Existing rows are backfilled by the migration's server default while new
    rows get the model default, so a mismatch would silently split guilds into
    two populations with different protection.
    """
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

    assert json.loads(seeded_json) == list(DEFAULT_SCAM_LINK_DOMAINS)


async def test_json_list_columns_round_trip(db_session):
    db_session.add(
        ModerationFilterConfig(
            guild_id="G1",
            content_filter_enabled=True,
            blocked_tlds=[".gay", ".xxx"],
            invite_filter_enabled=True,
            invite_filter_exempt_category_ids=["555", "666"],
            staff_exempt_role_ids=["777"],
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
    assert stored.staff_exempt_role_ids == ["777"]
    assert stored.webhook_killer_enabled is True
    assert stored.mod_log_channel_id == "888"


async def test_spam_engine_settings_round_trip(db_session):
    db_session.add(
        ModerationFilterConfig(
            guild_id="G1",
            spam_engine_enabled=True,
            message_rate_threshold=8,
            message_rate_window_seconds=10,
            channel_spread_threshold=4,
            channel_spread_window_seconds=20,
            duplicate_message_min_length=25,
            duplicate_message_window_seconds=90,
            mass_mention_window_seconds=30,
            warning_reoffense_window_seconds=300,
            mute_duration_seconds=3600,
            mod_alert_channel_id="111",
            mod_ping_role_id="222",
            scam_log_channel_id="333",
        )
    )
    await db_session.commit()

    stored = (await db_session.execute(select(ModerationFilterConfig))).scalar_one()

    assert stored.spam_engine_enabled is True
    assert stored.message_rate_threshold == 8
    assert stored.message_rate_window_seconds == 10
    assert stored.channel_spread_threshold == 4
    assert stored.channel_spread_window_seconds == 20
    assert stored.duplicate_message_min_length == 25
    assert stored.duplicate_message_window_seconds == 90
    assert stored.mass_mention_window_seconds == 30
    assert stored.warning_reoffense_window_seconds == 300
    assert stored.mute_duration_seconds == 3600
    assert stored.mod_alert_channel_id == "111"
    assert stored.mod_ping_role_id == "222"
    assert stored.scam_log_channel_id == "333"


async def test_guild_id_is_unique_primary_key(db_session):
    db_session.add(ModerationFilterConfig(guild_id="G1"))
    await db_session.commit()

    db_session.add(ModerationFilterConfig(guild_id="G1"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


def test_get_defaults_returns_unsaved_config_with_legacy_values():
    config = ModerationFilterConfig.get_defaults("G1")

    assert config.guild_id == "G1"
    assert config.content_filter_enabled is False
    assert config.spam_engine_enabled is False
    assert config.blocked_tlds == []
    assert config.staff_exempt_role_ids == []
    assert config.message_rate_threshold == 5
    assert config.mute_duration_seconds == 86400


def test_get_defaults_returns_independent_list_instances():
    first = ModerationFilterConfig.get_defaults("G1")
    second = ModerationFilterConfig.get_defaults("G2")

    first.blocked_tlds.append(".xxx")

    assert second.blocked_tlds == []


def test_repr_identifies_guild_and_toggles():
    config = ModerationFilterConfig.get_defaults("G1")

    assert "G1" in repr(config)
