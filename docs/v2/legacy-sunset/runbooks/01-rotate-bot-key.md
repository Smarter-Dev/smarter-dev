# Runbook 01 — Rotate the Bot API Key (legacy `sk-` → Skrift `sk_`)

HUMAN steps only. Nothing here is performed automatically by any agent or
migration. This runbook covers rotating the Discord bot's production API key
from a legacy `sk-` key to a Skrift-native `sk_` key.

> Prerequisite: the **dual-verify** web build from phase 01 must be deployed
> first. It accepts both key shapes — `verify_api_key`
> (`smarter_dev/web/api/dependencies.py`) checks Skrift `sk_` keys against the
> main DB `skrift.api_keys` table first and falls back to the legacy
> `public.api_keys` table for `sk-` keys. See the companion runbook
> `01-key-rotation.md` for the web-side dual-verify details and the legacy
> migration that ships with that deploy.

## Why the bot needs no coordinated redeploy

The bot HTTP client (`smarter_dev/bot/services/api_client.py`) now accepts
**either** key shape at construction time via `is_valid_api_key_format`:

- Legacy: `sk-` + 43 base64url chars (46 total).
- Skrift: `sk_` + `secrets.token_urlsafe(32)` (43 chars today), with a
  20–200 char bound so a future Skrift padding change won't lock the bot out.

`shared/config.py:bot_api_key` documents that both are valid. Because the
already-running bot pods accept both formats, you can swap the secret and roll
the bot **without** sequencing a code deploy. The bot only shape-checks the
key; the web API is the source of truth for validity.

## 1. Confirm the dual-verify build is live

In prod web logs, confirm legacy auth still succeeds (the bot is still on the
`sk-` key at this point):

```
legacy-api-key-auth: legacy sk- key 'sk-xxxxxxxx***' authenticated
```

`/api/auth/validate` should return 200 for the bot.

## 2. Mint the Skrift service key

Preferred: Skrift admin UI at `/admin/api-keys` → **New Key**. Pick or create
the service-owner user (a dedicated `bot@smarter.dev` service user is
recommended), set principal type **service** and service name `discord-bot`,
no expiry.

Alternative (one-off `kubectl exec` on a web pod), against the main DB in an
async session on the `skrift` schema:

```python
from skrift.db.services.api_key_service import create_api_key

api_key, raw_key, _refresh = await create_api_key(
    session,
    user_id,                       # service-owner user's UUID
    "discord-bot",
    principal_type="service",
    service_name="discord-bot",
    expires_at=None,               # no expiry for the long-lived bot service key
)
print(raw_key)  # sk_... — record it ONCE; it is not recoverable
```

Grant the key the **minimal scoped permissions** the bot actually uses (bytes
economy, squads, help/chat, forum-agent, moderation endpoints). Apply phase
04's scoping decisions once they land; until then scope to the bot's endpoint
set rather than a full-admin role.

## 3. Update the `bot-api-key` k8s secret

Patch **only** the `bot-api-key` entry of `smarter-dev-secrets`. Do not touch
any other value in the secret:

```bash
kubectl -n smarter-dev create secret generic smarter-dev-secrets \
  --from-literal=bot-api-key=sk_... --dry-run=client -o yaml | kubectl apply -f -
```

## 4. Roll the bot deployment

```bash
kubectl -n smarter-dev rollout restart deployment smarter-dev-bot
kubectl -n smarter-dev rollout status deployment smarter-dev-bot
```

## 5. Verify

- Bot startup logs show it loaded a key (`api_client` accepts the `sk_` shape;
  a bad key raises `ValueError: Invalid API key format` at startup).
- A bot command that hits the API (e.g. `/bytes balance`) succeeds.
- Web logs show Skrift-key auth succeeding and the `legacy-api-key-auth` line
  **stops** appearing for the bot.

## 6. Soak

Soak ≥ 1 week, watching for any `legacy-api-key-auth` log lines. Any hit means
something still holds the old `sk-` key — investigate before proceeding.

## 7. Later — remove the legacy verify fallback (gated by phases 04/05)

This is a code change, deferred until after the rollback window for phase 02's
deploy has closed:

- Delete the `sk-` fallback branch in `verify_api_key`
  (`smarter_dev/web/api/dependencies.py`, marked `LEGACY-FALLBACK`).
- Delete the legacy shape in `smarter_dev/web/security.py`
  `validate_api_key_format` (marked `LEGACY-FALLBACK`) and the legacy `sk-`
  branch/`_LEGACY_KEY_PREFIX` handling in
  `smarter_dev/bot/services/api_client.py` `is_valid_api_key_format`.
- Remove the legacy `public.api_keys` code path
  (`crud.APIKeyOperations.get_api_key_by_hash`) and deactivate all rows in the
  legacy `public.api_keys` table.
- Update the bot key-validation tests
  (`tests/bot/services/test_api_client_key_validation.py`) to drop the legacy
  `sk-` acceptance cases.
