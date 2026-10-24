"""Delete all's id-ordered pages after a cursor are served by an (owner, id)
index (#326): the model has it, and its migration builds it and takes it
away again. On Postgres the build is CONCURRENTLY, outside a transaction."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from smarter_dev.web.models import WebSearchRun

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic/main/versions/20261025_000000_d4a8e2c6f1b3_web_search_runs_owner_id_index.py"
)
NAME = "ix_web_search_runs_owner_id"


def _migration():
    spec = importlib.util.spec_from_file_location("owner_id_index", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_model_indexes_a_members_runs_by_id():
    index = next(i for i in WebSearchRun.__table__.indexes if i.name == NAME)
    assert [column.name for column in index.columns] == ["owner_user_id", "id"]


def test_the_migration_builds_the_index_and_its_downgrade_drops_it():
    migration = _migration()
    assert migration.down_revision == "b7e3c1d9a5f2"
    assert "CONCURRENTLY" in migration.CREATE and "CONCURRENTLY" in migration.DROP
    engine = sa.create_engine("sqlite://")
    table = WebSearchRun.__table__
    with engine.begin() as connection:
        # The table as the revision before this one left it.
        table.create(connection)
        connection.execute(sa.text(f"DROP INDEX {NAME}"))

    def indexes():
        with engine.connect() as connection:
            return {i["name"] for i in sa.inspect(connection).get_indexes(table.name)}

    def run(step):
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                step()

    assert NAME not in indexes()
    run(migration.upgrade)
    assert NAME in indexes()
    run(migration.upgrade)  # IF NOT EXISTS: a rerun is harmless
    run(migration.downgrade)
    assert NAME not in indexes()
    assert "ix_web_search_runs_owner_created" in indexes()
