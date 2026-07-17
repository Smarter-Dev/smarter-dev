"""Guard tests for the phase-02 single-database runtime.

After the DB consolidation (docs/v2/legacy-sunset/02-db-consolidation.md)
every runtime session targets the primary ``DATABASE_URL`` with the
``skrift`` schema translate map. The legacy database URL exists only for
``scripts/copy_legacy_data.py`` and the closed ``alembic/legacy`` tree.
These tests keep that invariant from silently regressing.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from smarter_dev.shared import database
from smarter_dev.shared.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PACKAGE_ROOT = REPO_ROOT / "smarter_dev"

# The config module still *defines* the legacy URL settings (consumed by
# scripts/ and alembic/legacy/ until the decommission phase); nothing else
# in the runtime package may reference them.
ALLOWED_LEGACY_URL_MODULES = frozenset({
    RUNTIME_PACKAGE_ROOT / "shared" / "config.py",
})

FORBIDDEN_RUNTIME_TOKENS = ("use_legacy_db", "effective_legacy_database_url")


def test_runtime_package_has_no_legacy_database_references() -> None:
    """No smarter_dev module (except config's definition) touches legacy-DB knobs."""
    offenders: list[str] = []
    for module_path in sorted(RUNTIME_PACKAGE_ROOT.rglob("*.py")):
        if module_path in ALLOWED_LEGACY_URL_MODULES:
            continue
        source = module_path.read_text()
        for token in FORBIDDEN_RUNTIME_TOKENS:
            if token in source:
                offenders.append(f"{module_path.relative_to(REPO_ROOT)}: {token}")
    assert offenders == [], (
        "runtime code must not reference the legacy database: "
        + "; ".join(offenders)
    )


def test_create_engine_has_no_legacy_flag() -> None:
    """create_engine(settings) takes no use_legacy_db switch anymore."""
    parameters = inspect.signature(database.create_engine).parameters
    assert list(parameters) == ["settings"]


def test_create_engine_targets_primary_database_url(tmp_path) -> None:
    """The engine always connects to DATABASE_URL, never LEGACY_DATABASE_URL."""
    settings = Settings(
        environment="development",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/primary.db",
        legacy_database_url=f"sqlite+aiosqlite:///{tmp_path}/legacy.db",
        discord_bot_token="guard-test-token",
        discord_application_id="1",
    )
    engine = database.create_engine(settings)
    assert "primary.db" in str(engine.url)
    assert "legacy.db" not in str(engine.url)


def test_skrift_schema_engine_applies_translate_map(tmp_path) -> None:
    """The shared engine factory maps schema-less tables into ``skrift``."""
    settings = Settings(
        environment="development",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/primary.db",
        discord_bot_token="guard-test-token",
        discord_application_id="1",
    )
    engine = database.create_skrift_schema_engine(settings)
    execution_options = engine.sync_engine.get_execution_options()
    assert execution_options["schema_translate_map"] == {None: "skrift"}


def test_skrift_session_accessors_are_aliases() -> None:
    """The *_skrift_* accessors are thin aliases of the primary accessors."""
    skrift_context_source = inspect.getsource(database.get_skrift_db_session_context)
    assert "get_db_session_context()" in skrift_context_source
    skrift_session_source = inspect.getsource(database.get_skrift_db_session)
    assert "get_db_session()" in skrift_session_source
