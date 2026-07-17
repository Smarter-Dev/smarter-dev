# 05 — Decommission: delete the legacy plumbing

> **STATUS: code deletions COMPLETE** (see `runbooks/05-final-decommission.md`
> for the remaining human-only deploy steps). Notes vs. the plan below: the
> copy script now takes `--source-url` instead of `LEGACY_DATABASE_URL`; the
> U9 legacy-key admin endpoints (`/api/admin/stats` + `/api-keys` CRUD) were
> removed along with `crud.APIKeyOperations`, the `APIKey` model, and the
> `sk-` helpers in `security.py`; the dead `legacy:` block in `app.yaml` was
> removed (Skrift only reads subdomain sites from `sites:`, so nothing in
> this deployment ever served legacy.smarter.dev); the dead
> `ADMIN_USERNAME`/`ADMIN_PASSWORD` env plumbing was also swept.

Final phase. Everything here is pure removal; prerequisites are 01–04 deployed
and soaked (recommend ≥ 2 weeks after the 02 cutover before dropping the DB).

## Code deletions

1. **alembic/legacy tree**: delete `alembic/legacy/` (env.py, alembic.ini,
   versions/ incl. `20260504_153337_0c69c7839de7_initial_legacy_schema.py` …
   `20260715_000000_2b7c9e4a1f83_add_channel_model_override_reasoning_level.py`).
2. **scripts/migrate.py**: remove the `skrift-legacy` and `legacy` entries
   from `_STEP_SCRIPTS` (migrate.py:44), the legacy branch in `_step_env`
   (:97-108 — the whole `if step.endswith("-legacy") or step == "legacy"`
   arm and the `LEGACY_DATABASE_URL` copies), the legacy half of
   `_resolve_urls` (:86), and the module docstring's two-database explanation.
3. **k8s/migrate-job.yaml**: command becomes
   `... scripts/migrate.py --only main` (drop `--only legacy`); delete the
   `LEGACY_DATABASE_URL` env block (secret `smarter-dev-migrate-secrets` key
   `legacy-database-url`) and its comment lines.
4. **Settings**: `smarter_dev/shared/config.py` — delete `legacy_database_url`
   field (:34) and `effective_legacy_database_url` property (:342).
5. **database.py**: `use_legacy_db` is already gone (phase 02); now delete the
   `get_skrift_db_session_context`/`get_skrift_db_session` aliases (:207,
   :229) after repointing their importers to the primary accessors:
   `bot/audit_logger.py`, `bot/plugins/mod_monitor.py`,
   `bot/plugins/timeout.py`, `bot/plugins/warn.py`,
   `bot/agents/mod_tools.py` (the phase-04 botapi modules should already use
   the injected session).
6. **Env plumbing** — remove `LEGACY_DATABASE_URL` everywhere (verified list):
   - `compose.yaml:50` (web) and `:74` (bot); also remove the `bc_websites`
     database from `scripts/postgres-init/` init scripts if nothing else uses
     it (keep the main-DB init).
   - `k8s/deploy.yaml:47-51`, `k8s/deploy-bot.yaml:38-42`,
     `k8s/deploy-worker.yaml:47-51`, `k8s/cron-sudo-sweep.yaml:52-56`.
   - `k8s/secrets.template.yaml`: drop the `legacy-database-url` key
     (template only — never touch real secret values).
   - `scripts/bootstrap.py` (:9 docstring, `_provision_bot_key` legacy-URL
     use at :134 — already rewritten in phase 01; delete any residue).
   - `.env.example` / `.env` docs mentions (structure only; do not edit real
     `.env` values beyond removing the now-dead key name from the example).
7. **Harness**: `scripts/local_harness/config.py:41 LEGACY_DATABASE_URL`,
   `run.py` legacy-migration step (:114) and banner (:89), `seed.py` legacy
   plumbing (`_legacy_rows` dual-DB split, `_create_legacy_leftover_tables`
   :291) — collapse to single-DB seeding; drop the second postgres database
   from `infra.py`; delete the seeded legacy `sk-` key and its dual-verify
   checks (keep malformed-key 401 checks against the `sk_` scheme).
   Update `docs/v2/legacy-sunset/06-test-harness.md` to describe the
   single-DB layout.
8. **Leftover code sweep** (grep-driven, delete what's now unreachable):
   `smarter_dev/web/security.py` legacy `sk-` generator/validator
   (`generate_secure_api_key`, `validate_api_key_format` legacy shape),
   `crud.py:1587 APIKeyOperations` (if phase 04 U9 dropped the admin key
   endpoints), the legacy `APIKey`/`SecurityLog` models **only if** nothing
   reads them anymore (security logging port decision from 04), and
   `smarter_dev/bot/services/api_client.py` dual-prefix check tightened to
   `sk_` only.
9. **Docs**: update `docs/CLAUDE.md` architecture diagram + "Web API
   (FastAPI)" / "Admin Interface (Starlette)" references, README/API docs
   mentions of `/api/docs` (FastAPI swagger is gone — note whatever replaces
   it, e.g. Litestar's schema route, or nothing), and mark the
   `docs/v2/legacy-sunset/` plan docs as completed.

## Tests

- Full suite green after each deletion commit (`uv run pytest tests/` in
  chunks if needed).
- Add a tripwire test: importing `smarter_dev` package tree contains no module
  named `smarter_dev.web.api` or `smarter_dev.web.admin`, and
  `grep -r "LEGACY_DATABASE_URL" smarter_dev/ scripts/ k8s/ compose.yaml`
  returns nothing (implement as a small pytest that walks the repo, excluding
  docs/).
- Harness run (single-DB layout) exits 0.

## Final human runbook — write to `runbooks/05-final-decommission.md`

Order matters; each step gates the next:

1. **Soak check**: ≥ 2 weeks on phases 01–04 in prod with no legacy-fallback
   auth events and no reads of bc_websites (check DO database metrics /
   pg_stat_activity for connections to bc_websites; the only expected client
   is the migrate job's legacy step until this phase's manifests deploy).
2. **Final backup**: take and verify a full bc_websites dump
   (`pg_dump` via DO managed-DB backup or manual), store per retention policy.
   Record where.
3. **Revoke legacy keys**: `UPDATE public.api_keys SET is_active = false;` on
   bc_websites (belt) — the verification code is already deleted (suspenders).
4. **Deploy this phase's manifests** (no more LEGACY_DATABASE_URL anywhere);
   confirm pods healthy, migrate job runs `--only main` clean.
5. **Rotate/retire secrets**: remove `legacy-database-url` from
   `smarter-dev-secrets` and `smarter-dev-migrate-secrets`
   (`kubectl -n smarter-dev patch secret ... --type=json
   -p='[{"op":"remove","path":"/data/legacy-database-url"}]'`). If
   `WEB_SESSION_SECRET` proved legacy-admin-only in phase 03, remove it too.
6. **Drop the database**: after backup verification, drop bc_websites in the
   DO control panel (or `DROP DATABASE bc_websites;` from the admin
   connection). This is irreversible except via the step-2 backup. NEVER
   performed by an agent.
7. Delete the DO database user/firewall entries that existed only for
   bc_websites, if any.

## Definition of done

- Repo contains no `LEGACY_DATABASE_URL`, `use_legacy_db`,
  `alembic/legacy`, `smarter_dev/web/api`, `smarter_dev/web/admin`,
  `templates/bot-admin` (tripwire test enforces).
- Prod runs on one database with only Skrift `sk_` keys; bc_websites dropped
  after verified backup.
