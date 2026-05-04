#!/usr/bin/env python
"""Run all database migrations for the smarter-dev project.

Local dev mirrors prod, which uses two databases:

  - DATABASE_URL (smarter-dev) — main app, Skrift core + new-app tables in
    the `skrift` schema.
  - LEGACY_DATABASE_URL (bc_websites) — legacy bot/admin tables in `public`,
    Skrift core (used by legacy admin auth) in `skrift`.

Each DB has a Skrift migration set (from the installed package) and a project
migration set (`alembic/main` or `alembic/legacy`). This script orchestrates
them in dependency order, running each step in its own subprocess so settings
singletons don't cache across DB switches:

  1. Skrift core   -> smarter-dev (creates skrift schema tables)
  2. main app      -> smarter-dev (creates app tables in skrift schema)
  3. Skrift core   -> bc_websites (creates skrift schema for legacy auth)
  4. legacy app    -> bc_websites (creates public schema legacy tables)

Skrift's own `skrift db` CLI is not used because it joins multiple
version_locations with a literal space, which silently breaks when the project
path contains a space (`Smarter Dev` in our path). We invoke alembic directly.

Usage:
    uv run python scripts/migrate.py
    uv run python scripts/migrate.py --only skrift-main
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
# Using subprocesses avoids settings-singleton caching across the URL switch
# between the main and legacy databases.
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
    "skrift-legacy": textwrap.dedent(
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
    "legacy": textwrap.dedent(
        """
        from alembic.config import Config
        from alembic import command
        cfg = Config('alembic/legacy/alembic.ini')
        command.upgrade(cfg, 'head')
        """
    ),
}


def _resolve_urls() -> tuple[str, str]:
    """Resolve DATABASE_URL / LEGACY_DATABASE_URL via the project's settings.

    Reading through pydantic-settings means we pick up the values in `.env`
    even when the caller's shell hasn't exported them.
    """
    from smarter_dev.shared.config import get_settings  # noqa: WPS433
    s = get_settings()
    return s.effective_database_url, s.effective_legacy_database_url


def _step_env(step: str, main_url: str, legacy_url: str) -> dict[str, str]:
    """Return env vars for the subprocess running the step."""
    env = os.environ.copy()
    if step.endswith("-legacy") or step == "legacy":
        if not legacy_url:
            sys.exit("LEGACY_DATABASE_URL is not set; legacy migrations cannot run")
        # Skrift's env.py reads from its own settings (db.url -> DATABASE_URL).
        # For the Skrift-against-legacy step we point DATABASE_URL at the legacy
        # DB; our own legacy/env.py reads from effective_legacy_database_url so it
        # keeps working off LEGACY_DATABASE_URL regardless of this swap.
        env["DATABASE_URL"] = legacy_url
        env["LEGACY_DATABASE_URL"] = legacy_url
    else:
        env["DATABASE_URL"] = main_url
        env["LEGACY_DATABASE_URL"] = legacy_url
    return env


def run_step(step: str, main_url: str, legacy_url: str) -> None:
    print(f"==> {step}")
    result = subprocess.run(
        [sys.executable, "-c", _STEP_SCRIPTS[step]],
        cwd=str(REPO_ROOT),
        env=_step_env(step, main_url, legacy_url),
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
    main_url, legacy_url = _resolve_urls()
    for step in steps:
        run_step(step, main_url, legacy_url)


if __name__ == "__main__":
    main()
