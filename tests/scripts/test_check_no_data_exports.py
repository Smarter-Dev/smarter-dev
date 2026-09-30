"""Tests for scripts/check_no_data_exports.py.

Every fixture is synthetic and built in a throwaway git repository: an empty
SQLite database, a few made-up dump lines. No real export is read or copied.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from scripts import check_no_data_exports as guard


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Isolate from the machine's git config (signing, hooks, templates).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(tmp_path)
    _git("init", "-q", "-b", "main")
    _commit({"README.md": b"hello\n"}, "root")
    return tmp_path


def _git(*args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c", "user.name=t",
            "-c", "user.email=t@example.invalid",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(files: dict[str, bytes], message: str, remove: tuple[str, ...] = ()) -> str:
    for name, data in files.items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        _git("add", "-f", name)
    for name in remove:
        _git("rm", "-q", name)
    _git("commit", "-q", "-m", message)
    return _git("rev-parse", "HEAD")


def _empty_sqlite(tmp: Path) -> bytes:
    db = tmp / "scratch.sqlite-src"
    sqlite3.connect(db).execute("create table t (x integer)").connection.commit()
    data = db.read_bytes()
    db.unlink()
    return data


@pytest.mark.parametrize(
    "path",
    [
        "database_backup/data_backup.json",
        "nested/database_backup/alembic_version.txt",
        "smarter_dev.db",
        "tests/app.sqlite3",
        "var/app.db-wal",
        "prod.pgdump",
        "nightly.dump",
        "exports/users_backup.json",
        "db_dump_2025.sql",
        "Backup.CSV",
        "dump.sql.gz",
    ],
)
def test_blocked_paths(path: str) -> None:
    assert guard.path_rule(path) is not None


@pytest.mark.parametrize(
    "path",
    [
        "alembic/versions/0001_initial.py",
        "scripts/postgres-init/01-create-databases.sql",
        "static/mascot-studio/recovered-exports/smarter-happy-square.png",
        "services/media/test/fixtures/cards/balance.json",
        "smarter_dev/web/api/backup_routes.py",
        "tests/bot/services/test_performance.py.bak",
        "docs/dump-format.md",
    ],
)
def test_allowed_paths(path: str) -> None:
    assert guard.path_rule(path) is None


def test_content_rules() -> None:
    assert guard.content_rule(b"SQLite format 3\x00" + b"\0" * 84)
    assert guard.content_rule(b"PGDMP\x01\x0e")
    assert guard.content_rule(b"--\n-- PostgreSQL database dump\n--\n")
    assert guard.content_rule(b"SET x = 1;\nCOPY public.t (a, b) FROM stdin;\n")
    assert guard.content_rule(b"CREATE TABLE t (a int);\n") is None
    assert guard.content_rule(b'{"api_keys": []}') is None


def test_clean_tree_passes(repo: Path) -> None:
    _commit({"schema.sql": b"CREATE TABLE t (a int);\n"}, "schema")
    assert guard.check_tree() == []
    assert guard.main([]) == 0


def test_renamed_sqlite_file_is_caught_by_content(repo: Path, tmp_path: Path) -> None:
    _commit({"assets/cache.bin": _empty_sqlite(tmp_path)}, "sneaky")
    assert guard.check_tree() == [("assets/cache.bin", "SQLite database content")]
    assert guard.main([]) == 1


def test_dump_added_then_removed_in_range_is_caught(repo: Path) -> None:
    base = _git("rev-parse", "HEAD")
    added = _commit({"database_backup/data_backup.json": b"{}"}, "add export")
    _commit({}, "remove export", remove=("database_backup/data_backup.json",))

    # The tip is clean, but the export is in the branch's history.
    assert guard.check_tree() == []
    assert guard.check_range(f"{base}..HEAD") == [
        (f"{added[:12]}:database_backup/data_backup.json", "export directory")
    ]
    assert guard.main(["--range", f"{base}..HEAD"]) == 1


def test_removal_commit_itself_passes(repo: Path) -> None:
    _commit({"database_backup/data_backup.json": b"{}"}, "old export")
    base = _git("rev-parse", "HEAD")
    _commit({}, "remove export", remove=("database_backup/data_backup.json",))
    assert guard.main(["--range", f"{base}..HEAD"]) == 0


def test_output_names_paths_not_contents(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit({"x.dump": b"MARKER-should-not-be-printed"}, "dump")
    assert guard.main([]) == 1
    err = capsys.readouterr().err
    assert "x.dump" in err
    assert "MARKER" not in err


def test_file_added_by_a_merge_and_removed_later_is_caught(repo: Path) -> None:
    base = _git("rev-parse", "HEAD")
    _git("checkout", "-q", "-b", "side")
    _commit({"side.txt": b"side\n"}, "side work")
    _git("checkout", "-q", "main")
    _commit({"main.txt": b"main\n"}, "main work")
    _git("merge", "-q", "--no-commit", "side")
    _commit({"cache.sqlite3": b"x"}, "merge side")
    merge = _git("rev-parse", "HEAD")
    _commit({}, "clean up", remove=("cache.sqlite3",))

    assert guard.check_tree() == []
    assert guard.check_range(f"{base}..HEAD") == [
        (f"{merge[:12]}:cache.sqlite3", "database file extension")
    ]


def test_range_takes_rev_list_arguments(repo: Path) -> None:
    _git("branch", "published")
    added = _commit({"x.db": b"x"}, "add db")
    _commit({}, "remove db", remove=("x.db",))
    assert guard.main(["--range", "HEAD", "--not", "published"]) == 1
    assert guard.check_range("HEAD", "--not", "published") == [
        (f"{added[:12]}:x.db", "database file extension")
    ]
