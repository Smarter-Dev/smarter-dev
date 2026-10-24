# Site code runs

The site Worker's chat model has smarter.dev's `run_code` tool: a short reason
and a Python snippet, run in the Monty sandbox (10 s, 256 MB, no network, no
files, no packages). The Worker never runs code. It posts the call here and
hands the model the text that comes back, which is exactly what smarter.dev's
own chat `run_code` returns for the same call (`smarter_dev/web/chat/toolsets.py`).

```
POST /api/site/code/users/{user_id}/runs
{"reason": "Adding up the invoice", "code": "sum([12.5, 7.25])"}

200 {"result": "return value: 19.75"}
```

The route needs the site service key (`principal_type="service"`,
`service_name="site"`) with an explicit **site-code** scope, and the owner's
`site-service` role must grant `site-code` too. The member is the synced site
user named in the path; an unknown or inactive one is a 404.

| Status | When |
| --- | --- |
| 200 | The run's text, success or not: a syntax error, an exception, a breached limit and the output cap are all text for the model. |
| 400 | The account id isn't one. |
| 422 | The code is over 100,000 characters or the reason over 500. |
| 413 | The body is over 512 KiB. |
| 429 | Two runs are already going for this member, or three in all (`Retry-After: 5`). |
| 429 | Over 600 calls a minute from one address: Skrift's global limiter, rule `site-code` in `app.yaml` (POST under `/api/site/code/users/`, keyed by IP, never by API key). |
| 503 | The run couldn't be admitted (Redis) or queued, or the worker that took it stopped without a result (its heartbeat lapsed). |
| 504 | No result within 45 seconds. |

## Where it runs

The web pod can't host a sandbox: one run can reach about 270 MiB, and the
pod's limit is 384 MiB. The route stores the call in Redis under a random id,
with the moment it stops waiting, queues `site_code.run` on the `agents`
queue with only that id, and waits on a Redis list for the text. The agent
worker takes the call with GETDEL, runs it through `toolsets.run_code` and
pushes the text. Nothing is saved: the call lives in Redis for at most a
minute, the result for at most a minute, and the Postgres queue and job state
never see the code. Neither side logs the code, the reason or the output.

A run the route gives up on never runs, or stops. Whenever the route ends
without a result (504, an error, the request cancelled) it deletes the call
and leaves an abandon mark (no code, gone within a minute). The job checks
the mark and the deadline once it holds a place in the sandbox budget and
before any sandbox starts. While the sandbox runs, the job renews a
two-second heartbeat and stops the sandbox if the mark appears. The route
watches the heartbeat: a job that took the call and stopped answering is a
503 at once rather than a 504 later.

## Memory

Each sandbox is a `monty` process in its pod's cgroup. Every process that
runs them has a budget, `CODE_SANDBOX_BUDGET_MIB`, set per deployment in
`k8s/`. A run reserves its memory cap plus 16 MiB (Monty's own runtime) and
waits, first come first served, until that fits. Its cap is what the caller
asks for, cut to fit the budget, and Monty enforces it inside the worker.
Every entry point counts: chat and site `run_code`, the bot's `run_code` and
handler fires (64 MiB each). The handler linter parses only, at a 16 MiB cap,
and is counted in each pod's margin.

| Pod | Limit | In use (2026-10-24) | Budget | What it holds |
| --- | --- | --- | --- | --- |
| agent-worker | 1Gi | 365Mi | 400 MiB | one 256 MiB chat or site run beside up to a few fires |
| chat-child-worker | 1Gi | 364Mi | 300 MiB | one 256 MiB sub-agent run |
| bot | 612Mi | 305Mi (425Mi under load, 2026-09-25) | 128 MiB | one `run_code`, capped at 112 MiB |
| website | 384Mi | ~352Mi | 32 MiB | nothing but the linter's parse |

Saturating every entry point of each pod at its limit, from that baseline,
peaked at 699.7 MiB (agent-worker), 634.2 MiB (chat-child-worker),
431.6 MiB and, from 425 MiB, 551.2 MiB (bot), and 362.7 MiB (website), with
no OOM. With the budget's wait removed, the agent-worker, chat-child-worker
and bot runs were OOM-killed.

The route's own limits (2 per member, 3 in all) keep queued runs from filling
the worker's job slots; three runs one after another fit the route's
45-second wait.

## Enabling it

Deploy this API first. Until the site key has the scope, the Worker's calls get
401 and its `run_code` tells the model code can't run right now; the reply
still finishes.

Then add **site-code** to the existing site key's scopes in place. The key is
not re-minted and the Worker's `SKRIFT_SITE_KEY` doesn't change. The
`site-service` role grants `site-code` once the deploy syncs roles; the
script refuses until it does. Run it as a one-off Job based on
`k8s/migrate-job.yaml`, with the deployed web image, `smarter-dev-config` and
`smarter-dev-migrate-secrets/database-url`, as the minting runbook in
`admin-host.md` does. Save it as a private `site-code-scope-job.yaml`,
replacing `<IMAGE_VERSION>`:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: smarter-dev-site-code-scope
  namespace: smarter-dev
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: scope
          image: zzmmrmn/smarter-dev-website:<IMAGE_VERSION>
          workingDir: /app
          command: ["/app/.venv/bin/python", "-m", "scripts.grant_site_key_scope", "site-code"]
          envFrom:
            - configMapRef:
                name: smarter-dev-config
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: smarter-dev-migrate-secrets
                  key: database-url
```

Apply it, read `kubectl -n smarter-dev logs job/smarter-dev-site-code-scope`
(the key's prefix and its scopes, never the key), then delete the Job. Running
it again changes nothing. It refuses when there isn't exactly one active site
key, or when the key's owner doesn't hold `site-code` yet.

The site PR that offers `run_code` goes out after this.
