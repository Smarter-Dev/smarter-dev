# Site identity handoff

`PUT /api/site/identities/{canonical_uuid}` accepts a server-held identity
snapshot from the Worker. It requires a **service** API key named **site**,
explicit **site-identity** scope, and that permission on its owner (`site-service`
role). Cookies, user/bot keys and older unscoped keys cannot provision accounts.
The Worker reads UUID, provider subjects, verification state and revision from D1;
none of these assertions come from a billing form.

```json
{"revision":1,"identities":[{"provider":"discord","subject":"123456789012345678","email":"member@example.test","email_verified":true}],"display_name":"Member"}
```

Revisions are strict JSON integers from 1 through 9007199254740991. Supported
providers are Discord, GitHub and Google; subjects are ASCII decimal strings.
Unknown fields, coercible booleans/strings and duplicate identities are rejected.
Only verified email can populate a **new** Python account. Email is never used to
find, merge, rename or move an existing account. Existing UUIDs require a matching
provider before enrolling provider reconciliation. An existing account with no
asserted providers may be acknowledged without enrolling it. No absent account
can be created without provider evidence.

The UUID is identical in both stores. New OAuth rows have no access/refresh
tokens. Existing Python bindings, profiles and tokens are preserved; only bindings
created by this handoff become owned by it and may subsequently be removed.
Provider ownership or verified-email collisions return 409 atomically. A repeated
revision with the same canonical payload is idempotent, but independently removed
or reassigned bindings fail closed. Older revisions or changed same-revision
payloads return 409. Inactive accounts and fenced UUIDs return 410 and are never
reactivated. There is no role grant, Polar customer creation or checkout in this
operation. Billing and role convergence remain in Python.

Success is 201 for creation or 200 otherwise, with
`{"user_id":"...","revision":1,"state":"ready"}`. Errors preserve 400 (UUID),
401 (principal/scope), 409 (conflict/revision), 410 (disabled), 422 (body), or
503 (configuration/projection failure); identity responses are private/no-store.
The Worker verifies the returned UUID/revision before acknowledging its outbox.

## Deletion, unlink and retry safety

The main migration adds three tables: live revision/provenance state, a global
key verifier, and per-UUID HMAC fences. A fence contains only a keyed digest,
deleted boolean and timestamps, never raw UUID, email, provider or profile.
Live revision state is removed at deletion. The durable pseudonymous marker is
retained indefinitely for the approved purpose of rejecting stale reprovisioning.

Provisioning and deletion insert then lock the same fence row in their database
transaction. PostgreSQL's unique constraint also arbitrates an **absent** UUID:
either creation commits first and deletion fences it, or deletion commits first
and creation receives 410. This is not a process-local mutex. The existing native
account-delete endpoint, site privacy endpoint (including unknown UUIDs), and
purge cleanup all fence before deleting. An unknown UUID's privacy 404 is returned
only **after** its fence commits. A failed transaction does not acknowledge deletion.

Removing a handoff-owned Discord binding calls the existing role-removal helper
in strict mode before committing the binding/revision update. Provider/config
failure returns 503 and rolls back the database transaction. The Worker retains
its pending revision and retries. Partial external removal is safe to repeat;
existing convergence can restore entitled roles if a transaction rolls back.
Imported Python-owned bindings are not unlinked by this API.

## Operator rollout and rollback

No deployment, secret update or remote migration is performed by this change.
The mandatory transition ordering, supported worker-first hold, durable captures,
all-writer checks and explicit release are in
[the #143 rollout runbook](site-identity-rollout.md). The overview below does not
replace that gate or authorize an ordinary rolling upgrade across old writers.

1. Generate one dedicated high-entropy `SITE_IDENTITY_FENCE_KEY` (at least 32
   characters) in the existing secret manager. Configure the **same** stable value
   for **every Python web replica and deletion worker before rolling out code
   requiring it**. It is independent of routinely rotated site API/session keys.
2. Apply the main Alembic migration through the normal reviewed migration process.
   Pin the expected key verifier using the trusted operator environment and the
   new code before admitting identity traffic. This does not store a user UUID:

   ```python
   import asyncio
   from smarter_dev.shared.database import get_session_maker
   from smarter_dev.web.site_identity import verify_identity_key

   async def pin():
       async with get_session_maker()() as session:
           await verify_identity_key(session)
           await session.commit()
   asyncio.run(pin())
   ```

3. Roll out all Python deletion writers/workers with the key and fence support.
   Do not enable provisioning while any old deletion writer can acknowledge an
   unfenced deletion. Drain old processes and in-flight deletion jobs first.
   Replay known D1 account-deletion UUIDs, including previously acknowledged
   unknown-account deletions, through the fenced privacy endpoint before enabling
   the new Worker. Review any historical deletion for which no UUID remains:
   this migration cannot reconstruct identities already anonymized before it.
4. Grant the Worker key explicit `site-identity` scope alongside its existing
   billing/privacy permissions; the service owner needs the matching permission.
   Roll out the site's D1 revision/outbox migration and code only after Python
   is ready. Existing global `SITE_BASE_URL`, billing origins and Polar settings
   are unchanged.

Missing/short keys fail closed: provisioning and deletion cannot proceed and no
privacy 404 acknowledgement is emitted. The first trusted pin persists a
verifier; a different key on any replica returns 503 before fencing or mutation.
A replica with the correct key continues normally. Do not delete/reset the
verifier to bypass a mismatch. Configuring a key is an operator prerequisite,
not a default generated at runtime.

**Do not replace or routinely rotate the fence key.** Replacing it without an
explicit old-key lookup/migration strategy would invalidate existing deletion
markers. The stored verifier rejects this instead of silently recreating them.
Back up the key and fences together under restricted access; losing either
breaks the deletion guarantee. The raw UUID is deliberately unavailable for
rehashing old markers. Key rotation needs a separately reviewed dual-key design.

Schema downgrade refuses to erase the fences. Roll back the Worker first and
revoke/disable identity provisioning if necessary; keep Python's fence-aware
deletion code, key and tables until a reviewed rollback can preserve every
marker. An arbitrary rollback to unfenced deletion writers is unsafe even while
provisioning is temporarily disabled, because those deletions could later replay.

## Local concurrency reproduction

Use a disposable local PostgreSQL database only; the fixture creates and drops
its test tables and refuses non-loopback hosts. These commands do not use service
secrets or production databases:

```sh
docker run --rm -d --name site-identity-pg-test \
  -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1::5432 postgres:16-alpine
docker port site-identity-pg-test 5432
# Replace PORT with the reported local port, after pg_isready succeeds.
SITE_IDENTITY_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:PORT/postgres \
  .venv/bin/pytest -q -o addopts='' tests/web/site_identity_postgres_test.py
docker stop site-identity-pg-test
```

The tests hold one transaction open, assert the competing operation blocks,
commit the first and verify final deletion/replay behavior for both create/delete
orders. Duplicate simultaneous creation must produce one created and one existing
result. The ordinary identity suite also covers provider/UUID/email conflict,
strict bodies, missing/mismatched keys, inactive accounts, out-of-order/replayed
snapshots, owned unlink rollback and deletion before/after creation.
