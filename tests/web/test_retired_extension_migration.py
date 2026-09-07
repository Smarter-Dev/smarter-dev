"""The data migration that removes already-installed prefix-command handlers.

Deleting an extension from the catalog stops shipping it; it does not stop the
copies already materialised into guilds. This revision deletes those rows, so
the tests drive the real ``upgrade()`` against a seeded database and assert on
what is left: the retired rows gone, every neighbouring row untouched.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select

from alembic.migration import MigrationContext
from alembic.operations import Operations
from smarter_dev.shared.database import Base
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ExtensionInstall

REPO_ROOT = Path(__file__).resolve().parents[2]
REVISION_PATH = (
    REPO_ROOT
    / "alembic"
    / "main"
    / "versions"
    / "20260906_120000_a4c7e2f9b6d3_remove_installed_prefix_command_handlers.py"
)

_GUILD = "111111111111111111"
_OTHER_GUILD = "222222222222222222"


def _revision_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "retired_extension_revision", REVISION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated_connection(tmp_path):
    """A sqlite database with the app schema, ready for the revision to run."""
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        yield connection
    engine.dispose()


def _install(connection, *, guild_id: str, slug: str, version: int = 2) -> UUID:
    install_id = uuid4()
    connection.execute(
        ExtensionInstall.__table__.insert().values(
            id=install_id,
            guild_id=guild_id,
            extension_slug=slug,
            installed_version=version,
            config={},
            enabled=True,
            installed_by="admin@example.com",
        )
    )
    return install_id


def _handler(
    connection,
    *,
    guild_id: str,
    name: str,
    install_id: UUID | None,
    handler_key: str | None,
) -> UUID:
    handler_id = uuid4()
    connection.execute(
        AdminHandler.__table__.insert().values(
            id=handler_id,
            guild_id=guild_id,
            name=name,
            trigger_type="message",
            settings={},
            channel_ids=[],
            description=name,
            script="await send_message('hi')\n",
            created_by_admin="admin@example.com",
            enabled=True,
            memory={},
            extension_install_id=install_id,
            extension_handler_key=handler_key,
        )
    )
    return handler_id


def _upgrade(connection) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        _revision_module().upgrade()


def _handler_names(connection) -> list[str]:
    return sorted(connection.execute(select(AdminHandler.__table__.c.name)).scalars())


def _install_slugs(connection) -> list[str]:
    return sorted(
        connection.execute(
            select(ExtensionInstall.__table__.c.extension_slug)
        ).scalars()
    )


def test_upgrade_deletes_the_bump_commands_handler_and_keeps_the_tracker(
    migrated_connection,
):
    install_id = _install(
        migrated_connection, guild_id=_GUILD, slug="disboard-bumping"
    )
    _handler(
        migrated_connection,
        guild_id=_GUILD,
        name="bump-commands",
        install_id=install_id,
        handler_key="bump-commands",
    )
    _handler(
        migrated_connection,
        guild_id=_GUILD,
        name="bump-tracker",
        install_id=install_id,
        handler_key="bump-tracker",
    )

    _upgrade(migrated_connection)

    assert _handler_names(migrated_connection) == ["bump-tracker"]
    assert _install_slugs(migrated_connection) == ["disboard-bumping"]


def test_upgrade_deletes_the_sus_install_and_its_handler(migrated_connection):
    install_id = _install(migrated_connection, guild_id=_GUILD, slug="sus", version=1)
    _handler(
        migrated_connection,
        guild_id=_GUILD,
        name="sus",
        install_id=install_id,
        handler_key="sus",
    )

    _upgrade(migrated_connection)

    assert _handler_names(migrated_connection) == []
    assert _install_slugs(migrated_connection) == []


def test_upgrade_cleans_every_guild_that_installed_them(migrated_connection):
    for guild_id in (_GUILD, _OTHER_GUILD):
        install_id = _install(migrated_connection, guild_id=guild_id, slug="sus")
        _handler(
            migrated_connection,
            guild_id=guild_id,
            name="sus",
            install_id=install_id,
            handler_key="sus",
        )

    _upgrade(migrated_connection)

    assert _handler_names(migrated_connection) == []
    assert _install_slugs(migrated_connection) == []


def test_upgrade_leaves_hand_authored_handlers_alone(migrated_connection):
    """A hand-authored row has no install to belong to and is not ours to delete."""
    _handler(
        migrated_connection,
        guild_id=_GUILD,
        name="bump-commands",
        install_id=None,
        handler_key="bump-commands",
    )

    _upgrade(migrated_connection)

    assert _handler_names(migrated_connection) == ["bump-commands"]


def test_upgrade_leaves_other_extensions_alone(migrated_connection):
    install_id = _install(
        migrated_connection, guild_id=_GUILD, slug="keyword-watch", version=1
    )
    _handler(
        migrated_connection,
        guild_id=_GUILD,
        name="keyword-watch",
        install_id=install_id,
        handler_key="keyword-watch",
    )

    _upgrade(migrated_connection)

    assert _handler_names(migrated_connection) == ["keyword-watch"]
    assert _install_slugs(migrated_connection) == ["keyword-watch"]


def test_upgrade_is_a_no_op_on_a_guild_that_never_installed_them(migrated_connection):
    _upgrade(migrated_connection)

    assert _handler_names(migrated_connection) == []
    assert _install_slugs(migrated_connection) == []


def test_upgrade_runs_twice_without_error(migrated_connection):
    """Re-running the sweep must not fail; alembic can replay on a restored dump."""
    install_id = _install(migrated_connection, guild_id=_GUILD, slug="sus", version=1)
    _handler(
        migrated_connection,
        guild_id=_GUILD,
        name="sus",
        install_id=install_id,
        handler_key="sus",
    )

    _upgrade(migrated_connection)
    _upgrade(migrated_connection)

    assert _install_slugs(migrated_connection) == []


def test_downgrade_does_not_restore_deleted_rows(migrated_connection):
    """The scripts left the repo, so nothing can re-render them; state that here."""
    with Operations.context(MigrationContext.configure(migrated_connection)):
        _revision_module().downgrade()

    assert _handler_names(migrated_connection) == []
    assert _install_slugs(migrated_connection) == []
