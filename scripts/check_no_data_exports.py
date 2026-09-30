"""Refuse database dumps and SQLite data files in the repository.

A JSON export of production tables (``database_backup/data_backup.json``)
sat on main for over a year. This guard keeps that class of file out: it
fails on any database file, dump or export by path or by content, so a
dump renamed to something innocent is still caught.

Two modes:

* ``python scripts/check_no_data_exports.py`` checks every tracked file in
  the current tree.
* ``--range BASE..HEAD`` also checks every file added or changed by each
  commit in the range, merge commits included. A dump committed and then
  deleted inside a pull request still lands in history on merge, so the tree
  alone is not enough. ``--range`` takes any rev-list arguments, e.g.
  ``--range HEAD --not --remotes``.

Findings print the path and the rule only, never file contents.

Schema and migrations are not data: ``alembic/`` and ``*.sql`` DDL files
pass unless their content looks like a pg_dump. Add a path to ``ALLOWED``
only for a synthetic fixture, with a comment saying where it comes from.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Iterable

# Directories that only ever held exports.
BLOCKED_DIRS = ("database_backup/",)

# Database files and their journals, and dump archives.
BLOCKED_SUFFIXES = (
    ".db",
    ".db3",
    ".sqlite",
    ".sqlite3",
    ".db-journal",
    ".db-wal",
    ".db-shm",
    ".sqlite-journal",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite3-journal",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".dump",
    ".pgdump",
    ".backup",
)

# Data files whose name says they are a backup or a dump.
BLOCKED_NAME = re.compile(
    r"(backup|dump)[^/]*\.(json|jsonl|ndjson|csv|tsv|sql|gz|zip|tar|tgz|bz2|xz|zst)$",
    re.IGNORECASE,
)

SQLITE_MAGIC = b"SQLite format 3\x00"
PGDUMP_MAGIC = b"PGDMP"
PGDUMP_TEXT = re.compile(rb"^-- (PostgreSQL|MySQL) database dump", re.MULTILINE)
# Row data in a plain-text pg_dump.
PGDUMP_COPY = re.compile(rb"^COPY [\w.\"]+ \([^)]*\) FROM stdin;$", re.MULTILINE)

HEAD_BYTES = 8192

# Synthetic fixtures that would otherwise match. Keep empty unless needed.
ALLOWED: frozenset[str] = frozenset()


def path_rule(path: str) -> str | None:
    """Name the rule a path breaks, or None."""
    lower = path.lower()
    if any(lower.startswith(d) or f"/{d}" in lower for d in BLOCKED_DIRS):
        return "export directory"
    if lower.endswith(BLOCKED_SUFFIXES):
        return "database file extension"
    if BLOCKED_NAME.search(lower.rsplit("/", 1)[-1]):
        return "backup/dump data file name"
    return None


def content_rule(head: bytes) -> str | None:
    """Name the rule the first bytes of a file break, or None."""
    if head.startswith(SQLITE_MAGIC):
        return "SQLite database content"
    if head.startswith(PGDUMP_MAGIC):
        return "pg_dump archive content"
    if PGDUMP_TEXT.search(head) or PGDUMP_COPY.search(head):
        return "SQL dump content"
    return None


def violations(
    paths: Iterable[str], read_head: Callable[[str], bytes | None]
) -> list[tuple[str, str]]:
    found = []
    for path in paths:
        if path in ALLOWED:
            continue
        rule = path_rule(path)
        if rule is None:
            head = read_head(path)
            if head is not None:
                rule = content_rule(head)
        if rule is not None:
            found.append((path, rule))
    return found


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True
    ).stdout


def _split_z(out: bytes) -> list[str]:
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def _blob_head(rev: str, path: str) -> bytes | None:
    proc = subprocess.run(
        ["git", "cat-file", "blob", f"{rev}:{path}"], capture_output=True
    )
    if proc.returncode != 0:
        return None  # submodule or symlink target outside the tree
    return proc.stdout[:HEAD_BYTES]


def check_tree(rev: str = "HEAD") -> list[tuple[str, str]]:
    paths = _split_z(_git("ls-tree", "-r", "-z", "--name-only", rev))
    return violations(paths, lambda p: _blob_head(rev, p))


def check_range(*revs: str) -> list[tuple[str, str]]:
    found = []
    for commit in _git("rev-list", *revs, "--").decode().split():
        # -m diffs a merge against each parent, so a file introduced while
        # resolving a merge is seen; duplicates across parents are dropped.
        paths = dict.fromkeys(
            _split_z(
                _git(
                    "diff-tree", "-m", "--root", "--no-commit-id", "-r", "-z",
                    "--name-only", "--diff-filter=ACMRT", commit,
                )
            )
        )
        for path, rule in violations(paths, lambda p: _blob_head(commit, p)):
            found.append((f"{commit[:12]}:{path}", rule))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--range",
        dest="revs",
        nargs=argparse.REMAINDER,
        help="also check every commit rev-list selects, e.g. BASE..HEAD",
    )
    args = parser.parse_args(argv)

    found = check_tree()
    if args.revs:
        found += check_range(*args.revs)

    if not found:
        print("No database files or dumps tracked.")
        return 0
    print("Database files or dumps must not be committed:", file=sys.stderr)
    for path, rule in found:
        print(f"  {path}  ({rule})", file=sys.stderr)
    print(
        "Remove them from the commit (and from every commit in the branch). "
        "Test data must be synthetic; see scripts/check_no_data_exports.py.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
