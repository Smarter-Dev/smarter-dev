# 02 — Database Consolidation (bc_websites → main DB, skrift schema)

Move the 24 legacy tables out of bc_websites `public` into the main DB's
`skrift` schema, then flip the default session accessors so every legacy code
path reads/writes the main DB. After this phase there is exactly one database
in use; bc_websites stays up read-only as the rollback net until phase 05.

## Current state (verified)

- `smarter_dev/shared/database.py:106 create_engine(settings, *,
  use_legacy_db=True)` — **default legacy**; line 115 picks
  `effective_legacy_database_url` vs `effective_database_url`.
  `get_db_session` (:182) / `get_db_session_context` (:196) use the global
  engine → legacy DB, `public` schema, no translate map.
  `get_skrift_db_session_context` (:207) / `get_skrift_db_session` (:229) →
  main DB with `schema_translate_map={None: "skrift"}`.
- Consumers of the legacy accessors (grep `get_db_session` outside tests):
  `web/api/dependencies.py:38`, `web/security_logger.py:83`,
  `bot/attachment_filter.py:55`, `bot/audit_logger.py:106,141` (it uses
  the skrift context for other tables at :215,:280,:346), plus whatever crud
  call-sites receive those sessions. `init_database`/`DatabaseManager`
  (:246, :305) also build legacy engines.
- `alembic/legacy/env.py:24 LEGACY_TABLES` — 24 tables (list below).
- `alembic/main/env.py:25 MAIN_TABLES` — 34 tables, **stale**: models in
  `smarter_dev/web/models.py` not present in either frozenset:
  `sudo_memberships` (models.py:157), `sudo_membership_reminders` (:264),
  `webhook_events_processed` (:303), `channel_handlers` (:4485),
  `handler_runs` (:4529), `admin_handlers` (:4584), `member_activity` (:4629).
  These live in the main DB (created by earlier main revisions or Skrift
  steps) — confirm each exists in `alembic/main/versions/` history and add to
  `MAIN_TABLES`; any that autogenerate would try to re-create must be
  reconciled first.
- The 24 legacy tables (single shared `Base.metadata`, all models in
  `smarter_dev/web/models.py`): `bytes_balances, bytes_transactions,
  bytes_configs, squads, squad_memberships, squad_sale_events, api_keys,
  security_logs, help_conversations, forum_agents, forum_agent_responses,
  forum_notification_topics, forum_user_subscriptions, campaigns, challenges,
  challenge_inputs, challenge_submissions, scheduled_messages,
  repeating_messages, audit_log_configs, advent_of_code_configs,
  advent_of_code_threads, attachment_filter_configs, channel_model_overrides`.
- Naming collision: legacy `public.api_keys` vs Skrift's `skrift.api_keys`
  (different shapes). **Do not migrate legacy `api_keys` into the skrift
  schema** — after phase 01 the Skrift table is authoritative. Same decision
  for `security_logs`: it is only written by `security_logger.py` and read by
  nothing user-facing (verify with grep before deciding); migrate the table
  (renamed problem does not exist — there is no skrift `security_logs`) but do
  not copy legacy `api_keys` rows.
- Tests: `tests/conftest.py` uses `Base.metadata.create_all` on a single test
  engine (lines 102/128/154) — already single-DB, low fallout expected.
- Harness note (06): point `LEGACY_DATABASE_URL` at the main DB in
  `scripts/local_harness/config.py` once flipped; move legacy seeds to the
  main DB.

## Step 1 — Fix the frozensets and move ownership

1. `alembic/main/env.py`: add the 7 stale tables + the 23 migrated legacy
   tables (all of the 24 except `api_keys`, which must NOT be listed — the
   name belongs to Skrift's own table in that schema and is owned by Skrift
   core migrations) to `MAIN_TABLES`.
2. `alembic/legacy/env.py`: leave `LEGACY_TABLES` as-is (the tree stops
   receiving new revisions; it is deleted in phase 05, not edited).
3. Guard test: a unit test asserting
   `MAIN_TABLES ∪ {skrift-owned names} ⊇ {t.name for t in Base.metadata.tables}`
   so the frozenset can never silently go stale again (put it in
   `tests/test_migration_ownership.py`; skrift-owned = names present in
   Skrift's own metadata, importable from `skrift.db.models`).

## Step 2 — One alembic/main revision creating the tables

`uv run alembic -c alembic/main/alembic.ini revision --autogenerate -m
"adopt legacy bot tables into skrift schema"` after step 1, with
`DATABASE_URL` pointed at a **local** DB that has current main head applied.
Autogenerate will emit creates for the 23 adopted tables (env.py's
`include_object` now admits them; the connection's
`SET search_path TO skrift, public` at env.py:87 puts them in `skrift`).
Review by hand:

- Every `create_table` matches the model (JSON columns, server defaults,
  `NAMING_CONVENTION` constraint names from `database.py:71`).
- No ops for the 7 stale tables (they already exist in prod's skrift schema;
  if autogenerate emits creates for them locally-but-not-prod, reconcile local
  state instead of editing prod expectations — the revision must be a no-op
  for tables prod already has, so split: stale-table adoption must produce
  **zero** ops once they're in `MAIN_TABLES`, because they exist at head).
- Downgrade drops exactly the 23 tables.

CI already runs the legacy tree on deploy; the new revision rides the existing
`--only main` step in `k8s/migrate-job.yaml` — no manifest change needed yet.

## Step 3 — `scripts/copy_legacy_data.py`

New script (HUMAN-executed at deploy time; never run by agents against real
DBs). Requirements:

- Reads `LEGACY_DATABASE_URL` (source, `public` schema) and `DATABASE_URL`
  (target, `skrift` schema via `schema_translate_map={None: "skrift"}`),
  both through `smarter_dev.shared.config.get_settings()`.
- `--dry-run` (default **on**; require `--execute` to write) prints per-table
  source/target row counts and the planned order.
- FK-ordered copy list (parents first):
  1. `bytes_configs`, `bytes_balances`, `bytes_transactions`
  2. `squads`, `squad_memberships`, `squad_sale_events`
  3. `campaigns`, `challenges`, `challenge_inputs`, `challenge_submissions`,
     `scheduled_messages`
  4. `forum_agents`, `forum_agent_responses`, `forum_notification_topics`,
     `forum_user_subscriptions`
  5. `help_conversations`, `repeating_messages`, `audit_log_configs`,
     `advent_of_code_configs`, `advent_of_code_threads`,
     `attachment_filter_configs`, `channel_model_overrides`, `security_logs`
  Derive the order programmatically from `Base.metadata.sorted_tables`
  filtered to the adopted set instead of hardcoding, then assert it matches an
  explicit expected list in a test.
- Idempotent: `INSERT ... ON CONFLICT (pk) DO NOTHING` batched (e.g. 1000
  rows), so a partial run can be re-run.
- Verification: after copy, compare `count(*)` per table and fail non-zero on
  mismatch; also spot-check max(created_at) parity.
- Excludes `api_keys` (phase 01 decision) — assert it is not in the copy set.
- Tests: run the script against two local podman postgres databases (reuse
  harness `infra.py` container helpers) seeded with representative rows,
  assert dry-run writes nothing, execute copies all, re-run is a no-op.

## Step 4 — Flip the session accessors (kill `use_legacy_db`)

`smarter_dev/shared/database.py`:

- Delete the `use_legacy_db` parameter (:106) and the URL branch (:115) —
  `create_engine(settings)` always uses `effective_database_url`.
- Make the **global** engine/sessionmaker (`get_engine`/`get_session_maker`,
  :159-:179) apply `execution_options(schema_translate_map={None: "skrift"})`
  so `get_db_session` (:182) and `get_db_session_context` (:196) now hit the
  main DB, skrift schema — identical behavior to what Litestar injects.
- Collapse duplicates: `get_skrift_db_session_context` (:207) and
  `get_skrift_db_session` (:229) become thin aliases of the primary accessors
  (keep the names for now; delete them in a follow-up sweep or in phase 05,
  updating `bot/audit_logger.py`, `bot/plugins/*`, `agents/mod_tools.py`,
  `web/api/routers/polar_webhooks.py`, `sudo_converge.py` imports).
- `init_database` (:246), `DatabaseManager` (:305), `create_tables`/
  `drop_tables` follow automatically once `create_engine` loses the flag.
- `effective_legacy_database_url` / `legacy_database_url` stay in config until
  phase 05 (still consumed by `scripts/migrate.py` legacy steps and the copy
  script), but nothing in app runtime may reference them after this step —
  add a test that greps/imports: `smarter_dev/` (excluding `scripts/`,
  `alembic/legacy/`) has no reference to `use_legacy_db` or
  `effective_legacy_database_url`.

Fallout to fix in the same change:

- `smarter_dev/web/api/dependencies.py:29 get_database_session` now serves
  main-DB sessions — the legacy-key fallback lookup (phase 01) must open its
  own legacy-DB session explicitly **or** (simpler, preferred) phase-01
  fallback removal happens before this flip in prod sequencing; in code, keep
  the fallback but give it a dedicated legacy engine created from
  `effective_legacy_database_url` on demand. Decide at implementation time;
  document the choice in the code.
- `tests/conftest.py` create_all fixtures: verify none set
  `use_legacy_db`; update `create_engine` call sites.
- Harness `config.py`/`seed.py`: legacy seeds move to the main DB
  (`skrift_schema=True` path), `LEGACY_DATABASE_URL` env still set (migrate.py
  needs it until 05) but pointing at bc_websites container only for the legacy
  alembic step; `_create_legacy_leftover_tables` (seed.py:291) and the
  legacy-admin checks keep working against `/bot-admin` until phase 03 —
  **note**: after the flip, `/bot-admin` views read the main DB too, so seeds
  must land there.

## Step 5 — Deploy runbook (HUMAN) — write to `runbooks/02-db-cutover.md`

1. Final pre-flight: harness green on the flip build; take a bc_websites
   snapshot/backup.
2. **Pause writes**: scale bot to 0 (`kubectl -n smarter-dev scale deploy
   smarter-dev-bot --replicas=0`); note the legacy admin also writes — do the
   cutover in a quiet window.
3. Run the migrate job for the new image (`--only main --only legacy`
   unchanged) so the 23 tables exist in `skrift`.
4. Run `uv run python scripts/copy_legacy_data.py --execute` from a pod with
   both URLs (the migrate job's env has both; run it as a one-off Job or
   `kubectl exec`). Confirm row-count verification passes.
5. Deploy web/worker/bot images with the flipped accessors; scale bot back up.
6. Verify: `/api/auth/validate`, a bytes balance read, one `/bot-admin` page,
   one Skrift admin page; watch error logs.
7. Make bc_websites read-only (`REVOKE`/`ALTER ... SET default_transaction_read_only`
   at the role level) — it is now the rollback artifact. Do NOT drop (phase 05).
8. Rollback path: redeploy previous images (they read LEGACY_DATABASE_URL,
   still intact); re-copy any delta later if writes occurred on the main DB.

## Definition of done

- One runtime database; `use_legacy_db` gone; `get_db_session*` =
  main DB + skrift translate.
- New alembic/main revision merged; `MAIN_TABLES` complete + guard test.
- `scripts/copy_legacy_data.py` merged with tests; runbook written.
- Harness + pytest green with main-DB seeds.
