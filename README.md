# Smarter Dev

> Build the skills AI can't replace.

This repo holds the website, Discord bot, and supporting services that power the [Smarter Dev](https://smarter.dev) community — a 13,000+ member community focused on critical thinking, architecture, and adversarial reasoning.

- **Website:** <https://smarter.dev>
- **Discord:** <https://smarter.dev/discord>
- **Premium membership:** <https://smarter.dev/sudo>

## What's in this repo

- **Website** — [Skrift](https://github.com/ZechCodes/Skrift)-based async CMS at `smarter.dev`. Marketing pages, blog, admin, and Skrift-managed content.
- **Discord bot** — slash commands powering the community: a Bytes economy, Squads system, daily Quests, multi-stage Challenges, AI-assisted help, moderation, forum notifications, scheduled messages, and more.
- **Web API** — FastAPI service mounted at `/api`. Bot authenticates with bearer API keys; sessions and OAuth handled by Skrift.
- **Legacy bot-admin** — original Starlette-based admin (still active, lives at `/bot-admin`) that owns the bot's data tables.
- **Scan** — AI research assistant, served from `scan.smarter.dev`.

## Architecture

Two databases on a single PostgreSQL cluster mirror the prod split:

| DB | Schema(s) | Owns |
|---|---|---|
| `smarter_dev` | `skrift` | Skrift core (users, roles, pages, etc.) + new app tables (research, scan, quests, moderation, campaign signups). |
| `bc_websites` | `public` + `skrift` | Legacy bot/admin tables (Bytes, Squads, Challenges, forum subs, scheduled messages, etc.) in `public`; Skrift core in `skrift` for auth. |

Each DB has its own alembic migration set:
- `alembic/main/` — new app tables (in `skrift` schema), version table `alembic_version_app`.
- `alembic/legacy/` — legacy bot tables (in `public` schema), version table `alembic_version_legacy`. Slated for deletion when the legacy admin is retired.
- Skrift core migrations are bundled with the [`skrift` package](https://github.com/ZechCodes/Skrift) and applied to both DBs.

`scripts/migrate.py` orchestrates all four migration runs in dependency order.

## Local development

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with `docker compose` (Docker Desktop, Colima, or equivalent).
- [uv](https://github.com/astral-sh/uv) for the bootstrap script and any out-of-container Python work.
- Python 3.13 (managed by uv).

### One-time setup

```sh
git clone git@github.com:Smarter-Dev/smarter-dev.git
cd smarter-dev

# Optional: copy the example env to start. Defaults work for local dev,
# but you can fill in real Discord credentials etc. as needed.
cp .env.example .env
```

The minimum `.env` needed to stand up the stack locally:

```sh
ENVIRONMENT=development
SKRIFT_ENV=development
DATABASE_URL=postgresql+asyncpg://smarter_dev:smarter_dev_password@localhost:5434/smarter_dev
LEGACY_DATABASE_URL=postgresql+asyncpg://smarter_dev:smarter_dev_password@localhost:5434/bc_websites
REDIS_URL=redis://:smarter_dev_redis_password@localhost:6380/0
SECRET_KEY=any-non-empty-string-for-local-dev
```

`scripts/bootstrap.py` will append `BOT_API_KEY` automatically. Real Discord and LLM credentials are only needed for the features that use them (see [Optional secrets](#optional-secrets) below).

### Bring it up

```sh
docker compose up -d postgres
uv run python scripts/bootstrap.py
docker compose up -d redis web bot
```

What that does:

1. Starts Postgres on host port `5434`. The init script in `scripts/postgres-init/` creates the `bc_websites` database and the `skrift` schema in both DBs.
2. `bootstrap.py`:
   - Runs Skrift + main + legacy migrations (via `scripts/migrate.py`).
   - Marks Skrift setup complete (otherwise the dispatcher redirects all routes — including `/api/*` — to its setup wizard).
   - Provisions a `local-bot` API key in `bc_websites.public.api_keys`, scopes `bot:read,bot:write`.
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
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | LLM-backed features (mod agent, help, mention, scan synthesis). |
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
# (or alembic/legacy/alembic.ini for legacy tables)
```

When editing models, decide which alembic config owns the table by checking the `MAIN_TABLES` / `LEGACY_TABLES` sets in each `env.py`. New tables go in `MAIN_TABLES` unless they're legacy.

## Troubleshooting

**`/api/*` redirects to `/setup` on a fresh DB.** Skrift's dispatcher locks routing to its setup wizard until `setup_completed_at` is recorded. Run `uv run python scripts/bootstrap.py` (the `mark Skrift setup complete` step does this), then `docker compose up -d --force-recreate web` so the dispatcher re-decides at startup.

**Bot logs `200` responses but reports `Expecting value: line 1 column 1 (char 0)`.** Same root cause as above — the bot's HTTP client follows the `/setup` redirect and tries to JSON-parse the wizard's HTML. Fix is the same.

**`scripts/migrate.py` reports success but no tables are created.** This is [an upstream Skrift bug](https://github.com/ZechCodes/Skrift/issues/123) — the `skrift db` CLI silently no-ops when the project path contains a space. `scripts/migrate.py` works around it by invoking alembic directly. Don't run `skrift db upgrade heads` from a path with a space.

**Port 5434 / 6380 / 8001 already in use.** Change the host-side port in `compose.yaml` and the matching `localhost:<port>` references in `.env`.

## License

MIT.
