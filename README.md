# Smarter Dev

> Build the skills AI can't replace.

This repo holds the website, Discord bot, and supporting services that power the [Smarter Dev](https://smarter.dev) community — a 13,000+ member community focused on critical thinking, architecture, and adversarial reasoning.

- **Website:** <https://smarter.dev>
- **Discord:** <https://smarter.dev/discord>
- **Premium membership:** <https://smarter.dev/sudo>

## What's in this repo

- **Website** — [Skrift](https://github.com/ZechCodes/Skrift)-based async CMS at `smarter.dev`. Marketing pages, blog, admin, and Skrift-managed content.
- **Discord bot** — slash commands powering the community: a Bytes economy, Squads system, daily Quests, multi-stage Challenges, AI-assisted help, moderation, forum notifications, scheduled messages, and more.
- **Web API** — native Litestar controllers mounted at `/api` (part of the Skrift app, in `smarter_dev/web/api_native/`). Bot authenticates with Skrift-native `sk_` bearer service keys; sessions and OAuth handled by Skrift.
- **Bot admin** — Litestar controllers inside Skrift's `/admin` interface (guild config, Bytes, Squads, Campaigns, scheduled messages, and the rest of the bot's data), plus Skrift's built-in API-key management at `/admin/api-keys`.
- **Scan** — AI research assistant, served from `scan.smarter.dev`.

## Architecture

Everything runs against a single PostgreSQL database (`smarter_dev`): Skrift core tables (users, roles, pages, API keys, etc.) and all app tables (bot data — Bytes, Squads, Challenges, forum subs, scheduled messages — plus research, scan, quests, moderation, campaign signups) live in the `skrift` schema.

Two migration sets apply to it, in dependency order:
- Skrift core migrations — bundled with the [`skrift` package](https://github.com/ZechCodes/Skrift).
- `alembic/main/` — every project table (in the `skrift` schema), version table `alembic_version_app`.

`scripts/migrate.py` orchestrates both runs.

## Local development

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with `docker compose` (Docker Desktop, Colima, or equivalent).
- [uv](https://github.com/astral-sh/uv) for the bootstrap script and any out-of-container Python work.
- Python 3.13 (managed by uv).

### One-time setup

```sh
git clone git@github.com:Smarter-Dev/smarter-dev.git
cd smarter-dev
cp .env.example .env
```

The defaults in `.env.example` are wired for the local compose stack — no edits required to get the stack standing up. `scripts/bootstrap.py` will append `BOT_API_KEY` automatically. Real Discord and LLM credentials are only needed for the features that use them (see [Optional secrets](#optional-secrets) below).

> **Note for Colima / non-default Docker setups:** Postgres' init scripts (in `scripts/postgres-init/`) only run if the project lives somewhere your Docker engine can bind-mount. Colima shares your home directory by default, so cloning under `~` works; cloning under `/tmp` or other unshared paths will leave the `skrift` schema uncreated and bootstrap will fail with `schema "skrift" does not exist`.

### Bring it up

```sh
docker compose up -d postgres
uv run python scripts/bootstrap.py
docker compose up -d redis web bot
```

What that does:

1. Starts Postgres on host port `5434`. The init script in `scripts/postgres-init/` creates the `skrift` schema in the `smarter_dev` database.
2. `bootstrap.py`:
   - Runs Skrift + main migrations (via `scripts/migrate.py`).
   - Marks Skrift setup complete (otherwise the dispatcher redirects all routes — including `/api/*` — to its setup wizard).
   - Mints a Skrift-native `discord-bot` service API key (`sk_…`) in `skrift.api_keys`, scopes `bot:read,bot:write`.
   - Writes the plaintext key into `.env` as `BOT_API_KEY=…`.
3. Starts Redis on `6380`, web on `8001` (host) → `8000` (container), and the bot.

Visit <http://localhost:8001>.

### Bootstrap flags

```sh
uv run python scripts/bootstrap.py --rotate-key      # force-rotate the bot key
uv run python scripts/bootstrap.py --skip-migrations # skip the migration step
```

Re-running without flags is idempotent: if the row in the DB and the key in `.env` agree, nothing changes; if they disagree, a fresh key is generated.

### Resetting

```sh
docker compose down -v       # nukes volumes, full reset
docker compose up -d postgres
uv run python scripts/bootstrap.py
docker compose up -d redis web bot
```

### Auth in development

`app.development.yaml` configures Skrift's built-in `dummy` auth provider so you can log into the admin without setting up real OAuth credentials. Production (`app.yaml`) uses Discord/GitHub/Google; Skrift refuses to start with the dummy provider in production.

### Optional secrets

These only matter when you exercise the feature:

| Var | Feature |
|---|---|
| `DISCORD_BOT_TOKEN` + `DISCORD_APPLICATION_ID` | The bot connecting to Discord. Without these the bot logs an error and exits cleanly. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | LLM-backed features (mod agent, help, mention, scan synthesis). `GEMINI_API_KEY` is also used for Discord voice message TTS. |
| `BRAVE_SEARCH_API_KEY` / `JINA_API_KEY` / `YOUTUBE_API_KEY` | Scan tools (search, page reader, YouTube). |
| `RESEND_API_KEY` | Outbound email (e.g. signup confirmations). Logs a warning and skips if absent. |
| `SPACES_ACCESS_KEY` / `SPACES_SECRET_KEY` | DigitalOcean Spaces / S3-compatible asset storage. |
| `DISCORD_APPLICATION_SECRET`, `GITHUB_CLIENT_*`, `GOOGLE_CLIENT_*` | Real OAuth providers (production only). |

## Working in the codebase

```sh
# Quality
uv run ruff check .
uv run mypy smarter_dev

# Tests
uv run pytest

# Generate a new project migration after editing models
uv run alembic -c alembic/main/alembic.ini revision --autogenerate -m "describe change"
```

`alembic/main` owns every project model table — add new tables to `MAIN_TABLES` in `alembic/main/env.py` (guarded by `tests/test_migration_ownership.py`).

## Troubleshooting

**`/api/*` redirects to `/setup` on a fresh DB.** Skrift's dispatcher locks routing to its setup wizard until `setup_completed_at` is recorded. Run `uv run python scripts/bootstrap.py` (the `mark Skrift setup complete` step does this), then `docker compose up -d --force-recreate web` so the dispatcher re-decides at startup.

**Bot logs `200` responses but reports `Expecting value: line 1 column 1 (char 0)`.** Same root cause as above — the bot's HTTP client follows the `/setup` redirect and tries to JSON-parse the wizard's HTML. Fix is the same.

**`scripts/migrate.py` reports success but no tables are created.** This is [an upstream Skrift bug](https://github.com/ZechCodes/Skrift/issues/123) — the `skrift db` CLI silently no-ops when the project path contains a space. `scripts/migrate.py` works around it by invoking alembic directly. Don't run `skrift db upgrade heads` from a path with a space.

**Port 5434 / 6380 / 8001 already in use.** Change the host-side port in `compose.yaml` and the matching `localhost:<port>` references in `.env`.

## License

MIT.
