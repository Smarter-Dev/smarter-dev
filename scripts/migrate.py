#!/usr/bin/env python
"""Run all database migrations for the smarter-dev project.

The app runs against a single database — DATABASE_URL — with Skrift core
tables and the project's app tables both living in the `skrift` schema.
Two migration sets apply to it, in dependency order:

  1. Skrift core   -> from the installed skrift package (creates skrift schema tables)
  2. main app      -> alembic/main (creates app tables in the skrift schema)

Each step runs in its own subprocess so settings singletons never leak
between steps. Skrift's own `skrift db` CLI is not used because it joins
multiple version_locations with a literal space, which silently breaks when
the project path contains a space (`Smarter Dev` in our path). We invoke
alembic directly.

Usage:
    uv run python scripts/migrate.py
    uv run python scripts/migrate.py --only skrift-main
    uv run python scripts/migrate.py --only main
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Each step is rendered as a tiny Python program executed via `uv run python -c`.
# Subprocesses keep settings-singleton caching from leaking across steps.
_STEP_SCRIPTS: dict[str, str] = {
    "skrift-main": textwrap.dedent(
        """
        from pathlib import Path
        from alembic.config import Config
        from alembic import command
        skrift_pkg = Path(__import__('skrift').__file__).parent
        cfg = Config(str(skrift_pkg / 'alembic.ini'))
        cfg.set_main_option('script_location', str(skrift_pkg / 'alembic'))
        command.upgrade(cfg, 'heads')
        """
    ),
    "main": textwrap.dedent(
        """
        from alembic.config import Config
        from alembic import command
        cfg = Config('alembic/main/alembic.ini')
        command.upgrade(cfg, 'head')
        """
    ),
}


def _resolve_database_url() -> str:
    """Resolve DATABASE_URL via the project's settings.

    Reading through pydantic-settings means we pick up the values in `.env`
    even when the caller's shell hasn't exported them.
    """
    from smarter_dev.shared.config import get_settings  # noqa: WPS433
    return get_settings().effective_database_url


def _step_env(database_url: str) -> dict[str, str]:
    """Return env vars for the subprocess running a step."""
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    return env


def run_step(step: str, database_url: str) -> None:
    print(f"==> {step}")
    result = subprocess.run(
        [sys.executable, "-c", _STEP_SCRIPTS[step]],
        cwd=str(REPO_ROOT),
        env=_step_env(database_url),
    )
    if result.returncode != 0:
        sys.exit(f"step {step} failed with exit code {result.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--only",
        choices=list(_STEP_SCRIPTS),
        action="append",
        help="Run only the listed step(s). Repeatable. Default: all.",
    )
    args = parser.parse_args()
    steps = args.only or list(_STEP_SCRIPTS)
    database_url = _resolve_database_url()
    for step in steps:
        run_step(step, database_url)


if __name__ == "__main__":
    main()
