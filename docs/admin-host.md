# Python app on admin.smarter.dev

The Python web app, bot API and workers remain behind the `smarter-dev-website`
service. During the transition, its ingress accepts both `smarter.dev` and
`admin.smarter.dev`; the public hostname is removed by the separate cutover
task. No DNS or Cloudflare Worker change is part of this deploy.

## Before switching public traffic

1. Point `admin.smarter.dev` at the existing ingress. Verify cert-manager has
   issued `smarter-dev-website-cert` with **both** hostnames in its SANs; the
   ingress TLS entry and rules must contain both hosts until cutover. Confirm
   HTTPS to the admin host reaches the Python app before changing the public
   hostname.
2. Zech adds the following callback URI to each existing provider OAuth app,
   keeping its `smarter.dev` callback until sessions and rollback no longer
   need it:

   | Provider | Callback URI |
   | --- | --- |
   | Discord | `https://admin.smarter.dev/auth/discord/callback` |
   | GitHub | `https://admin.smarter.dev/auth/github/callback` |
   | Google | `https://admin.smarter.dev/auth/google/callback` |

   The separate bot-admin Discord OAuth flow uses
   `https://admin.smarter.dev/bot-admin/auth/discord/callback`; add that URI
   to its Discord app too if the same app is not used for site sign-in.
   `app.yaml` generates the three site callbacks from
   `auth.redirect_base_url`. `DISCORD_REDIRECT_URI` in `k8s/configmap.yaml`
   controls the bot-admin flow.
3. Check a sign-in on `admin.smarter.dev` for each provider. The session cookie
   stays scoped to `.smarter.dev`, so a login is shared with the public site.

## Site service key

After the site memory API and privacy routes land, create a dedicated Skrift
service user (for example `site@smarter.dev`) and assign it a role granting
`site-memory` and `site-privacy`. The owner's role and key scopes must both
include these permissions, because Skrift intersects them when authorizing a
request. The API also requires `service_name="site"` exactly. Skrift's
`/admin/api-keys` **New Key** form cannot set the principal type or service
name, so mint this key through `create_api_key`, as the bot-key runbook does.

Run a one-off Job based on `k8s/migrate-job.yaml`, using the same deployed web
image, `smarter-dev-config` and `smarter-dev-migrate-secrets/database-url`.
Replace `<IMAGE_VERSION>` with the deployed image tag. The dedicated owner
and its role must already exist; the snippet checks its effective permissions
before it creates a key. Save this as a private `site-key-job.yaml`:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: smarter-dev-site-key
  namespace: smarter-dev
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: mint
          image: zzmmrmn/smarter-dev-website:<IMAGE_VERSION>
          workingDir: /app
          command: ["sh", "-c"]
          args:
            - |
              PYTHONPATH=/app uv run --no-sync python - <<'PY'
              import asyncio
              from sqlalchemy import select
              from sqlalchemy.ext.asyncio import async_sessionmaker
              from skrift.auth.services import get_user_permissions
              from skrift.db.models.user import User
              from skrift.db.services.api_key_service import create_api_key
              from smarter_dev.shared.config import get_settings
              from smarter_dev.shared.database import convert_postgres_url_for_asyncpg, create_async_engine

              async def mint():
                  url = convert_postgres_url_for_asyncpg(get_settings().effective_database_url)
                  engine = create_async_engine(url).execution_options(
                      schema_translate_map={None: "skrift"}
                  )
                  try:
                      async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                          owner = await session.scalar(
                              select(User).where(User.email == "site@smarter.dev")
                          )
                          if owner is None:
                              raise RuntimeError("site key owner is missing")
                          required = {"site-memory", "site-privacy"}
                          permissions = await get_user_permissions(session, str(owner.id))
                          if not required <= permissions.permissions:
                              raise RuntimeError("site key owner lacks required permissions")
                          _, raw_key, _ = await create_api_key(
                              session, owner.id, "site",
                              principal_type="service",
                              service_name="site",
                              scoped_permissions=sorted(required),
                              expires_at=None,
                          )
                          await session.commit()
                          print(raw_key, flush=True)  # sk_...; capture once
                  finally:
                      await engine.dispose()

              asyncio.run(mint())
              PY
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

Run `kubectl -n smarter-dev apply -f site-key-job.yaml`, then read the raw key
once with `kubectl -n smarter-dev logs job/smarter-dev-site-key`. Store it in a
secret manager immediately and delete the Job after capture. This does not
execute code inside a running web or bot pod.

Patch only the `site-api-key` field of the existing `smarter-dev-secrets`
Kubernetes Secret in namespace `smarter-dev`. Follow the safe one-field
rotation procedure in `docs/v2/legacy-sunset/runbooks/01-rotate-bot-key.md`,
substituting `site-api-key` for `bot-api-key`; avoid replacing the entire
Secret or putting the raw key in a shell argument. The Cloudflare Worker
deployment consumes the same key under its own secret configuration; that
deployment is handled separately. Validate the new key against the memory API
before discarding the one-time value.

## URL ownership

Production `SITE_BASE_URL` points to `smarter.dev` for public privacy, terms
and chat-agent URLs. `ADMIN_BASE_URL` points to `admin.smarter.dev` for
Python-owned confirmation, account, billing, bot configuration, search and
admin links, including the answer view. Both default to
`http://localhost:8000` for local development.

Public content literals intentionally stay on `smarter.dev`: canonical and
Open Graph URLs in the resource controllers, the marketing link in the signup
email, and public privacy, terms and chat-agent links. These identify the
public site after the Worker cutover. The notice keeps its public account
security link because Python serves it until cutover and the Worker afterwards.
Email addresses ending `@smarter.dev` are contact or sender addresses and are
unrelated to routing. Tests using a `smarter.dev` base explicitly exercise
public URLs or URL formatting.
