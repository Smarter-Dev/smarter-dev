# 06 — Local End-to-End Smoke Harness

The harness is the ground truth for "nothing broke" during the legacy-sunset
migration. It boots the **whole app** (Skrift + mounted legacy FastAPI `/api`
+ legacy Starlette `/bot-admin`) against throwaway podman postgres/redis,
seeds representative data across both databases, and asserts every
bot-consumed API endpoint and every admin page still works. Verification
gates run it before every phase transition.

## Running it

```bash
uv run python scripts/local_harness/run.py
```

Exit code 0 = all checks green. Each check prints a named `[PASS]`/`[FAIL]`
line. A full run takes ~2–3 minutes.

Flags:

| Flag | Effect |
| --- | --- |
| `--keep-up` | Leave containers + app running after the checks and print URLs/credentials for manual poking. Tear down with `podman rm -f smarter_dev_harness_postgres smarter_dev_harness_redis` and kill the hypercorn process. |
| `--skip-infra` | Reuse already-running harness containers (skips podman setup, migrations, and seeding). Useful for fast re-check loops after `--keep-up`. |

## What it stands up

| Piece | Detail |
| --- | --- |
| postgres | podman `postgres:15-alpine` on port **55432**, container `smarter_dev_harness_postgres`, no named volume. Init scripts from `scripts/postgres-init/` create both databases (`smarter_dev` main + `bc_websites` legacy) with `skrift` schemas — the same layout as prod. |
| redis | podman `redis:7-alpine` on port **56379**, container `smarter_dev_harness_redis`. |
| migrations | `scripts/migrate.py` (all four trees: skrift-main, main, skrift-legacy, legacy) run as subprocesses with `DATABASE_URL`/`LEGACY_DATABASE_URL` pointed at the harness DBs. |
| seed | `scripts/local_harness/seed.py` — since the phase-02 DB consolidation everything seeds into the **main DB's `skrift` schema**: the adopted bot tables (bytes config/balances/transactions, squads + memberships, sale events, forum agents + responses + notification topics + subscriptions, campaigns + challenges, scheduled + repeating messages, audit-log/AoC/attachment-filter configs, help conversations, channel model overrides) plus quests + daily quest, feature flags, chat-agent engagement, member activity, channel handler, Skrift setup-complete settings, and a **known-plaintext Skrift-native `sk_` service API key** in `skrift.api_keys` owned by a `bot@smarter.dev` service user. No legacy `sk-` key is seeded (the legacy api_keys table is unreachable post-flip; the checks assert the retired key 401s). The legacy `bc_websites` DB gets migrations but no seeds. |
| app | `hypercorn scripts.local_harness.harness_app:app` on port **8791** with `SKRIFT_ENV=development`. `harness_app` is `main:app` plus one patch: the legacy admin's `DiscordClient.base_url` is pointed at the local mock Discord server. |
| mock Discord | `scripts/local_harness/mock_discord.py` on port **8792** — serves the guild/roles/channels fixtures the legacy `/bot-admin` views fetch, so every page renders fully offline. |

Ports/container names are deliberately far from the dev compose (5434/6380),
so the harness never collides with a running dev stack. Nothing ever touches
remote infrastructure.

## What it checks (121 checks)

All expectations live in **`scripts/local_harness/expectations.py`** as three
data-driven tables:

1. **`API_CHECKS`** — every bot-consumed `/api` endpoint group with Bearer
   auth: health, auth validate/status (with the seeded Skrift-native `sk_`
   key; the retired legacy `sk-` key must 401),
   bad/malformed/missing/unknown-`sk_` key → 401/403,
   bytes (balance/daily/transactions/leaderboard/config), squads
   (list/detail/join/members/lookup/leave), challenges (pending + upcoming
   announcements, detail, mark-announced/released, scoreboards, upcoming
   campaign), quests (daily current, mark-announced/active, upcoming),
   scheduled + repeating messages (pending/due/upcoming/mark-sent), advent of
   code (active-configs, config, threads list/lookup/record), forum agents
   (list, record response, response count) + notification topics and user
   subscriptions (lookup + upsert), channel model overrides (seeded GET, PUT
   → GET → DELETE(204) → GET(404)), chat conversations (engagement create →
   turn create → end → usage leaderboard), image quota, member-activity
   batch, handlers (list + active channels), member delete. Status codes and
   key response fields are asserted; several checks assert seeded values
   exactly (e.g. balance == 1000).
2. **`SKRIFT_ADMIN_PAGES`** — every registered Skrift `/admin` page with a
   parameterless GET (plus the guild-scoped bot pages) returns 200 for an
   authenticated admin. The runner logs in through the dev **dummy auth
   provider** (`/auth/dummy/login` → CSRF → `/auth/dummy-login` with the
   admin toggle), which creates the admin user itself.
3. **`LEGACY_ADMIN_PAGES`** — every legacy `/bot-admin` GET page returns 200
   for an admin session. The runner **forges the Starlette session cookie**
   (itsdangerous-TimestampSigner over base64 JSON, signed with the harness
   `WEB_SESSION_SECRET`) exactly as `smarter_dev/web/admin/app.py` configures
   it.
4. **`UNAUTHENTICATED_PAGES`** — anonymous requests to `/admin/` and
   `/bot-admin/` must NOT return 200.

Well-known IDs (guild, users, squad/campaign/challenge UUIDs, the API key)
live in `scripts/local_harness/config.py`; seeds and checks share them, which
is what lets the check table stay static.

## Updating expectations when behavior intentionally changes

Edit **only** `scripts/local_harness/expectations.py` (and, if the seeded
data must change, `seed.py`/`config.py`) in the *same change* that alters the
behavior. Examples for later sunset phases:

- **New API key format** (Skrift-native `sk_` keys): done — only
  `config.SKRIFT_BOT_API_KEY` (seeded by `seed.py:_seed_skrift_bot_api_key`)
  authenticates. The phase-02 DB consolidation retired the legacy `sk-` key:
  `config.LEGACY_BOT_API_KEY` is never seeded and the `auth-legacy-key-401`
  check asserts it rejects cleanly. In phase 05 the fallback code itself is
  deleted; keep the bad/malformed-key 401 checks.
- **Removed `/bot-admin`**: replace `LEGACY_ADMIN_PAGES` entries with
  the redirect/410 behavior you expect (e.g.
  `AdminPageCheck("bot-admin-gone", "/bot-admin/", expect_status=(404,))`),
  and drop the forged-cookie client once nothing needs it.
- **Ported admin pages**: add rows to `SKRIFT_ADMIN_PAGES` for each new
  `/admin/...` route as it lands.
- **Single database**: done in phase 02 — all seeds land in the main DB's
  `skrift` schema; `LEGACY_DATABASE_URL` still points at the `bc_websites`
  container only for the closed `alembic/legacy` migrate step (dropped with
  the tree in phase 05).

Keep every row a named check — the gate output should always say exactly
which behavior regressed.

## Fixes made while getting the harness green (pre-existing bugs)

These were defects in current `main` that only shows up on fresh
databases/pages, found and fixed by the first harness run:

1. `alembic/main/versions/20260703_170000_e3c9a1f7b2d5_backfill_base_timestamps.py`
   — added guards: the timestamp backfill columns already exist on a fresh DB
   (created by the regenerated initial schema), so each `ADD COLUMN` now
   checks `information_schema` first. No-op on prod where it already ran.
2. `smarter_dev/web/admin/routes.py` — `/conversations/cleanup` was
   registered *after* `/conversations/{conversation_id}`, so Starlette routed
   "cleanup" into the detail view as a bogus UUID (500). Reordered.
3. `templates/bot-admin/repeating_message_form.html` — the create view
   referenced this template but it never existed (500 on the create page);
   added it, modeled on the edit template.
