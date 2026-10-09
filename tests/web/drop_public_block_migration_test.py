"""The guild memory's public block is dropped and the switch stays (#104)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text

from alembic.migration import MigrationContext
from alembic.operations import Operations

REPO = Path(__file__).resolve().parents[2]
_GUILD = "123456789012345678"
_BLOB = "## People\n- someone loves shaders"


def _revision(revision_id: str):
    path = next((REPO / "alembic" / "main" / "versions").glob(f"*_{revision_id}_*.py"))
    spec = importlib.util.spec_from_file_location(f"revision_{revision_id}", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    return revision


def test_the_public_block_comes_and_goes_and_the_switch_stays(tmp_path):
    added = _revision("b4e8a1d6c2f9")
    dropped = _revision("c7d2e9f4a1b6")
    assert dropped.down_revision == "b4e8a1d6c2f9"

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE chat_agent_guild_memory (id CHAR(32) PRIMARY KEY, "
                "guild_id VARCHAR(20) NOT NULL, content VARCHAR(2000) NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO chat_agent_guild_memory VALUES ('a', :g, :c)"),
            {"g": _GUILD, "c": _BLOB},
        )
        operations = Operations(MigrationContext.configure(connection))
        added.op = dropped.op = operations
        added.upgrade()

        def columns() -> set[str]:
            return {
                c["name"]
                for c in inspect(connection).get_columns("chat_agent_guild_memory")
            }

        assert {"public_content", "public_page_enabled"} <= columns()

        dropped.upgrade()
        assert "public_content" not in columns()
        row = connection.execute(
            text("SELECT content, public_page_enabled FROM chat_agent_guild_memory")
        ).one()
        assert row == (_BLOB, 0)

        dropped.downgrade()
        assert (
            connection.execute(
                text("SELECT public_content FROM chat_agent_guild_memory")
            ).scalar_one()
            == ""
        )
    engine.dispose()
