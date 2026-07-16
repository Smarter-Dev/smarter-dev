# Runbook 02 — Database Cutover (bc_websites → main DB skrift schema)

HUMAN steps only. Nothing here is performed automatically by any agent. This
runbook executes the one-time data copy for phase 02
(docs/v2/legacy-sunset/02-db-consolidation.md) and flips production onto a
single database.

> Prerequisites:
> - The alembic/main revision adopting the 23 legacy tables is merged (the
>   `skrift` schema tables exist after the migrate job runs).
> - `scripts/copy_legacy_data.py` is in the deployed image.
> - The local smoke harness is green on the flip build.

What the copy script does: copies 23 of the 24 legacy tables from
`LEGACY_DATABASE_URL` (`public` schema) to `DATABASE_URL` (`skrift` schema),
FK-parents first, preserving primary keys. `api_keys` is **excluded** —
`skrift.api_keys` is Skrift's own key table (phase 01). It is idempotent
(`INSERT ... ON CONFLICT DO NOTHING`): an interrupted run is simply re-run.
It is dry-run by default and verifies per-table `count(*)` and
`max(created_at)` parity after `--execute`, exiting non-zero on any mismatch.

## 1. Pre-flight

- Confirm the harness passed on the exact image about to be deployed.
- Take a bc_websites snapshot/backup (managed-DB snapshot or `pg_dump`).
  This is the rollback artifact for the whole phase.

## 2. Pause writes

The bot and the legacy `/bot-admin` UI both write the legacy DB. Do the
cutover in a quiet window.

```bash
kubectl -n smarter-dev scale deploy smarter-dev-bot --replicas=0
```

Announce a short admin freeze — nobody uses `/bot-admin` until step 6.

## 3. Ensure target tables exist

Run the migrate job for the new image (command unchanged:
`scripts/migrate.py --only main --only legacy`, see `k8s/migrate-job.yaml`).
The new alembic/main revision creates the 23 adopted tables in `skrift`.

## 4. Dry-run, then copy

From a pod that has both `DATABASE_URL` and `LEGACY_DATABASE_URL` (the
migrate job's env has both — run a one-off Job with the same spec, or
`kubectl exec` into it before it completes; a web pod also works if it has
`LEGACY_DATABASE_URL`):

```bash
# Read-only: prints the planned order and per-table source/target counts.
PYTHONPATH=/app uv run --no-sync python scripts/copy_legacy_data.py

# The real copy — verifies count(*) and max(created_at) per table.
PYTHONPATH=/app uv run --no-sync python scripts/copy_legacy_data.py --execute
```

- Exit code 0 + the printed report showing `after == source` for every table
  means verification passed.
- Non-zero exit: read the `VERIFICATION FAILED` line. If the run was merely
  interrupted, re-run `--execute` (idempotent). If the target has *more* rows
  than the source, something wrote to the main DB during the window — find
  and stop it, reconcile the extra rows, re-run.

## 5. Deploy the flip build

Deploy the web/worker/bot images whose session accessors point at the main DB
(the build that removed `use_legacy_db`), then resume the bot:

```bash
kubectl -n smarter-dev rollout status deploy smarter-dev-website
kubectl -n smarter-dev rollout status deploy smarter-dev-agent-worker
kubectl -n smarter-dev scale deploy smarter-dev-bot --replicas=1
kubectl -n smarter-dev rollout status deploy smarter-dev-bot
```

## 6. Verify in prod

- `GET /api/auth/validate` returns 200 for the bot key.
- A bytes read works end-to-end (e.g. `/bytes balance` in Discord, or
  `GET /api/guilds/<guild>/bytes/balance/<user>`).
- One legacy admin page (`/bot-admin/...`) renders with real data — it now
  reads the main DB.
- One Skrift admin page (`/admin/...`) renders.
- Watch web + bot error logs for a few minutes; any relation-does-not-exist
  or FK errors mean a table was missed — rollback (step 8) and investigate.

## 7. Make bc_websites read-only

bc_websites is now the frozen rollback artifact. Do NOT drop it (phase 05).
As an admin on the legacy DB:

```sql
ALTER DATABASE bc_websites SET default_transaction_read_only = on;
-- and/or, per app role:
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public
  FROM <legacy_app_role>;
```

## 8. Rollback path

- Redeploy the previous images (they read `LEGACY_DATABASE_URL`, which is
  untouched). Undo step 7's read-only setting first:
  `ALTER DATABASE bc_websites SET default_transaction_read_only = off;`
- Any rows written to the main DB's copied tables between step 5 and the
  rollback are NOT in bc_websites — diff by `created_at`/`updated_at` newer
  than the cutover timestamp and re-copy the delta manually if needed.
- The copied rows in the `skrift` schema can stay; a later re-cutover just
  re-runs `--execute` (idempotent; note it never overwrites existing target
  rows — if legacy rows were *updated* during a rollback window, delete the
  affected target rows first so the re-copy reinserts them fresh).
