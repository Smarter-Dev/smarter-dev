"""A selected model key reads back as its successor once the model retires."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy import text

from smarter_dev.web.models import ChannelModelOverride
from smarter_dev.web.models import ChatSettings


def _settings() -> ChatSettings:
    return ChatSettings(
        id=1,
        default_model_key="gpt-5-6-luna",
        default_reasoning="medium",
        default_intelligence_mode="efficient",
        summarizer_model_key="gpt-5-6-luna",
        summarizer_fallback_model_key="gpt-5-6-terra",
        compaction_model_key="gpt-5-6-luna",
        compaction_fallback_model_key=None,
        thread_evaluator_model_key="deepseek-v4",
        thread_evaluator_fallback_model_key="gpt-5-6-terra",
        thread_idle_minutes=30,
    )


async def test_settings_read_retired_keys_as_successors(db_session):
    db_session.add(_settings())
    await db_session.commit()
    db_session.expunge_all()

    settings = await db_session.get(ChatSettings, 1)

    assert settings.default_model_key == "gpt-6-luna"
    assert settings.summarizer_model_key == "gpt-6-luna"
    assert settings.summarizer_fallback_model_key == "gpt-6-sol"
    assert settings.compaction_model_key == "gpt-6-luna"
    assert settings.compaction_fallback_model_key is None
    assert settings.thread_evaluator_model_key == "deepseek-v4"
    assert settings.thread_evaluator_fallback_model_key == "gpt-6-sol"


async def test_reading_and_saving_other_columns_leaves_stored_keys_alone(db_session):
    # Pods on the previous build read the same row during a rollout, and they
    # know no GPT-6 key: a read, or an unrelated save, must not rewrite it.
    db_session.add(_settings())
    await db_session.commit()
    db_session.expunge_all()

    settings = await db_session.get(ChatSettings, 1)
    settings.thread_idle_minutes = 45
    await db_session.commit()

    stored = (
        await db_session.execute(
            text("SELECT default_model_key, summarizer_fallback_model_key"
                 " FROM chat_settings WHERE id = 1")
        )
    ).one()
    assert tuple(stored) == ("gpt-5-6-luna", "gpt-5-6-terra")


async def test_channel_pins_keep_the_retired_key(db_session):
    # Pins are never moved: the channel stops with a notice until an admin
    # repins it (Zech, 2026-09-24).
    db_session.add(
        ChannelModelOverride(
            guild_id="G1",
            channel_id="C1",
            model_key="gpt-5-6-terra",
            fallback_model_key="gpt-5-6-luna",
        )
    )
    await db_session.commit()
    db_session.expunge_all()

    pin = (await db_session.execute(select(ChannelModelOverride))).scalar_one()

    assert pin.model_key == "gpt-5-6-terra"
    assert pin.fallback_model_key == "gpt-5-6-luna"
