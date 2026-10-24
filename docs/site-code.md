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
| 429 | Two runs are already going for this member, or four in all (`Retry-After: 5`). |
| 429 | Over 600 calls a minute from one address: Skrift's global limiter, rule `site-code` in `app.yaml` (POST under `/api/site/code/users/`, keyed by IP, never by API key). |
| 503 | The run couldn't be queued. |
| 504 | No result within 45 seconds. |

## Where it runs

The web pod can't host a sandbox: one run can reach about 270 MiB, and the
pod's limit is 384 MiB. The route stores the call in Redis under a random id,
queues `site_code.run` on the `agents` queue with only that id, and waits on a
Redis list for the text. The agent worker takes the call with GETDEL, runs it
through `toolsets.run_code` and pushes the text. Nothing is saved: the call
lives in Redis for at most a minute, the result for at most a minute, and the
Postgres queue and job state never see the code. A run the route gave up on is
deleted, so a job that starts late runs nothing. Neither side logs the code,
the reason or the output.

On the agent worker, every member-written snippet (smarter.dev chat, site
chat) shares `code_sandbox.MAX_CONCURRENT_RUNS` = 2 places, so code adds at
most two workers of about 270 MiB to the pod at once. The route's own limits
(2 per member, 4 in all) keep queued runs from filling the worker's job slots.

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
