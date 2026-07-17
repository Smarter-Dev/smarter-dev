# 00 — Legacy Sunset: Overview

> **STATUS: all code phases (01–05) are COMPLETE.** The repo contains no
> legacy admin, legacy FastAPI, legacy keys, legacy alembic tree, or
> `LEGACY_DATABASE_URL` plumbing (enforced by
> `tests/test_single_database_runtime.py`). The only remaining work is the
> human-only runbook `runbooks/05-final-decommission.md` (final backup →
> secret retirement → drop bc_websites).

Goal: retire the two legacy subsystems still mounted inside the Skrift app and
the second database that exists only to serve them.

## What "legacy" means here (verified anchors)

| Piece | Anchor | Fate |
| --- | --- | --- |
| Legacy admin | Starlette app mounted at `/bot-admin` via `smarter_dev/web/controllers.py:228` (`bot_admin_mount`) → `smarter_dev/web/admin/` (`app.py`, `auth.py`, `discord_oauth.py`, `discord.py`, `routes.py`, `views.py` ~4000 lines) + `templates/bot-admin/` | Deleted after per-feature port to Skrift-native `/admin` controllers (03) |
| Legacy bot API | FastAPI mounted at `/api` via `controllers.py:219` (`api_mount`) → `smarter_dev/web/api/` (~21 routers) | Deleted after 1:1 Litestar rewrite under the same paths (04) |
| Legacy API keys | `sk-` keys in legacy `public.api_keys`, verified by `smarter_dev/web/api/dependencies.py:42` via `crud.py:1587 APIKeyOperations` | Replaced by Skrift-native `sk_` keys (`skrift/db/services/api_key_service.py`) (01) |
| Legacy DB | `LEGACY_DATABASE_URL` (bc_websites, `public` schema, 24 tables) reached through `smarter_dev/shared/database.py:106` `create_engine(use_legacy_db=True)` default | Tables moved into the main DB's `skrift` schema; DB dropped by a human at the end (02, 05) |
| Legacy alembic tree | `alembic/legacy/` (version table `alembic_version_legacy`), orchestrated by `scripts/migrate.py` and `k8s/migrate-job.yaml` (`--only main --only legacy`) | Deleted (05) |

## Definition of done

All of the following are gone from the repo and from prod:

1. `/bot-admin` mount, `smarter_dev/web/admin/`, `templates/bot-admin/`, and the
   legacy Discord-OAuth session stack (`admin/auth.py`, `admin/discord_oauth.py`).
2. `smarter_dev/web/api/` and the FastAPI dependency (`fastapi` may leave
   `pyproject.toml` if nothing else imports it).
3. Legacy `public.api_keys` verification path (`crud.APIKeyOperations` key lookup,
   `security.py` `sk-` format functions); bot authenticates with a Skrift `sk_` key.
4. `alembic/legacy/`, the `skrift-legacy`/`legacy` steps in `scripts/migrate.py`,
   and `--only legacy` in `k8s/migrate-job.yaml`.
5. `LEGACY_DATABASE_URL` / `legacy_database_url` / `effective_legacy_database_url`
   (`smarter_dev/shared/config.py:34`, `:342`) and `use_legacy_db`
   (`database.py:106`) plumbing, plus the env var in `compose.yaml`,
   `k8s/deploy.yaml`, `k8s/deploy-bot.yaml`, `k8s/deploy-worker.yaml`,
   `k8s/cron-sudo-sweep.yaml`, `k8s/migrate-job.yaml`.
6. bc_websites database dropped (HUMAN step, final runbook in 05).

## Phase ordering and rationale

```
01 skrift-api-keys   → auth cutover first: everything later assumes the Skrift
                       key path exists; dual-verify keeps the old key working.
02 db-consolidation  → single database before any feature port, so ported admin
                       pages and Litestar API controllers can use the injected
                       Litestar db_session (main DB, skrift schema) directly
                       instead of growing new legacy-DB plumbing.
03 admin-parity      → port /bot-admin features to Skrift /admin controllers,
                       then remove the mount + smarter_dev/web/admin/.
04 api-rewrite       → port FastAPI routers to Litestar controllers (built
                       unregistered, switched over atomically), then remove the
                       /api mount + smarter_dev/web/api/.
05 decommission      → delete alembic/legacy, LEGACY_DATABASE_URL plumbing,
                       manifests/env; final human runbook (backup, revoke legacy
                       keys, drop bc_websites).
```

03 and 04 are independent of each other once 02 lands; do 03 first because it
is lower risk (admin pages are human-facing; the API is bot-facing and gated by
the harness's 117 checks).

## Verification gate (every phase)

`uv run python scripts/local_harness/run.py` (see `06-test-harness.md`) must
exit 0. Any phase that intentionally changes checked behavior updates
`scripts/local_harness/expectations.py` (and `seed.py`/`config.py` if seeds
change) **in the same change**. Full pytest suite must also pass
(`uv run pytest tests/` — chunk into `tests/bot tests/web tests/shared
tests/integration` if slow).

## Rollback strategy per phase

| Phase | Deploy-time rollback |
| --- | --- |
| 01 | Dual-verify means the legacy `sk-` key keeps working; rolling back = keep using the old key, no code revert needed. Revert the bot secret to the old key if the new key misbehaves. |
| 02 | bc_websites is left intact and read-only after the copy. Rollback = redeploy previous image (which still reads LEGACY_DATABASE_URL) — data written to the main DB between deploy and rollback must be re-copied or accepted as lost, so keep the pause window (bot scaled to 0) until verification passes. |
| 03 | Each ported feature lands as an additive `/admin` controller while `/bot-admin` still serves; the mount removal is a single small commit that can be reverted alone. |
| 04 | Ported Litestar controllers are built unregistered; the switchover (register all + remove `api_mount`) is one commit, revertible alone. Same key, same DB either way. |
| 05 | Nothing to roll back in code (pure deletion); the DB drop is a human step taken only after a final backup and a soak period. |

## Doc map

- `01-skrift-api-keys.md` — Skrift-native API keys, dual-verify, bot client relax, key admin.
- `02-db-consolidation.md` — one database, data copy, session-accessor flip.
- `03-admin-parity.md` — per-feature `/bot-admin` → `/admin` port specs + removal.
- `04-api-rewrite.md` — FastAPI → Litestar port units + switchover.
- `05-decommission.md` — deletion of all legacy plumbing + final human runbook.
- `06-test-harness.md` — (already written) the smoke harness gate.
- `runbooks/` — human deploy-time steps only; agents never execute them.
