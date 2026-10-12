# Identity writer rollout candidate (#143)

This is a **review candidate, not live authorization**. No secrets, Kubernetes,
remote SQL, D1, payment or deployment actions were performed in preparing it.
Migration lead owns merge/release; the operator owns the separately approved
window. Keep site PR19 and the explicit `site-identity` service scope off until
this procedure finishes. The prior Python approval at `74699f0` does not cover
this added rollout delta.

## Controls and impact

The primary candidate is **worker-first**. Use the existing
`smarter-dev-agent-worker` Deployment scale control, preserving queue rows,
dispatches, claim tokens and recoverable in-flight work. There is no new queue
pause API, queue schema or receipt lock in the implementation.

Only `chat/jobs.py::delete_chat_account` purges the User and anonymizes its
receipt. `account_controller.py::delete_account` and
`api_native/site_privacy.py::delete_account` construct receipts and enqueue
`chat.account.delete`; they do not purge the account. `chat/dispatch.py` repairs
existing dispatch state, not receipt creation or UUID anonymization. The child
worker consumes `chat-subagents`, not `agents`. Reconfirm the live writer inventory
(no ad hoc Jobs, alternate agents consumers, direct administrative deletion) at
execution; stop if it differs.

Stopping agents delays **all root agents work**: chat turns, Resources, web
searches, blogging/LLM work and account purge. Web pages/API, bot and child workers
remain up; old web pods can deactivate accounts and append durable receipts while
the consumer is stopped. Root jobs that await children may be interrupted; retries
retain their queue identities and existing application idempotency. This is not
a promise that arbitrary external tool side effects are exactly once. Review
in-flight root-job types and existing retry limits before approving the window.

Plan a **45-minute root-job delay budget**, not a timing guarantee: up to 30 minutes
for the effective claim lease, 5 minutes for the migration Job, and web/worker
startup plus capture/reconciliation. Set a separate operator stop time before
starting. Unexpected backlog, external calls, schema locks, snapshot size or
insufficient capacity can exceed it; abort without releasing the hold. Web
readiness already allows up to four minutes of startup per pod. After new web
pods start, account deletion and identity routes return private/no-store 503 until
release; normal pages, billing and other API traffic continue.

The effective queue lease is **max(1800 seconds global, 600 seconds handler) =
1800 seconds**. Do not substitute the handler's 600 seconds, the Job TTL or a
blind sleep for verified old-pod and actual claim clearance. Kubernetes termination
and the runtime's bounded graceful drain can cancel jobs; PostgreSQL rolls back
uncommitted transactions and normal queue reclamation later recovers expired
claims. Never manually acknowledge, delete or clear their tokens.

## Stage before the approved window

1. Have the reviewed immutable image available, the exact manifests/scripts,
   enough surge capacity, database connectivity, operator access, and a tested
   restoration plan. Record original replicas (normally web=2, agents=1), pod
   UIDs/images, Deployment generations, queue/DLQ counts and in-flight claims.
   Freeze concurrent deployments, scalers, pod-template edits and Secret updates
   through release. Kubernetes reads across different objects are not an atomic
   transaction; the release checks require this exclusive operator window.
2. Provision one independent high-entropy 32+ character fence key in the existing
   restricted secret manager as `smarter-dev-secrets/site-identity-fence-key`.
   The web, agents worker and migration Job reference this **same required field**.
   Do not print it or put it in a ConfigMap, command argument or repository.
   Other workers/bot/cron do not receive it. Stage before any fence-aware writer.
3. Keep provisioning scope disabled. Inventory every D1 environment that can
   send privacy traffic. Retain **both pending and processed** deletion intent
   UUIDs (`account_deletion_requests.user_id`); omit email. Processed old 404s can
   represent unknown Python accounts that still require a fence. The Worker
   retains those intent UUIDs; pending requests receiving 503 remain pending.
4. Stage a restricted **durable** capture directory outside repository/build
   context, mode 0700, and a trusted execution environment with the approved
   snapshot script and database access. Container `/tmp` alone is not durable.
   Securely copy and verify captures before destroying that execution environment.
   Raw UUID captures are temporary reconciliation material, not the durable marker;
   remove them under the reviewed operator retention procedure after verified
   fencing. Permanent markers retain only keyed digest, deleted flag and timestamps.
5. Record the plan/review reference and expected window end. Neither a missing
   ConfigMap nor setting `phase=complete` can bootstrap the new verifier. CI fails
   closed without the operator control; only the held migration Job may pin it.

## Capture boundary, stop and reconciliation

Run all example commands only in that separately authorized window. `kubectl`
commands below explicitly select namespace `smarter-dev`. Capture commands run
inside the trusted database environment, using its configured database URL;
never pass database credentials on the command line.

**A — before any pod termination**, run:

```sh
python -m scripts.capture_identity_receipts /restricted/initial-receipts.json
```

This starts a read-only **REPEATABLE READ** transaction. Its first query records
`pg_current_snapshot()` and database time; the following ordered query sees
original UUIDs for purge updates uncommitted at that boundary, even when those
purges commit between the boundary query and the row query. It captures receipt
ID, UUID and whether the UUID is still recoverable; no email, profile, provider,
subscription ID or error text. A completed receipt with no billing retry at this
initial boundary is already anonymized history, never a source UUID for replay.

The file is exclusively created mode0600, flushed/fsynced, parent directory
fsynced and read back before a checksum is reported. Record and independently
verify the durable copy/hash. Failed writes, lost copies, unexpected modes or
unverified captures prohibit termination and release. A partial file is not a
successful capture; retry at a new path and retain the failure evidence.

**B — stop only the agents consumer**, after A is durable:

```sh
kubectl -n smarter-dev scale deployment/smarter-dev-agent-worker --replicas=0
kubectl -n smarter-dev wait --for=delete pod -l app=smarter-dev-agent-worker --timeout=120s
kubectl -n smarter-dev get pods -l app=smarter-dev-agent-worker -o wide
```

Require zero old worker pods, including Terminating pods; a timeout is an abort,
not permission to force-delete or assume they are dead. Verify no other consumer
can claim `agents`. Query the queue using the database clock (read only):

```sql
SELECT count(*) AS unexpired_claims, max(claim_expires_at) AS last_expiry
FROM skrift.worker_queue
WHERE queue = 'agents' AND claim_token IS NOT NULL
  AND (claim_expires_at IS NULL OR claim_expires_at > clock_timestamp());
```

Require zero unexpired claims before marking transition evidence verified.
Preserve rows for the new runtime to reclaim; verify no lease extension after
old-pod exit. Any surprise consumer, newer expiry, missing queue rows or
unexplained DLQ movement blocks progress. Inspect actual job-specific expiry;
other root handlers can have different leases. Do not sleep a fixed1800 and infer
clearance. Do not remove an expired claim manually.

**C — take a post-stop durable capture** at a new path with the same command.
Compare using `scripts.capture_identity_receipts.reconcile_captures(initial,
post_stop)`. A pre-existing recoverable receipt that is now anonymized retains its
**initial original UUID**. New non-complete receipts admitted by old web remain
recoverable and are added. A newly completed/anonymized receipt absent from A,
or any disappeared receipt, is a **transition failure**, not historical loss.
Keep provisioning disabled and do not release; seek an authoritative original
UUID source and independent reconciliation review. Do not silently retry using
the newer snapshot as the new historical baseline.

The worker does not create receipts, so once all consumers are stopped, old web
admissions remain durable and cannot be newly anonymized. Preserve initial A
through the whole rollout and compare the final capture against it as well.
This detects and blocks a purge that raced between initial capture and worker
termination; it does not claim such a race cannot happen.

## Migration, pin and held writers

After approved evidence, durable captures and actual claim clearance, create the
operator-owned ConfigMap. It is intentionally **not automatically applied** by CI:

```sh
kubectl -n smarter-dev create configmap site-identity-rollout \
  --from-literal=phase=held --from-literal=transition-mode=worker-first \
  --from-literal=transition-evidence=APPROVED_REVIEW_AND_CAPTURE_REFERENCE \
  --from-literal=capture-verified=true --from-literal=reconciliation-verified=false
python scripts/identity_rollout_control.py preflight
```

The reference must name restricted capture/check evidence, not a secret or UUID
list. Do not use placeholder assertions to get past the gate. Preflight verifies
agents replicas0 and no pods; worker-first intentionally allows old web to remain
up. A pre-existing ConfigMap must be inspected rather than blindly overwritten.

The workflow runs preflight and then `identity_rollout_control.py stamp` reads
only the Secret's `metadata.resourceVersion` via a kubectl JSONPath projection.
It treats that as an **opaque equality token**, never a sortable number or a
timestamp. It renders `smarter.dev/identity-key-version` into all three pod
templates (migration Job, web, agents) **before any of their containers are
created**. This binds each startup pin check to that staged key generation. The
annotation is not a key or verifier digest. Do not patch existing pod annotations
to manufacture freshness; a new generation requires a reviewed fresh rollout.

The trusted Job independently rejects unexpired
or invalid agents claims using the database clock before **core migration → app
migration → key pin/check**. Any failed command, missing/short key, invalid phase
or mismatched verifier fails the Job and stops all subsequent writer deployment.
A phase `complete` Job may check an existing pin but cannot initialize one.

Each new writer checks the existing verifier using its own runtime environment
before starting. The agents wrapper becomes Ready after the key check but **does
not start the queue consumer while held**. New web starts through an ASGI gate
that refuses `/account/delete`, `/api/site/privacy/delete-account/*`, and
`/api/site/identities/*` while held/missing/invalid. It is host independent and
therefore also covers beginner.codes, NodePort30822, ClusterIP and direct pod
requests **on new pods**. There is no claim that it gates old pods. During web
rolling replacement, old pods can still enqueue receipts; workers stay held.

Normal rollout waits for all new web/worker pods. Independently verify no old
web pod or active old HTTP transaction remains, no old agents pod can run, exact
reviewed image and key on every writer. Probe both deletion route families via
smarter.dev, admin.smarter.dev, beginner.codes, NodePort and in-cluster Service;
new-process responses must be503/private-no-store. Also verify session, billing
and normal API behavior. Scope remains off.

## Final reconciliation and explicit release

With all new web writers held and no old web remaining, take the **final** durable
Python capture, reconcile it against initial A, and fence all recoverable original
UUIDs. Recapture/reconcile **all D1 pending AND processed** UUID intents at this
boundary, including old unknown-account404s. Prove every old in-flight privacy
request is finished; if another D1 environment/caller is unaccounted for, stop.
New D1 intents while all Python routes are held remain pending503 and will be
fenced by the new privacy endpoint after release. Do not mark them processed on
transport, redirect, 202 or 503 failures.

While HTTP admission is held, use the trusted Python service in an operator
transaction to fence the reconciled UUIDs (no account creation/deletion, Polar or
roles). For each canonical UUID, call `fence_account_deletion(session, UUID(...))`
and commit using `get_session_maker`; first call
`pin_identity_key()` with its default **check-only** behavior. Verify every keyed
row has `deleted=True` using `fence_digest(uuid)` in the same trusted environment.
Do not print UUIDs or digests; record counts/hash references. Replay is idempotent
and a failed/partial replay must be retried and fully verified before release.
Use only the reviewed reconciled captures/D1 UUID list, never random/anonymized
receipt UUIDs. This is the same fence path as privacy, including absent accounts.

For a concrete trusted replay, normalize the restricted D1 export to a JSON
array of canonical UUID strings at `/restricted/d1-deletion-uuids.json`. Export
`user_id` from **all** rows, without a status filter or email. After verifying the
recorded checksums and coverage, run this with the approved image and its stable
key/database environment (it is intentionally not run by CI):

```python
import asyncio
import json
from pathlib import Path
from uuid import UUID
from scripts.capture_identity_receipts import reconcile_captures
from scripts.migrate_and_pin_identity import pin_identity_key
from smarter_dev.shared.database import get_session_maker
from smarter_dev.web.site_identity import fence_account_deletion, fence_digest
from smarter_dev.web.site_identity_models import SiteIdentityFence

initial = json.loads(Path("/restricted/initial-receipts.json").read_text())
final = json.loads(Path("/restricted/final-receipts.json").read_text())
values = set(reconcile_captures(initial, final))
values.update(json.loads(Path("/restricted/d1-deletion-uuids.json").read_text()))
assert all(isinstance(value, str) and str(UUID(value)) == value for value in values)

async def replay():
    await pin_identity_key()  # check existing pin; never initialize here
    async with get_session_maker()() as session:
        for value in sorted(values):
            await fence_account_deletion(session, UUID(value))
        await session.commit()
    async with get_session_maker()() as session:
        for value in values:
            row = await session.get(SiteIdentityFence, fence_digest(UUID(value)))
            assert row is not None and row.deleted
    print("Reconciliation verified; count:", len(values))

asyncio.run(replay())
```

Fencing is proof against stale provisioning, not proof that a pending purge has
finished. Preserve D1 pending intents and Python receipts for the fenced worker
to process after release. A D1 **processed** intent whose User still exists is an
unresolved deletion: stop and reconcile it through the reviewed privacy workflow;
do not treat the marker alone as completion or silently change its status.

Record historical anonymized-at-A receipts separately. They cannot be recovered
by these changes; enabling identity needs the operator/lead's explicit disposition
of that inventory, not an assumption based on the earlier zero-count preflight.
An A→final coverage gap remains a transition failure and cannot be waived as old
history. Any unaccounted UUID, capture error or replay/check failure keeps the
hold and scope off.

After all of that evidence is reviewed, update only the verification field and
perform the explicit guarded release:

```sh
kubectl -n smarter-dev patch configmap site-identity-rollout --type=merge \
  -p '{"data":{"reconciliation-verified":"true"}}'
python scripts/identity_rollout_control.py release \
  --image zzmmrmn/smarter-dev-website:EXACT_APPROVED_IMAGE_TAG
```

Release checks desired replicas, observed generations, exactly the expected
number of non-terminating Ready pods, exact image/startup wrapper/effective
readiness probe/key secretRef and matching observed pod template. It requires
current running containerID, startedAt and restartCount, with the Ready transition
at or after that container start. The worker clears its marker before every
startup check; web cannot serve its health endpoint until its startup check has
passed. Stale readiness from an earlier container cannot count as current proof.

Release compares each template/pod's staged Secret-version token with the current
metadata-only Secret projection, then reads pod/container evidence and the token
again before conditional release. A restarted/replaced/unready/mismatched writer
or any changed token fails closed. Even an unrelated field update on the shared
Secret conservatively requires review and a new stamped rollout; the operator
detects it by **inequality**, without reading the key or comparing timestamps.
The migration Job supplies separate database pin proof; release launches **no
kubectl exec, Python interpreter or DB session in serving web/agents pods**. There
is no live-pod memory/headroom experiment. If more DB proof is needed, prepare a
separately bounded verifier Job, not an in-pod process.

It rechecks the control and uses JSON Patch test before changing phase to
complete; failure leaves it held. Maintain the exclusive deployment/scaler/Secret
freeze through this last step; a resourceVersion is not a cross-object lock. Projected ConfigMaps
propagate asynchronously: verify every pod sees complete and the consumer starts
before declaring restored. Check queue recovery/DLQ and original replica counts.
Do not force success by editing readiness markers or invoking worker CLI directly.
Only then grant explicit identity scope and perform the separately reviewed site
D1 migration/Worker deployment. Billing origins/global SITE_BASE_URL stay intact.

The phase gate is an **initial startup/release control**, not a runtime queue
pause: an already exec'd consumer does not stop if the ConfigMap returns to held.
To interrupt it use the supported scale control and repeat safe drain verification.

## Abort, recovery and alternatives

- Before initial capture is durable: do not stop anything. Keep scope disabled.
- Before pin or new writers start: a failed window may restore the prior agents
  replica count only under operator direction with identity scope still off;
  retain captures and re-run the whole capture/drain review before another attempt.
  It does not erase or excuse an observed transition gap.
- After pin/new writer start: retain the dedicated key, verifier and fences;
  restore only the same or an approved **fence-aware** image. Never reset verifier,
  downgrade tables, revert to unfenced deletion writers, or release on failed
  rollout/readiness. Fix forward while held; ordinary web can remain available.
- Missing or mismatched key on any writer stops startup/operation. One stable key
  must reach every replica **before code requiring it starts**. Never couple it
  to rotated API/session keys. Replacing it without an old-key/migration strategy
  invalidates historical markers; the verifier rejects it. Back up key and fences
  together with restricted access.
- The rejected SHARE-lock candidate blocks receipt INSERT, UPDATE **and DELETE**,
  including both web admissions and purge/receipt writes, and waits behind prior
  write transactions. It cannot recover a purge that commits before acquisition;
  lock timeout/loss may release waiting writers. Tests demonstrate these limits.
  No production table lock or queue-internal mutation is required here.
- **Unselected full-stop fallback:** if the live writer audit invalidates
  worker-first, stop and obtain review. Scaling web0 as well would remove **all
  Python HTTP/API service** across every host, NodePort and in-cluster caller,
  including login, internal bot API and billing. Stage everything first; use the
  same captures/claims/reconciliation, `transition-mode=full-stop`, all old pods
  gone, and the same45-minute planning budget/abort deadline. This is broader
  interruption, not implicitly authorized by this task.
- **Lower-impact compatibility gate alternative:** a separately reviewed hold-only
  image based on the deployed Python version can wrap both delete entrypoints
  at the application boundary (all hosts/direct paths) before identity changes.
  It needs no fence tables/key, must preserve sessions/other routes, must drain
  every old pod and verify direct-path503 before relying on closure. It still
  needs pre-stop capture/worker drain/reconciliation. No such extra release,
  proxy or custom queue control is added by this candidate.

## Reproducible local evidence

Run only against a disposable loopback PostgreSQL, never a service database:

```sh
docker run --rm -d --name identity-rollout-pg-test \
  -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1::5432 postgres:16-alpine
docker port identity-rollout-pg-test 5432
# Wait for pg_isready; replace PORT with the reported loopback port.
SITE_IDENTITY_TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:PORT/postgres \
  .venv/bin/pytest -q -o addopts='' tests/web/identity_rollout_postgres_test.py
.venv/bin/pytest -q -o addopts='' tests/test_identity_rollout.py
docker stop identity-rollout-pg-test
```

The PostgreSQL tests exercise actual MVCC, locks, backend termination/rollback
and commit-during-capture. Kubernetes behavior is tested with a fake CLI boundary;
no claim of live rollout verification is made. Original approved identity tests
and their recorded fixture/baseline failures remain separate from this delta.
