# Python app on admin.smarter.dev

The Python web app, bot API and workers remain behind the `smarter-dev-website`
service. The ingress can accept both `smarter.dev` and `admin.smarter.dev`, but
production login and Python-owned links stay on `smarter.dev` until Zech signs
off on the new site and the cutover task runs. Nothing about login changes
until the **At cutover** section runs.

## Now

1. Point `admin.smarter.dev` at the existing ingress. The ingress alias is
   additive: both hostnames remain in its TLS entry and rules. Verify
   cert-manager has issued `smarter-dev-website-cert` with **both** hostnames
   in its SANs, then confirm HTTPS to the admin host reaches the Python app.
2. Prepare the site service key below after its API role is registered.

## Site service key

After the site memory API and privacy routes land, create a dedicated Skrift
service user (for example `site@smarter.dev`) and assign it a role granting
`site-memory`, `site-privacy`, `site-billing`, `site-search`,
`site-resources` and `site-code`. The owner's role and key scopes must both include these
permissions, because Skrift intersects them when authorizing a request. The API also requires `service_name="site"` exactly. Skrift's
`/admin/api-keys` **New Key** form cannot set the principal type or service
name, so mint this key through `create_api_key`, as the bot-key runbook does.
This is for a first key only: to give the existing key a newly added scope,
don't re-mint; see [Adding a scope to the existing site
key](#adding-a-scope-to-the-existing-site-key).

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
                          required = {"site-memory", "site-privacy", "site-billing", "site-search", "site-resources", "site-code"}
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

### Adding a scope to the existing site key

When a new site route needs a scope the existing key lacks (for example
`site-resources`), **do not re-mint**. Add the scope to the existing key in
place: the secret is unchanged, so the Worker's `SKRIFT_SITE_KEY` and the
`site-api-key` Secret field stay as they are and nothing is rotated. The
`site-service` role must already grant the scope (deploy the change that adds
it first), because Skrift intersects the role and the key scopes.

Either open the key in Skrift's `/admin/api-keys/{id}/edit` form and add the
scope, or run a one-off Job built like the mint Job above (same image,
`smarter-dev-config` and `database-url`; name it `smarter-dev-site-key-scope`)
with this script in place of the mint script. It finds the one active site
service key, appends the scope and prints only the key's prefix and scopes,
never a key:

```python
import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from skrift.db.models.api_key import APIKey
from skrift.db.services.api_key_service import update_api_key
from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import convert_postgres_url_for_asyncpg, create_async_engine

SCOPE = "site-resources"

async def add_scope():
    url = convert_postgres_url_for_asyncpg(get_settings().effective_database_url)
    engine = create_async_engine(url).execution_options(
        schema_translate_map={None: "skrift"}
    )
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            keys = (
                await session.scalars(
                    select(APIKey).where(
                        APIKey.principal_type == "service",
                        APIKey.service_name == "site",
                        APIKey.is_active.is_(True),
                    )
                )
            ).all()
            if len(keys) != 1:
                raise RuntimeError(f"expected one active site key, found {len(keys)}")
            key = keys[0]
            scopes = key.scoped_permission_list
            if SCOPE not in scopes:
                await update_api_key(session, key, scoped_permissions=[*scopes, SCOPE])
            print(key.key_prefix, sorted(key.scoped_permission_list), flush=True)
    finally:
        await engine.dispose()

asyncio.run(add_scope())
```

`update_api_key` commits. Running it twice is harmless. Check the Job log
shows the new scope, delete the Job, then confirm the Worker's calls to the
new route return 200 instead of 401.

## At cutover

1. Zech adds the admin callback URI to each provider OAuth app **before**
   changing the running app. Keep the existing `smarter.dev` callbacks for
   rollback:

   | Provider | Add this callback URI |
   | --- | --- |
   | Discord | `https://admin.smarter.dev/auth/discord/callback` |
   | GitHub | `https://admin.smarter.dev/auth/github/callback` |
   | Google | `https://admin.smarter.dev/auth/google/callback` |

   Add `https://admin.smarter.dev/bot-admin/auth/discord/callback` to the
   Discord app for the separate bot-admin flow too.
2. In `app.yaml`, set `auth.redirect_base_url` to
   `https://admin.smarter.dev`. In `k8s/configmap.yaml`, set
   `DISCORD_REDIRECT_URI` to
   `https://admin.smarter.dev/bot-admin/auth/discord/callback` and
   `ADMIN_BASE_URL` to `https://admin.smarter.dev`. Update the production
   fallback of `effective_discord_redirect_uri` in
   `smarter_dev/shared/config.py` to the admin host in the same change.
   Leave `SITE_BASE_URL=https://smarter.dev` and the `.smarter.dev` session
   cookie in place.
3. Deploy the cutover change. Check sign-in on `admin.smarter.dev` with
   Discord, GitHub and Google. The shared cookie lets the public site and
   admin host share the login.

## URL ownership

Production `SITE_BASE_URL` points to `smarter.dev` for public privacy, terms,
chat-agent, signup confirmation and account billing URLs. `ADMIN_BASE_URL`
also points to `smarter.dev` now, then changes to `admin.smarter.dev` at
cutover for Python-owned bot configuration, search and admin links, including
the answer view. Both default to `http://localhost:8000` for local
development.

Public content literals intentionally stay on `smarter.dev`: canonical and
Open Graph URLs in the resource controllers, the marketing link in the signup
email, and public privacy, terms and chat-agent links. These identify the
public site after the Worker cutover. The notice keeps its public account
security link because Python serves it until cutover and the Worker afterwards.
Email addresses ending `@smarter.dev` are contact or sender addresses and are
unrelated to routing. Tests using a `smarter.dev` base explicitly exercise
public URLs or URL formatting.
