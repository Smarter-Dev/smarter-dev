"""Tests for the ChannelModelOverride model — one row per channel, budget defaults."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from smarter_dev.web.models import ChannelModelOverride


def _override(
    channel_id: str = "C1",
    guild_id: str = "G1",
    model_key: str = "kimi-k2",
    **kwargs,
) -> ChannelModelOverride:
    return ChannelModelOverride(
        guild_id=guild_id,
        channel_id=channel_id,
        model_key=model_key,
        **kwargs,
    )


async def test_create_and_read_override(db_session):
    db_session.add(_override(daily_token_budget=1000, hourly_token_budget=100))
    await db_session.commit()

    stored = (
        await db_session.execute(
            select(ChannelModelOverride).where(
                ChannelModelOverride.channel_id == "C1"
            )
        )
    ).scalar_one()

    assert stored.guild_id == "G1"
    assert stored.model_key == "kimi-k2"
    assert stored.daily_token_budget == 1000
    assert stored.hourly_token_budget == 100
    assert stored.created_at is not None
    assert stored.updated_at is not None


async def test_budgets_default_to_zero(db_session):
    db_session.add(_override())
    await db_session.commit()

    stored = (
        await db_session.execute(select(ChannelModelOverride))
    ).scalar_one()
    assert stored.daily_token_budget == 0  # 0 == unlimited
    assert stored.hourly_token_budget == 0


async def test_new_settings_default_off(db_session):
    db_session.add(_override())
    await db_session.commit()

    stored = (
        await db_session.execute(select(ChannelModelOverride))
    ).scalar_one()
    assert stored.auto_respond is False
    assert stored.fallback_model_key is None
    assert stored.response_filter is None


async def test_new_settings_round_trip(db_session):
    db_session.add(
        _override(
            auto_respond=True,
            fallback_model_key="glm-4-6",
            response_filter="Only answer coding questions.",
        )
    )
    await db_session.commit()

    stored = (
        await db_session.execute(select(ChannelModelOverride))
    ).scalar_one()
    assert stored.auto_respond is True
    assert stored.fallback_model_key == "glm-4-6"
    assert stored.response_filter == "Only answer coding questions."


async def test_channel_id_is_unique(db_session):
    db_session.add(_override(channel_id="C1", model_key="kimi-k2"))
    await db_session.commit()

    db_session.add(_override(channel_id="C1", model_key="glm-4-6"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_different_channels_coexist(db_session):
    db_session.add(_override(channel_id="C1"))
    db_session.add(_override(channel_id="C2"))
    await db_session.commit()  # uniqueness is per channel, not per guild
