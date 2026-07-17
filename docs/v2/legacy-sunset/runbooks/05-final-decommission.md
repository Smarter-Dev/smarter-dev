# Runbook 05 — Final Decommission (backup, revoke, drop bc_websites)

HUMAN steps only. Nothing here is performed automatically by any agent. This
runbook executes the irreversible tail of phase 05
(docs/v2/legacy-sunset/05-decommission.md): the final backup of the legacy
`bc_websites` database, legacy-key revocation, secret retirement, and the
database drop itself.

> Prerequisites:
> - Phases 01–04 deployed and soaked in prod.
> - The phase-05 code is merged: `alembic/legacy` deleted, the migrate job
>   runs `--only main`, no manifest references `LEGACY_DATABASE_URL`, the
>   legacy `sk-` key code and `APIKey` model are gone, and the local smoke
>   harness (115 checks, single-DB layout) is green on the exact image about
>   to be deployed.

**Order matters; each step gates the next.**

## 1. Soak check (≥ 2 weeks on phases 01–04)

Confirm nothing still touches the legacy database or legacy keys:

- No legacy-fallback auth events since the phase-01/04 switchover: in the
  main DB, `SELECT count(*) FROM skrift.security_logs WHERE action =
  'authentication_failed' AND timestamp > now() - interval '14 days';` and
  review any rows — failures with an `sk-` prefix mean some client still has
  a legacy key.
- No reads of bc_websites: check DO managed-database metrics and
  `SELECT * FROM pg_stat_activity WHERE datname = 'bc_websites';` on the
  cluster. After the phase-05 manifests deploy, **zero** clients are
  expected (before that, the only expected client was the migrate job's
  legacy step).

Do not proceed while anything is still connecting.

## 2. Final backup of bc_websites

Take and **verify** a full dump; this is the only artifact that survives
step 6.

```bash
pg_dump "$BC_WEBSITES_ADMIN_URL" \
  --format=custom \
  --file=bc_websites-final-$(date +%Y%m%d).dump

# Verify: restore into a scratch database and spot-check row counts.
createdb bc_websites_verify
pg_restore --dbname=bc_websites_verify bc_websites-final-*.dump
psql bc_websites_verify -c "SELECT count(*) FROM public.bytes_transactions;"
dropdb bc_websites_verify
```

(Or use the DO control panel's on-demand backup if preferred — but still
verify it restores.) Store the dump per the retention policy and **record
where it lives** here: `___________________________`.

## 3. Revoke the remaining legacy keys (belt)

The verification code for `sk-` keys is already deleted (suspenders); flip
the rows off anyway so a restored dump can never contain live credentials:

```sql
-- On bc_websites
UPDATE public.api_keys SET is_active = false;
```

## 4. Deploy the phase-05 manifests

Push/merge deploys via `.github/workflows/deploy.yaml`. Confirm:

- The migrate job runs `scripts/migrate.py --only main` and completes clean.
- All pods healthy (`kubectl get pods -n smarter-dev`); no pod env contains
  `LEGACY_DATABASE_URL` (`kubectl -n smarter-dev describe deploy | grep -i
  legacy` returns nothing).
- Bot functions normally (daily bytes, squads list) and `/api/health` is 200.

## 5. Retire the secrets

Remove the now-unreferenced secret keys (values are never printed):

```bash
kubectl -n smarter-dev patch secret smarter-dev-secrets --type=json \
  -p='[{"op":"remove","path":"/data/legacy-database-url"}]'
kubectl -n smarter-dev patch secret smarter-dev-migrate-secrets --type=json \
  -p='[{"op":"remove","path":"/data/legacy-database-url"}]'
```

If runbook 03's deferred check concluded `WEB_SESSION_SECRET` was
legacy-admin-only and nothing else reads it, remove that key the same way;
otherwise leave it.

The `admin-username` / `admin-password` keys are also dead (they fed the
deleted Starlette admin; phase 05 removed the env plumbing from
`k8s/deploy.yaml`) — remove them from `smarter-dev-secrets` the same way.

## 6. Drop the database (irreversible)

Only after the step-2 backup is verified and steps 1–5 are complete:

- DO control panel → the managed Postgres cluster → Databases → delete
  `bc_websites`; **or** from the admin connection:

```sql
DROP DATABASE bc_websites;
```

This is irreversible except via the step-2 backup. **NEVER performed by an
agent.**

## 7. Clean up cluster leftovers

- Delete any DO database **user** that existed only for bc_websites.
- Remove any DB **firewall/trusted-source entries** that existed only for
  bc_websites clients.

## Rollback

- Steps 1–5 are reversible: re-add the secret keys and redeploy an older
  image tag (pre-phase-05 images still read `LEGACY_DATABASE_URL`).
- After step 6 the only path back is restoring the step-2 dump into a new
  database — which is why step 2 verification gates everything.
