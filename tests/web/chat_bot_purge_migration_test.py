"""The chat bot purge migration: column widths, one open request per user,
and a downgrade that refuses to un-block purged users."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text

from alembic.migration import MigrationContext
from alembic.operations import Operations

REVISION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic/main/versions/20261003_230000_f3a8b1c6d2e9_chat_bot_purge.py"
)


def _revision() -> ModuleType:
    spec = importlib.util.spec_from_file_location("chat_bot_purge_revision", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def connection(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            _revision().upgrade()
        yield conn
    engine.dispose()


def _downgrade(conn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        _revision().downgrade()


def test_columns_fit_22_digit_ids_and_the_longest_status(connection):
    columns = {
        (table, c["name"]): c["type"].length
        for table in ("chat_bot_blocked_users", "chat_bot_purge_requests")
        for c in inspect(connection).get_columns(table)
        if c["name"] in ("discord_user_id", "status")
    }
    assert columns[("chat_bot_blocked_users", "discord_user_id")] == 22
    assert columns[("chat_bot_purge_requests", "discord_user_id")] == 22
    assert columns[("chat_bot_purge_requests", "status")] == 32


def test_the_downgrade_refuses_while_anyone_is_blocked(connection):
    connection.execute(
        text(
            "INSERT INTO chat_bot_blocked_users "
            "(discord_user_id, source, created_at, updated_at) "
            "VALUES ('1111111111111111111111', 'purge', '2026-10-04', '2026-10-04')"
        )
    )
    with pytest.raises(RuntimeError, match="un-blocks"):
        _downgrade(connection)
    assert "chat_bot_blocked_users" in inspect(connection).get_table_names()


def test_the_downgrade_runs_on_an_empty_block_list(connection):
    _downgrade(connection)
    assert "chat_bot_blocked_users" not in inspect(connection).get_table_names()
