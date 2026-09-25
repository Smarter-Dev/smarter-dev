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
    settings = _settings()
    settings.summarizer_fallback_model_key = "gemini-3-6-flash"
    settings.compaction_fallback_model_key = "gemini-3-5-flash-lite"
    settings.thread_evaluator_fallback_model_key = "gemini-3-7-flash"
    settings.summarizer_model_key = "grok-4-6"
    db_session.add(settings)
    await db_session.commit()
    db_session.expunge_all()

    settings = await db_session.get(ChatSettings, 1)

    assert settings.summarizer_fallback_model_key == "gemini-3-8-flash"
    # 3.5 Flash Lite was restored on 2026-09-25: it reads as itself again.
    assert settings.compaction_fallback_model_key == "gemini-3-5-flash-lite"
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

    # Each add-only revision admits its own successors; together, less the
    # keys restored since, they cover the catalog's map exactly.
    combined: dict[str, str] = {}
    for revision in ("c8e2f4a6b1d9", "b7d3f9a2c6e4"):
        for retired, successor in _load_migration(revision)._SUCCESSORS:
            assert retired not in combined, retired
            combined[retired] = successor
    restored = _load_migration("479f5fca562d")._KEY
    assert restored in combined
    del combined[restored]
    assert combined == RETIRED_SUCCESSORS


def test_flash_lite_restore_follows_the_contract_step():
    from smarter_dev.shared.model_catalog import get_model

    module = _load_migration("479f5fca562d")
    assert module.down_revision == "7a18ff6495e1"
    assert module._KEY == "gemini-3-5-flash-lite"
    assert get_model(module._KEY) is not None


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


async def test_flash_lite_restore_puts_back_its_row_as_it_was(db_session):
    # What 7a18ff6495e1 left: no row for 3.5 Flash Lite. The restore puts its
    # pre-retirement row back and leaves an existing row (an admin's) alone.
    from sqlalchemy import text

    from smarter_dev.web.models import ChatCatalogModel

    upgrade_sql = (
        "INSERT INTO chat_catalog_models (model_key, enabled, cost_tier, sort_order)"
        " VALUES (:key, true, 'low', 8) ON CONFLICT (model_key) DO NOTHING"
    )
    module = _load_migration("479f5fca562d")
    executed: list = []

    class _Op:
        @staticmethod
        def execute(statement):
            executed.append(statement)

    module.op = _Op
    module.upgrade()
    [statement] = executed
    assert " ".join(str(statement).split()) == upgrade_sql
    assert statement.compile().params == {"key": "gemini-3-5-flash-lite"}

    for _ in range(2):  # idempotent
        await db_session.execute(text(upgrade_sql), {"key": module._KEY})
    await db_session.commit()
    row = await db_session.get(ChatCatalogModel, "gemini-3-5-flash-lite")
    assert (row.enabled, row.cost_tier, row.sort_order) == (True, "low", 8)

    row.enabled = False
    await db_session.commit()
    await db_session.execute(text(upgrade_sql), {"key": module._KEY})
    await db_session.commit()
    await db_session.refresh(row)
    assert row.enabled is False


async def test_restored_flash_lite_is_selectable_and_3_6_flash_is_not(db_session):
    # With its row back, a new web conversation can start on 3.5 Flash Lite;
    # 3.6 Flash, still retired, is refused even with a leftover enabled row.
    import pytest

    from smarter_dev.web.chat.api import HTTPException
    from smarter_dev.web.chat.api import resolved_conversation_settings
    from smarter_dev.web.chat.settings import ensure_settings
    from smarter_dev.web.models import ChatCatalogModel

    await ensure_settings(db_session)
    lite = await db_session.get(ChatCatalogModel, "gemini-3-5-flash-lite")
    lite.enabled = True
    db_session.add(
        ChatCatalogModel(model_key="gemini-3-6-flash", enabled=True, cost_tier="low", sort_order=9)
    )
    await db_session.commit()

    _, key, _ = await resolved_conversation_settings(
        db_session, permissions=frozenset(), model_key="gemini-3-5-flash-lite"
    )
    assert key == "gemini-3-5-flash-lite"
    with pytest.raises(HTTPException):
        await resolved_conversation_settings(
            db_session, permissions=frozenset(), model_key="gemini-3-6-flash"
        )
