"""Shared constants for the local smoke harness.

Everything the harness stages — container names, ports, credentials, and the
well-known IDs the seed data uses — lives here so checks and seeds always
agree. Ports are deliberately far away from the dev compose ports (5434/6380)
so the harness never collides with a running dev stack.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Infrastructure (podman containers; unique names + ports, no named volumes)
# ---------------------------------------------------------------------------
POSTGRES_CONTAINER = "smarter_dev_harness_postgres"
REDIS_CONTAINER = "smarter_dev_harness_redis"
POSTGRES_PORT = 55432
REDIS_PORT = 56379
APP_PORT = 8791
MOCK_DISCORD_PORT = 8792

POSTGRES_IMAGE = "postgres:15-alpine"
REDIS_IMAGE = "redis:7-alpine"

DB_USER = "smarter_dev"
DB_PASSWORD = "smarter_dev_password"
REDIS_PASSWORD = "smarter_dev_redis_password"

MAIN_DB_NAME = "smarter_dev"

MAIN_DATABASE_URL = (
    f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@localhost:{POSTGRES_PORT}/{MAIN_DB_NAME}"
)
REDIS_URL = f"redis://:{REDIS_PASSWORD}@localhost:{REDIS_PORT}/0"

# app.development.yaml sets ``domain: localhost``; Skrift redirects other
# hosts to the canonical domain, so the checks must use localhost too.
APP_BASE_URL = f"http://localhost:{APP_PORT}"
API_BASE_URL = f"{APP_BASE_URL}/api"
MOCK_DISCORD_BASE_URL = f"http://127.0.0.1:{MOCK_DISCORD_PORT}"

WEB_SESSION_SECRET = "harness-web-session-secret"


def _deterministic_skrift_api_key(seed: str) -> str:
    """Derive a valid-format Skrift API key (sk_ + 43 base64url chars).

    Deterministic so the seed subprocess and the checks process agree on the
    plaintext without storing a literal key in the repo; derived keys are
    local-harness-only credentials.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    token = base64.urlsafe_b64encode(digest + digest).decode("ascii")[:43]
    return f"sk_{token}"


# Known-plaintext Skrift-native key seeded into skrift.api_keys —
# the only key shape the API accepts.
SKRIFT_BOT_API_KEY = _deterministic_skrift_api_key(
    "smarter-dev-local-harness-skrift-bot-key"
)
# Valid Skrift format, never seeded — must always 401.
UNKNOWN_SKRIFT_API_KEY = _deterministic_skrift_api_key(
    "smarter-dev-local-harness-skrift-unknown"
)

# Owner of the seeded Skrift service key (mirrors the prod recommendation in
# docs/v2/legacy-sunset/runbooks/01-rotate-bot-key.md).
BOT_SERVICE_USER_EMAIL = "bot@smarter.dev"
BOT_SERVICE_NAME = "discord-bot"

# ---------------------------------------------------------------------------
# Well-known seed identifiers (fixed so the check table can be static)
# ---------------------------------------------------------------------------
GUILD_ID = "111111111111111111"
TEXT_CHANNEL_ID = "222200000000000001"
FORUM_CHANNEL_ID = "222200000000000002"
SQUAD_ROLE_ID = "333300000000000001"
OTHER_ROLE_ID = "333300000000000002"

USER_ID = "444400000000000001"
USER_NAME = "harness-user"
JOINER_USER_ID = "444400000000000002"
JOINER_USER_NAME = "harness-joiner"
DELETABLE_USER_ID = "444400000000000003"
DELETABLE_USER_NAME = "harness-deletable"

SQUAD_ID = "00000000-0000-4000-8000-000000000001"
FORUM_AGENT_ID = "00000000-0000-4000-8000-000000000002"
CAMPAIGN_ID = "00000000-0000-4000-8000-000000000003"
CHALLENGE_ID = "00000000-0000-4000-8000-000000000004"
SCHEDULED_MESSAGE_ID = "00000000-0000-4000-8000-000000000005"
REPEATING_MESSAGE_ID = "00000000-0000-4000-8000-000000000006"
QUEST_ID = "00000000-0000-4000-8000-000000000007"
DAILY_QUEST_ID = "00000000-0000-4000-8000-000000000008"
HELP_CONVERSATION_ID = "00000000-0000-4000-8000-000000000009"
CHAT_ENGAGEMENT_ID = "00000000-0000-4000-8000-00000000000a"

AOC_YEAR = 2025
AOC_DAY = 1
AOC_THREAD_ID = "555500000000000001"

MODEL_OVERRIDE_CHANNEL_ID = "222200000000000003"
MODEL_OVERRIDE_MODEL_KEY = "kimi-k2-6"

# Skrift admin login (dummy provider, dev-only)
ADMIN_EMAIL = "harness-admin@example.com"
ADMIN_NAME = "Harness Admin"


def harness_env() -> dict[str, str]:
    """Environment for every harness subprocess (migrate, seed, app)."""
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(REPO_ROOT)
    )
    env.update(
        {
            "SKRIFT_ENV": "development",
            "ENVIRONMENT": "development",
            "DATABASE_URL": MAIN_DATABASE_URL,
            "REDIS_URL": REDIS_URL,
            "WEB_SESSION_SECRET": WEB_SESSION_SECRET,
            # Skrift's own Settings.secret_key (sessions/CSRF); dev-only value.
            "SECRET_KEY": "harness-skrift-secret-key",
            # Any non-empty token satisfies get_admin_discord_client(); actual
            # calls are redirected to the local mock via
            # HARNESS_DISCORD_API_BASE_URL.
            "DISCORD_BOT_TOKEN": "harness-dummy-bot-token",
            "HARNESS_DISCORD_API_BASE_URL": MOCK_DISCORD_BASE_URL,
        }
    )
    return env
