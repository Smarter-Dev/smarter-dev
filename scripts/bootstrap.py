#!/usr/bin/env python
"""Bootstrap the local development environment.

Runs migrations and provisions a bot API key so a fresh
`docker compose up -d postgres` -> `python scripts/bootstrap.py`
sequence yields a working stack.

What this does:
  1. Apply migrations via `scripts/migrate.py` (Skrift + main + legacy).
  2. Insert a `local-bot` row in `bc_websites.public.api_keys` with
     scopes `bot:read,bot:write` (or rotate the existing one).
  3. Write `BOT_API_KEY=<plaintext>` into `.env` so the bot service
     picks it up on next start.

Plaintext keys aren't recoverable from the DB hash, so any state where
`.env` and the DB disagree is treated as out-of-sync and triggers a
fresh rotation.

Usage:
    uv run python scripts/bootstrap.py
    uv run python scripts/bootstrap.py --rotate-key
    uv run python scripts/bootstrap.py --skip-migrations
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
BOT_KEY_NAME = "local-bot"
BOT_KEY_SCOPES = ["bot:read", "bot:write"]
BOT_KEY_DESCRIPTION = "Local development bot key (managed by scripts/bootstrap.py)."


def _run_migrations() -> None:
    print("==> running migrations")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "migrate.py")],
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        sys.exit(f"migrations failed (exit {result.returncode})")


def _read_env_bot_key() -> str | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("BOT_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def _write_env_bot_key(key: str) -> None:
    """Set or replace BOT_API_KEY=<key> in .env, creating the file if needed."""
    line = f"BOT_API_KEY={key}"
    if not ENV_FILE.exists():
        ENV_FILE.write_text(line + "\n")
        return

    text = ENV_FILE.read_text()
    new_text, n = re.subn(r"^BOT_API_KEY=.*$", line, text, count=1, flags=re.MULTILINE)
    if n == 0:
        if not new_text.endswith("\n"):
            new_text += "\n"
        new_text += line + "\n"
    ENV_FILE.write_text(new_text)


async def _provision_bot_key(rotate: bool) -> tuple[str, str]:
    """Insert or rotate the bot API key in the legacy DB.

    Returns (full_plaintext_key, status) where status is one of:
        'reused' | 'rotated' | 'created'
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smarter_dev.shared.config import get_settings
    from smarter_dev.shared.database import (
        convert_postgres_url_for_asyncpg,
        create_async_engine,
    )
    from smarter_dev.web.models import APIKey
    from smarter_dev.web.security import generate_secure_api_key, hash_api_key

    settings = get_settings()
    db_url = convert_postgres_url_for_asyncpg(settings.effective_legacy_database_url)
    engine = create_async_engine(db_url)

    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            existing = (
                await session.execute(
                    select(APIKey).where(
                        APIKey.name == BOT_KEY_NAME,
                        APIKey.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()

            env_key = _read_env_bot_key()

            # If a row exists AND .env already holds a key whose hash matches it,
            # the existing setup is consistent; reuse without rotation.
            if existing and env_key and not rotate:
                if hash_api_key(env_key) == existing.key_hash:
                    return env_key, "reused"

            # Either there's no row, no matching .env key, or the user requested
            # rotation. Generate a fresh key, swap in (or create) the row.
            full_key, key_hash, key_prefix = generate_secure_api_key()

            if existing:
                existing.key_hash = key_hash
                existing.key_prefix = key_prefix
                existing.scopes = BOT_KEY_SCOPES
                existing.description = BOT_KEY_DESCRIPTION
                existing.is_active = True
                existing.revoked_at = None
                status = "rotated"
            else:
                session.add(
                    APIKey(
                        name=BOT_KEY_NAME,
                        description=BOT_KEY_DESCRIPTION,
                        key_hash=key_hash,
                        key_prefix=key_prefix,
                        scopes=BOT_KEY_SCOPES,
                        is_active=True,
                        rate_limit_per_second=100,
                        rate_limit_per_minute=2000,
                        rate_limit_per_15_minutes=20000,
                        rate_limit_per_hour=50000,
                        usage_count=0,
                        created_by="scripts/bootstrap.py",
                    )
                )
                status = "created"

            await session.commit()
            return full_key, status
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--rotate-key",
        action="store_true",
        help="Force-rotate the bot API key even if .env and DB are already in sync.",
    )
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Skip the migration step (use when migrations are already applied).",
    )
    args = parser.parse_args()

    if not args.skip_migrations:
        _run_migrations()

    print("==> provisioning bot API key")
    full_key, status = asyncio.run(_provision_bot_key(rotate=args.rotate_key))
    _write_env_bot_key(full_key)

    print(
        textwrap.dedent(
            f"""\
            ==> bot API key {status} ({BOT_KEY_NAME})
                scopes:   {", ".join(BOT_KEY_SCOPES)}
                prefix:   {full_key[:12]}...
                .env:     BOT_API_KEY updated at {ENV_FILE}

            Restart the bot to pick up the new key:
                docker compose up -d --force-recreate bot
            """
        )
    )


if __name__ == "__main__":
    main()
