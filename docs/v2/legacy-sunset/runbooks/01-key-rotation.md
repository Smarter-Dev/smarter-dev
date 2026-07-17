# Runbook 01 — Bot API Key Rotation (legacy `sk-` → Skrift `sk_`)

HUMAN steps only. The dual-verify build (phase 01) accepts both key formats:
`verify_api_key` checks Skrift-native `sk_` keys against the main DB first
and falls back to the legacy `public.api_keys` table for `sk-` keys.

## 1. Deploy the dual-verify build

Bot keeps using the old `sk-` key. Verify `/api/auth/validate` returns 200 in
prod logs. Successful legacy auth is observable as this log line from
`smarter_dev.web.api.dependencies`:

```
legacy-api-key-auth: legacy sk- key 'sk-xxxxxxxx***' authenticated
```

The deploy also runs the legacy-tree migration `3f8d2c5b9a41`, which drops
the `security_logs.api_key_id` foreign key so rate-limit accounting works
for Skrift key IDs. No data changes.

## 2. Mint the Skrift service key

Either in the Skrift admin UI (`/admin/api-keys` → New Key, pick/create the
service-owner user, e.g. a dedicated `bot@smarter.dev` user) or via a one-off
`kubectl exec` on a web pod:

```python
from skrift.db.services.api_key_service import create_api_key
# inside an async session against the main DB (skrift schema):
api_key, raw_key, _refresh = await create_api_key(
    session,
    user_id,                       # service-owner user's UUID
    "discord-bot",
    principal_type="service",
    service_name="discord-bot",
    expires_at=None,               # no expiry for the bot service key
)
print(raw_key)  # sk_... — record it ONCE, it is not recoverable
```

Scoping (phase 04 decision, REQUIRED after the `/api` switchover): the native
controllers guard on the `bot-api` permission (`bot-api-admin` for admin-ish
paths), and Skrift intersects a key's scoped permissions with the owning
user's actual permissions. So:

1. Assign the `bot-service` role (defined in `smarter_dev/web/roles.py`,
   grants `bot-api` + `bot-api-admin`) to the service-owner user — via the
   admin users UI or `skrift.auth.services.assign_role_to_user`.
2. Mint the key with `scoped_permissions=["bot-api", "bot-api-admin"]` (add
   the kwarg to the `create_api_key` call above).

A key without both of these answers 401 on every guarded `/api` route.

## 3. Rotate the k8s secret

Patch only the `bot-api-key` entry of `smarter-dev-secrets`; do not touch
other values:

```bash
kubectl -n smarter-dev create secret generic smarter-dev-secrets \
  --from-literal=bot-api-key=sk_... --dry-run=client -o yaml | kubectl apply -f -
kubectl -n smarter-dev rollout restart deployment smarter-dev-bot
```

Verify bot auth succeeds in logs and the `legacy-api-key-auth` line stops
appearing.

## 4. Soak

Soak >= 1 week watching for any `legacy-api-key-auth` log lines. Any hit
means something still holds the old key.

## 5. Remove the legacy fallback (code change, gated by phases 04/05)

- ~~Delete the `sk-` branch in `verify_api_key`~~ **DONE** — the phase 04
  switchover deleted `smarter_dev/web/api/` (including `dependencies.py`)
  outright; the native `/api` accepts only Skrift `sk_` keys. **The bot must
  already be on its `sk_` key (steps 2–4) before that build deploys.**
- Still pending (phase 05 decommission):
  - Delete the legacy shape in `smarter_dev/web/security.py`
    `validate_api_key_format` (marked `LEGACY-FALLBACK`) and in
    `smarter_dev/bot/services/api_client.py`.
  - Deactivate all rows in legacy `public.api_keys`.
