"""Guard tests for the single-database runtime and the legacy decommission.

After the phase-02 DB consolidation every runtime session targets the primary
``DATABASE_URL`` with the ``skrift`` schema translate map, and after the
phase-05 decommission (docs/v2/legacy-sunset/05-decommission.md) the legacy
plumbing — ``LEGACY_DATABASE_URL``, ``use_legacy_db``, ``alembic/legacy``,
``smarter_dev/web/api``, ``smarter_dev/web/admin``, ``templates/bot-admin`` —
is gone entirely. These tripwires keep it from silently coming back.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from smarter_dev.shared import database
from smarter_dev.shared.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PACKAGE_ROOT = REPO_ROOT / "smarter_dev"

FORBIDDEN_RUNTIME_TOKENS = (
    "use_legacy_db",
    "effective_legacy_database_url",
    "LEGACY_DATABASE_URL",
)

# Every python/yaml file under these roots must be free of legacy-DB tokens.
TRIPWIRE_SCAN_ROOTS = (
    RUNTIME_PACKAGE_ROOT,
    REPO_ROOT / "scripts",
    REPO_ROOT / "k8s",
)

TRIPWIRE_SCAN_FILES = (
    REPO_ROOT / "compose.yaml",
    REPO_ROOT / "app.yaml",
    REPO_ROOT / "app.development.yaml",
    REPO_ROOT / ".env.example",
)

DELETED_LEGACY_PATHS = (
    "alembic/legacy",
    "smarter_dev/web/api",
    "smarter_dev/web/admin",
    "templates/bot-admin",
)


def _iter_tripwire_files():
    for root in TRIPWIRE_SCAN_ROOTS:
        for pattern in ("*.py", "*.yaml", "*.yml", "*.sql"):
            yield from sorted(root.rglob(pattern))
    for file_path in TRIPWIRE_SCAN_FILES:
        if file_path.exists():
            yield file_path


def test_no_legacy_database_tokens_anywhere() -> None:
    """smarter_dev/, scripts/, k8s/, and the top-level yamls are legacy-free."""
    offenders: list[str] = []
    for file_path in _iter_tripwire_files():
        source = file_path.read_text()
        for token in FORBIDDEN_RUNTIME_TOKENS:
            if token in source:
                offenders.append(f"{file_path.relative_to(REPO_ROOT)}: {token}")
    assert offenders == [], (
        "legacy database plumbing must stay deleted: " + "; ".join(offenders)
    )


def test_deleted_legacy_trees_stay_deleted() -> None:
    """The removed legacy packages/trees contain no python or template files."""
    resurrected: list[str] = []
    for relative_path in DELETED_LEGACY_PATHS:
        absolute_path = REPO_ROOT / relative_path
        if not absolute_path.exists():
            continue
        real_files = [
            path
            for path in absolute_path.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ]
        if real_files:
            resurrected.append(relative_path)
    assert resurrected == [], (
        f"decommissioned legacy trees reappeared: {resurrected}"
    )


def test_settings_has_no_legacy_database_fields() -> None:
    """The legacy URL field and its property are gone from Settings."""
    assert "legacy_database_url" not in Settings.model_fields
    assert not hasattr(Settings, "effective_legacy_database_url")


def test_database_module_has_no_skrift_session_aliases() -> None:
    """The transitional get_skrift_db_session* aliases were removed."""
    assert not hasattr(database, "get_skrift_db_session")
    assert not hasattr(database, "get_skrift_db_session_context")


def test_create_engine_has_no_legacy_flag() -> None:
    """create_engine(settings) takes no use_legacy_db switch anymore."""
    parameters = inspect.signature(database.create_engine).parameters
    assert list(parameters) == ["settings"]


def test_create_engine_targets_primary_database_url(tmp_path) -> None:
    """The engine always connects to DATABASE_URL."""
    settings = Settings(
        environment="development",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/primary.db",
        discord_bot_token="guard-test-token",
        discord_application_id="1",
    )
    engine = database.create_engine(settings)
    assert "primary.db" in str(engine.url)


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
