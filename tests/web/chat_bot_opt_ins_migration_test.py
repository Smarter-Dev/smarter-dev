"""The opt-in times migration (#92): the table, and a downgrade that refuses
to expose what people wrote before opting back in."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text

from alembic.migration import MigrationContext
from alembic.operations import Operations

REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic/main/versions/20261006_210000_9d2e6f4a8b17_chat_bot_opt_ins.py"
)


def _run(conn, step: str) -> None:
    spec = importlib.util.spec_from_file_location("chat_bot_opt_ins_revision", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(conn)):
        getattr(module, step)()


@pytest.fixture
def connection(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.connect() as conn:
        _run(conn, "upgrade")
        yield conn
    engine.dispose()


def test_the_table_holds_only_the_id_and_times(connection):
    columns = {c["name"]: c for c in inspect(connection).get_columns("chat_bot_opt_ins")}
    assert set(columns) == {"discord_user_id", "read_from", "created_at", "updated_at"}
    assert columns["discord_user_id"]["type"].length == 22


def test_the_downgrade_refuses_while_anyone_has_an_opt_in_time(connection):
    connection.execute(
        text(
            "INSERT INTO chat_bot_opt_ins "
            "(discord_user_id, read_from, created_at, updated_at) "
            "VALUES ('111111111111111111', '2026-10-06', '2026-10-06', '2026-10-06')"
        )
    )
    with pytest.raises(RuntimeError, match="before opting back in"):
        _run(connection, "downgrade")
    assert "chat_bot_opt_ins" in inspect(connection).get_table_names()


def test_the_downgrade_runs_on_an_empty_table(connection):
    _run(connection, "downgrade")
    assert "chat_bot_opt_ins" not in inspect(connection).get_table_names()
