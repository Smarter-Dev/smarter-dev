# Legacy Sunset — Final Report

Branch: `refactor/skrift-admin-migration` (32 commits on top of `origin/main`).
Date: 2026-07-17.

The legacy Starlette `/bot-admin` app, the legacy FastAPI `/api` mount, the
legacy `sk-` API-key stack, the second database (`bc_websites` /
`LEGACY_DATABASE_URL`), and the `alembic/legacy` migration tree are all
removed from the codebase. Everything now runs as Skrift-native Litestar
controllers against the single main DB (`skrift` schema), authenticated by
Skrift-native `sk_` service keys and Skrift admin sessions.

## 1. Test results

Full suite (`uv run pytest -q`, redis test container on localhost:6379):

```
1655 passed, 13 skipped, 8 deselected, 0 failed (162s)
Coverage 45.51% (gate: 20%)
```

Baseline comparison — the 3 pre-existing failures recorded before the sunset
(`tests/web/test_admin/test_auth.py::TestAdminAuthentication::test_login_page_get`,
`tests/web/test_admin/test_integration.py::TestAdminIntegration::test_authentication_flow`,
`tests/web/test_admin/test_integration.py::TestAdminSecurity::test_login_route_accepts_get_and_post_only`)
no longer exist: they tested the deleted legacy `/bot-admin` auth stack and
were removed with it in phase 3. Net: **0 failures vs 3 at baseline**.

Local e2e smoke harness (`uv run python scripts/local_harness/run.py`,
podman postgres + redis, single-DB layout): **115/115 checks passed**,
including `/bot-admin/* -> 404` assertions, every ported `/admin/bot` page,
and every bot-consumed native `/api` endpoint with an `sk_` key.

## 2. Change summary

`git diff --shortstat origin/main..HEAD`:
**295 files changed, 30,003 insertions(+), 40,911 deletions(-)** — a net
removal of ~11k lines.

Commits (oldest first), grouped by phase:

**Phase 0 — harness + plans**
- `77e5605` sunset: local e2e smoke harness
- `684b64f` docs: legacy-sunset implementation plans

**Phase 1 — Skrift API keys (dual-verify)**
- `5d8baa3` feat: dual-verify bot API keys — Skrift-native sk_ first, legacy sk- fallback
- `51f4f1d` feat: bot client accepts Skrift sk_ keys; key-rotation runbook
- `7775bb9` feat: harness dual-key expectations; bootstrap mints Skrift bot keys

**Phase 2 — DB consolidation**
- `5ee0b62` feat: adopt legacy bot tables into alembic/main — close the legacy tree
- `ebebf40` feat: one-time legacy data copy script + DB cutover runbook
- `e3b014e` feat: flip session plumbing onto the single main DB (skrift schema)
- `9f699eb` fix(harness): seed quest dates in the quest timezone, not the local date

**Phase 3 — Skrift-native admin (`/admin/bot/...`), legacy `/bot-admin` deleted**
- `e888b22`…`9a1f9dd` skrift admin: guilds, bytes-config, squads, forum-agents,
  campaigns-challenges, scheduled-messages, repeating-messages, guild-configs,
  help-conversations
- `71d24db` sunset(phase 3): remove legacy /bot-admin Starlette admin

**Phase 4 — native Litestar `/api` (FastAPI mount deleted)**
- `3986df8`…`e769bf1` native api: bytes, squads, quests-challenges, messages,
  advent-of-code, forum, overrides-quota, conversations-activity, admin-auth,
  billing-webhooks
- `246214d` sunset(phase 4): switch /api to native Skrift controllers

**Phase 5 — decommission**
- `c321ae9` sunset(phase 5): decommission legacy plumbing
- `41ff27e` docs: update README + harness docstring for the decommissioned layout

Verified end state: `smarter_dev/web/api/` and `smarter_dev/web/admin/` are
gone, `alembic/` contains only `main/`, and `k8s/migrate-job.yaml` runs
`scripts/migrate.py --only main`. No manifest references
`LEGACY_DATABASE_URL` or `admin-username`/`admin-password`.

## 3. Consolidated HUMAN deploy sequence (order matters)

All deploy-time actions are human steps; runbooks live in
`docs/v2/legacy-sunset/runbooks/`.

1. **Deploy the phase-1 dual-verify build** (accepts both `sk_` and `sk-`
   keys; its migrate job also runs legacy revision `3f8d2c5b9a41` dropping
   the `security_logs.api_key_id` FK). Verify legacy auth still works.
   — `runbooks/01-key-rotation.md` §1, `runbooks/01-rotate-bot-key.md` §1.
2. **Mint the Skrift bot service key**: create/pick the `bot@smarter.dev`
   service-owner user, assign the `bot-service` role (`bot-api` +
   `bot-api-admin`, defined in `smarter_dev/web/roles.py`), mint via
   `/admin/api-keys` or `create_api_key(...)` with
   `scoped_permissions=["bot-api", "bot-api-admin"]`, `principal_type="service"`,
   `service_name="discord-bot"`. Record the raw key once.
   — `runbooks/01-rotate-bot-key.md` §2, `runbooks/01-key-rotation.md` §2.
3. **Rotate the `bot-api-key` k8s secret** (patch only that entry of
   `smarter-dev-secrets`), roll the bot deployment, verify the
   `legacy-api-key-auth` log line stops; **soak ≥ 1 week**.
   — `runbooks/01-rotate-bot-key.md` §3–6.
4. **DB cutover** (quiet window): take a bc_websites backup; **pause the bot**
   (`scale deploy smarter-dev-bot --replicas=0`) and freeze admin use; run
   the migrate job so the 23 adopted tables exist in `skrift`; dry-run then
   `scripts/copy_legacy_data.py --execute` (idempotent, verifies per-table
   counts; `api_keys` deliberately excluded); **deploy the flip build**
   (single-DB session plumbing) and resume the bot; verify end-to-end; set
   bc_websites read-only as the frozen rollback artifact.
   — `runbooks/02-db-cutover.md` (post-phase-5 note: the copy script now
   needs `--source-url` since `LEGACY_DATABASE_URL` no longer exists).
5. **Deploy the remainder** (phase 3–5 code: `/bot-admin` removal, native
   `/api`, decommissioned plumbing). Hard gate: the native `/api` accepts
   **only** `sk_` keys, so step 3 must be complete and soaked first.
   `/bot-admin/*` now 404s — update operator bookmarks per the mapping table.
   — `runbooks/03-bot-admin-removal.md`.
6. **Verify + soak ≥ 2 weeks**: no `sk-`-prefixed auth failures in
   `skrift.security_logs`, zero clients in `pg_stat_activity` for
   bc_websites. — `runbooks/05-final-decommission.md` §1.
7. **Final verified backup** of bc_websites (`pg_dump --format=custom`,
   restore-verify into a scratch DB, record storage location).
   — `runbooks/05-final-decommission.md` §2.
8. **Revoke legacy keys**: `UPDATE public.api_keys SET is_active = false;`
   on bc_websites (belt; the verifying code is already deleted).
   — `runbooks/05-final-decommission.md` §3.
9. **Retire secrets**: remove `legacy-database-url` from
   `smarter-dev-secrets` and `smarter-dev-migrate-secrets`; remove the dead
   `admin-username`/`admin-password` keys; optionally `WEB_SESSION_SECRET`
   (see residuals below). Values are never printed or changed.
   — `runbooks/05-final-decommission.md` §4–5.
10. **Drop bc_websites** (irreversible; only after step 7 verifies), then
    delete bc_websites-only DB users and firewall entries.
    — `runbooks/05-final-decommission.md` §6–7.

Rollback paths are in each runbook; after step 10 the only path back is the
step-7 dump.

## 4. Security sweep (full branch diff)

- **gitleaks** (`gitleaks git --log-opts="origin/main..HEAD"`): 2 findings,
  both the literal dummy fixture `sk_abc123def` in
  `tests/web/test_api/test_skrift_key_auth.py` at intermediate commit
  `5d8baa3`. That file was deleted in phase 4 and does not exist at HEAD.
  Not a real secret — false positives; no action.
- **semgrep** (`--config auto` over all 115 changed-and-present Python
  files): 3 findings, **all pre-existing on `origin/main`** (same code,
  shifted line numbers):
  - `alembic/main/env.py:121` `avoid-sqlalchemy-text` — `SET search_path`
    built from the hardcoded `SCHEMA` constant, no user input.
  - `smarter_dev/web/crud.py:2451,3358` `exec-detected` — the pre-existing
    admin-authored script-execution feature, untouched by this branch.
  - **0 new findings introduced by the sunset.**

## 5. Residual risks and intentional deferrals

- **`WEB_SESSION_SECRET` still present**: the setting
  (`smarter_dev/shared/config.py:145`) and the env injection in
  `k8s/deploy.yaml`, `k8s/deploy-worker.yaml`, `k8s/migrate-job.yaml`
  remain, but its only consumer (the legacy admin session stack) is deleted —
  it is dead config. Removal is the conditional step in
  `runbooks/05-final-decommission.md` §5 plus a small manifest/config sweep;
  deferred so the current manifests deploy without a secret change.
- **Legacy `sk-` residue is dead rows, not dead code**: all `sk-` verify and
  format code is deleted; the `public.api_keys` rows stay live until
  runbook 05 §3 flips them off, and remain inside any bc_websites backup —
  hence the revoke-before-backup-retention guidance.
- **Cutover write-loss window**: between the DB flip and any rollback, rows
  written to the main DB are not in bc_websites. `runbooks/02-db-cutover.md`
  §8 documents the delta re-copy (and that updated-then-recopied legacy rows
  must be deleted on the target first, since the copy never overwrites).
- **Copy script now requires `--source-url`**: phase 5 removed
  `LEGACY_DATABASE_URL`, so any re-run of `scripts/copy_legacy_data.py`
  against a restored backup must pass the source explicitly.
- **Baseline failing tests removed, not fixed**: the 3 baseline failures
  covered deleted legacy-admin behavior; their package
  (`tests/web/test_admin/`) is now empty. Equivalent coverage lives in the
  Skrift admin controller tests (`tests/web/test_admin_*.py`) and the
  harness's auth checks (`/admin/` anonymous -> 401).
- **Deploy-time actions not performed**: no key was minted, no secret
  rotated, no data copied, no database touched — all are human runbook steps
  per the safety rules.
- **History retains legacy code**: intermediate commits still contain the
  legacy admin/API (normal git history); the dummy `sk_abc123def` gitleaks
  hits live only there.
