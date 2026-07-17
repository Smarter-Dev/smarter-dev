#!/usr/bin/env python
"""Bootstrap the local development environment.

Runs migrations and provisions a bot API key so a fresh
`docker compose up -d postgres` -> `python scripts/bootstrap.py`
sequence yields a working stack.

What this does:
  1. Apply migrations via `scripts/migrate.py` (Skrift core + main app).
  2. Mark Skrift setup complete (sets `setup_completed_at` in
     `skrift.settings`) so the dispatcher serves the real app instead
     of redirecting `/api/*` to the setup wizard.
  3. Mint a Skrift-native `discord-bot` service key (`sk_...`) in the
     `skrift.api_keys` table, owned by a dedicated `bot@smarter.dev`
     service user (or rotate the existing one). Skrift keys are the only
     keys the API accepts (docs/v2/legacy-sunset/01-skrift-api-keys.md).
  4. Write `BOT_API_KEY=<plaintext>` into `.env` so the bot service
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
import hashlib
import re
import subprocess
import sys
import textwrap
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from skrift.auth.services import assign_role_to_user, sync_roles_to_database
from skrift.db.models.api_key import APIKey as SkriftAPIKey
from skrift.db.models.user import User
from skrift.db.services import api_key_service

import smarter_dev.web.roles  # noqa: F401 — registers the bot-service role for the sync below

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
BOT_SERVICE_NAME = "discord-bot"
BOT_SERVICE_USER_EMAIL = "bot@smarter.dev"
BOT_KEY_SCOPES = ["bot-api", "bot-api-admin"]
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


async def _mark_setup_complete() -> str:
    """Set Skrift's `setup_completed_at` setting so the dispatcher serves
    the real app instead of redirecting all routes (including `/api/*`)
    to the setup wizard.

    Returns the status: 'already_set' | 'marked'.
    """
    from skrift.db.services.setting_service import (
        SETUP_COMPLETED_AT_KEY,
        get_setting,
        set_setting,
    )
    from skrift.setup.state import create_setup_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smarter_dev.shared.config import get_settings
    from smarter_dev.shared.database import convert_postgres_url_for_asyncpg

    settings = get_settings()
    db_url = convert_postgres_url_for_asyncpg(settings.effective_database_url)
    engine = create_setup_engine(db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            existing = await get_setting(session, SETUP_COMPLETED_AT_KEY)
            if existing:
                return "already_set"

            from datetime import UTC, datetime
            await set_setting(session, SETUP_COMPLETED_AT_KEY, datetime.now(UTC).isoformat())
            await session.commit()
            return "marked"
    finally:
        await engine.dispose()


async def _get_or_create_bot_service_user(session: AsyncSession) -> User:
    """Fetch (or create) the dedicated service user that owns the bot key."""
    existing_user = (
        await session.execute(select(User).where(User.email == BOT_SERVICE_USER_EMAIL))
    ).scalar_one_or_none()
    if existing_user is not None:
        return existing_user

    service_user = User(
        email=BOT_SERVICE_USER_EMAIL,
        name=BOT_SERVICE_NAME,
        is_active=True,
    )
    session.add(service_user)
    await session.commit()
    return service_user


async def provision_skrift_bot_key(
    session: AsyncSession, env_key: str | None, rotate: bool
) -> tuple[str, str]:
    """Reuse, rotate, or create the Skrift-native bot service key.

    Keys live in the main DB ``skrift.api_keys`` table (the session must be
    bound there), minted via ``api_key_service.create_api_key`` with
    ``principal_type='service'`` and owned by the ``bot@smarter.dev`` service
    user, mirroring docs/v2/legacy-sunset/runbooks/01-rotate-bot-key.md.

    Returns (raw_plaintext_key, status) where status is one of:
        'reused' | 'rotated' | 'created'
    """
    service_user = await _get_or_create_bot_service_user(session)

    # Skrift resolves a key's effective permissions as key-scope ∩ user-actual,
    # so the owning service user must hold the bot-service role or the scoped
    # bot-api permissions resolve to nothing and every API call 401s.
    await sync_roles_to_database(session)
    if not await assign_role_to_user(session, service_user.id, "bot-service"):
        raise RuntimeError("bot-service role missing after role sync")

    active_bot_keys = list(
        (
            await session.execute(
                select(SkriftAPIKey).where(
                    SkriftAPIKey.service_name == BOT_SERVICE_NAME,
                    SkriftAPIKey.principal_type == "service",
                    SkriftAPIKey.is_active.is_(True),
                )
            )
        ).scalars().all()
    )

    # If .env already holds a key whose hash matches an active row, the
    # existing setup is consistent; reuse without rotation.
    if env_key and not rotate:
        env_key_hash = hashlib.sha256(env_key.encode("utf-8")).hexdigest()
        if any(key.key_hash == env_key_hash for key in active_bot_keys):
            return env_key, "reused"

    # Either there's no usable row, no matching .env key, or the user
    # requested rotation: deactivate the stale rows and mint a fresh key.
    for stale_key in active_bot_keys:
        stale_key.is_active = False
    status = "rotated" if active_bot_keys else "created"

    _api_key, raw_key, _raw_refresh = await api_key_service.create_api_key(
        session,
        service_user.id,
        BOT_SERVICE_NAME,
        description=BOT_KEY_DESCRIPTION,
        scoped_permissions=BOT_KEY_SCOPES,
        principal_type="service",
        service_name=BOT_SERVICE_NAME,
    )
    return raw_key, status


async def _provision_bot_key(rotate: bool) -> tuple[str, str]:
    """Provision the Skrift-native bot API key against the main DB.

    Returns (full_plaintext_key, status) where status is one of:
        'reused' | 'rotated' | 'created'
    """
    from smarter_dev.shared.config import get_settings
    from smarter_dev.shared.database import (
        convert_postgres_url_for_asyncpg,
        create_async_engine,
    )

    settings = get_settings()
    db_url = convert_postgres_url_for_asyncpg(settings.effective_database_url)
    engine = create_async_engine(db_url).execution_options(
        schema_translate_map={None: "skrift"}
    )

    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return await provision_skrift_bot_key(
                session, env_key=_read_env_bot_key(), rotate=rotate
            )
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

    print("==> marking Skrift setup complete")
    setup_status = asyncio.run(_mark_setup_complete())
    print(f"    setup: {setup_status}")

    print("==> provisioning bot API key")
    full_key, key_status = asyncio.run(_provision_bot_key(rotate=args.rotate_key))
    _write_env_bot_key(full_key)

    print(
        textwrap.dedent(
            f"""\
            ==> bot API key {key_status} (Skrift service key '{BOT_SERVICE_NAME}')
                scopes:   {", ".join(BOT_KEY_SCOPES)}
                prefix:   {full_key[:12]}...
                .env:     BOT_API_KEY updated at {ENV_FILE}

            Restart web + bot to pick up the new state:
                docker compose up -d --force-recreate web bot
            """
        )
    )


if __name__ == "__main__":
    main()
