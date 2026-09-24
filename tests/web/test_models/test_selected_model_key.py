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


async def test_retired_gemini_flash_and_grok_selections_read_as_successors(db_session):
    # Production on 2026-09-24: both fallbacks name 3.5 Flash Lite.
    settings = _settings()
    settings.summarizer_fallback_model_key = "gemini-3-5-flash-lite"
    settings.compaction_fallback_model_key = "gemini-3-5-flash-lite"
    settings.thread_evaluator_fallback_model_key = "gemini-3-7-flash"
    settings.summarizer_model_key = "grok-4-6"
    db_session.add(settings)
    await db_session.commit()
    db_session.expunge_all()

    settings = await db_session.get(ChatSettings, 1)

    assert settings.summarizer_fallback_model_key == "gemini-3-8-flash"
    assert settings.compaction_fallback_model_key == "gemini-3-8-flash"
    assert settings.thread_evaluator_fallback_model_key == "gemini-3-8-flash"
    assert settings.summarizer_model_key == "grok-4-7"


async def test_channel_pins_keep_retired_gemini_and_grok_keys(db_session):
    db_session.add(
        ChannelModelOverride(
            guild_id="G1",
            channel_id="C1",
            model_key="grok-4-6",
            fallback_model_key="gemini-3-6-flash",
        )
    )
    await db_session.commit()
    db_session.expunge_all()

    pin = (await db_session.execute(select(ChannelModelOverride))).scalar_one()

    assert pin.model_key == "grok-4-6"
    assert pin.fallback_model_key == "gemini-3-6-flash"


def _load_migration(revision: str):
    import importlib.util
    from pathlib import Path

    path = next(Path("alembic/main/versions").glob(f"*_{revision}_*.py"))
    spec = importlib.util.spec_from_file_location(revision, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_successors_match_the_catalog_map():
    from smarter_dev.shared.model_catalog import RETIRED_SUCCESSORS

    # Each add-only revision admits its own successors; together they cover
    # the catalog's map exactly.
    combined: dict[str, str] = {}
    for revision in ("c8e2f4a6b1d9", "b7d3f9a2c6e4"):
        for retired, successor in _load_migration(revision)._SUCCESSORS:
            assert retired not in combined, retired
            combined[retired] = successor
    assert combined == RETIRED_SUCCESSORS


def test_gemini_flash_and_grok_migration_copies_the_slot_holders():
    module = _load_migration("b7d3f9a2c6e4")
    assert module.down_revision == "e5a9c3f7d2b8"
    # 3.8 Flash takes 3.7 Flash's slot; Grok 4.7 takes 4.6's.
    assert module._predecessors() == {
        "gemini-3-8-flash": "gemini-3-7-flash",
        "grok-4-7": "grok-4-6",
    }
    # The previous build already knows 3.8 Flash, so only Grok 4.7 is unwound.
    assert module._NEW_KEYS == frozenset({"grok-4-7"})


def test_gemini_flash_and_grok_contract_step_follows_its_add_step():
    add = _load_migration("b7d3f9a2c6e4")
    contract = _load_migration("7a18ff6495e1")
    assert contract.down_revision == "b7d3f9a2c6e4"
    # It rewrites exactly the pairs the add step admitted, and the same live
    # selection columns the GPT contract step did; never a channel pin.
    assert contract._SUCCESSORS == add._SUCCESSORS
    assert contract._SELECTION_COLUMNS == _load_migration("e5a9c3f7d2b8")._SELECTION_COLUMNS
    assert all(table != "channel_model_overrides" for table, _ in contract._SELECTION_COLUMNS)
