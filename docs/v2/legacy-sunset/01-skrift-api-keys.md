# 01 — Skrift-Native API Keys

Move bot authentication from the legacy `public.api_keys` table (`sk-` keys,
verified in `smarter_dev/web/api/dependencies.py:42`) to Skrift's built-in API
key system (`sk_` keys in the **main DB** `skrift.api_keys` table), with a
dual-verify window so the currently-deployed key never breaks.

## Current state (verified)

- **Legacy verify**: `api/dependencies.py:42 verify_api_key` — HTTPBearer →
  `security.py:66 validate_api_key_format` (exactly 46 chars, `sk-` + 43
  base64url) → `security.py:50 hash_api_key` (SHA-256) →
  `crud.py:1662 APIKeyOperations.get_api_key_by_hash` against the **legacy DB**
  (its session comes from `get_database_session` → `shared/database.py:182
  get_db_session`, which defaults to the legacy engine).
- **Bot client**: `smarter_dev/bot/services/api_client.py:90` hard-rejects
  anything not `sk-` + 46 chars before it will even send a request.
- **Skrift native** (installed package, currently unused by this repo):
  - `skrift/db/models/api_key.py` — `APIKey` model: `key_prefix` (12),
    `key_hash` SHA-256, `scoped_permissions`/`scoped_roles`
    (newline-delimited), `principal_type` (default `"user"`, use
    `"service"`), `service_name`, `is_active`, `expires_at`, refresh-token
    rotation columns.
  - `skrift/db/services/api_key_service.py` — `create_api_key(...) ->
    (APIKey, raw_key, raw_refresh_token)` (line 46), `verify_api_key(db_session,
    raw_key, *, client_ip) -> APIKey | None` (line 98; checks active/expiry/user
    active, updates `last_used_at`/`last_used_ip`).
  - `skrift/auth/guards.py` — `auth_guard` (line 213) with `APIKeyAuth` /
    `APIKeyOnly` markers; only considers bearer tokens starting `sk_`
    (guards.py:236).
  - `skrift/admin/api_keys.py` — `APIKeyAdminController`, path `/admin`,
    routes under `/api-keys`, guarded by `Permission("manage-api-keys")`.
    **Not registered** in this repo's `app.yaml`.
  - `skrift/config.py:730 APIKeyConfig` — config key `api_keys:`
    (`enabled`, `default_expiration_days`, `max_keys_per_user`,
    `refresh_token_expiration_days`). `app.yaml` currently has **no**
    `api_keys:` block (defaults apply: enabled=True).
- **Unregistered dead controller**: `smarter_dev/web/api_keys_admin.py`
  (154 lines) manages the *legacy* key table via `crud.APIKeyOperations` and is
  never listed in `app.yaml` `controllers:` — delete it.
- **Key provisioning today**: `scripts/bootstrap.py:116 _provision_bot_key`
  writes a legacy `APIKey` row into the legacy DB. Prod bot reads
  `BOT_API_KEY` from k8s secret `smarter-dev-secrets` key `bot-api-key`
  (`k8s/deploy-bot.yaml:63-67`).
- Skill reference: `.claude/skills/skrift-auth` (repo) — load it before
  touching guard code.

## Implementation steps (TDD each)

### 1. Dual-verify in `api/dependencies.py`

Rewrite `verify_api_key` (dependencies.py:42) to:

1. Keep the case-sensitive `Bearer ` scheme check and empty-token 401 as-is.
2. **Skrift path first**: if the token starts with `sk_`, open a main-DB
   session via `shared/database.py:229 get_skrift_db_session` (already exists,
   used by `polar_webhooks.py`/`sudo_converge.py`) and call
   `skrift.db.services.api_key_service.verify_api_key(session, token,
   client_ip=request.client.host)`. On success, adapt the returned Skrift
   `APIKey` so downstream code keeps working (see "compat shim" below) and
   return it. On `None`, fall through to a 401 (do NOT try the legacy hash for
   an `sk_` token — the formats are disjoint).
3. **Legacy fallback**: if the token starts with `sk-`, keep the existing flow
   (format check → hash → `APIKeyOperations.get_api_key_by_hash` → expiry
   check → security log). Mark this branch with a
   `# LEGACY-FALLBACK: remove after key rotation (see runbook)` comment.
4. Anything else: existing generic 401.

Compat shim: downstream consumers touch `request.state.api_key` and use
`api_key.id`, `api_key.key_prefix`, and rate-limit fields. Grep consumers
(`multi_tier_rate_limiter.py:62 _get_windows_for_api_key`,
`security_logger.py`) and either (a) give the shim the attributes they read, or
(b) key the rate limiter off `key_prefix` only. Prefer a small
`AuthenticatedKey` dataclass (`id`, `key_prefix`, `principal_name`,
`is_legacy`) produced by both branches so no downstream module needs to know
which table the key came from.

Note `request.state.db_session` (dependencies.py:170) is set to the session
that verified the key; for Skrift keys that must be the skrift-schema session.
The rate limiter logs usage against the legacy DB today — keep its
session-source unchanged in this phase (it still works for both branches
because it only reads/writes its own tables); it moves wholesale in 02.

Tests (`tests/web/`): valid `sk_` key → 200 path; revoked/expired/inactive-user
`sk_` key → 401; valid legacy `sk-` key still authenticates; malformed
prefixes (`sk-` wrong length, `sk_` unknown, garbage, missing Bearer) → 401.
Existing legacy-key tests must stay green.

### 2. Relax the format gates to accept both prefixes

- `smarter_dev/web/security.py:66 validate_api_key_format`: accept **either**
  the legacy shape (exactly `sk-` + 43 base64url) **or** a Skrift shape
  (`sk_` prefix, `secrets.token_urlsafe(32)`-derived — verify actual generated
  length from `api_key_service._generate_key` (`sk_` + token_urlsafe(32) ⇒ 46
  chars total; assert in a test against a real `create_api_key` output) and
  validate prefix + base64url charset + length range rather than a hardcoded
  46 if the service ever pads differently).
- `smarter_dev/bot/services/api_client.py:90`: replace
  `if not api_key.startswith("sk-") or len(api_key) != 46` with a check that
  accepts `sk-` (legacy) or `sk_` (Skrift) prefixes with a sane length bound
  (e.g. 20–200). The bot must not need a redeploy sequenced with the key swap.

Tests: `tests/bot/` client construction with `sk_` key succeeds; junk still
raises `ValueError`.

### 3. Enable Skrift's key admin

- `app.yaml`: add
  ```yaml
  api_keys:
    enabled: true
    default_expiration_days: 0   # verify: skrift semantics for "no expiry"; if 0 unsupported use a long horizon
  ```
  (confirm accepted values against `skrift/config.py:730` before committing)
  and register the built-in controller in `controllers:`:
  `- skrift.admin.api_keys:APIKeyAdminController`. Mirror in
  `app.development.yaml` if it carries its own `controllers:` list (check).
- Grant the `manage-api-keys` permission to the admin role — check
  `smarter_dev/web/roles.py` (registers custom Skrift roles) and add the
  permission to the administrator role definition if not already implied.
- Delete `smarter_dev/web/api_keys_admin.py` and its templates
  `templates/admin/api-keys/` **only after** confirming the Skrift built-in
  ships its own templates (it renders from the skrift package theme; verify by
  loading `/admin/api-keys` in the harness). Also delete any tests importing
  `smarter_dev.web.api_keys_admin`.

Harness: add `/admin/api-keys` to `SKRIFT_ADMIN_PAGES` in
`scripts/local_harness/expectations.py`.

### 4. Seed/bootstrap updates

- `scripts/bootstrap.py:116 _provision_bot_key`: switch to
  `api_key_service.create_api_key(session, user_id, "discord-bot",
  principal_type="service", service_name="discord-bot", ...)` against the
  **main** DB (needs a service-owner user: use the first admin user, or create
  a dedicated `bot@smarter.dev` service user — pick one and document it in the
  runbook). Keep `'reused' | 'rotated' | 'created'` semantics by hash-matching
  the `.env` key.
- Harness (`scripts/local_harness/config.py:56` key derivation + `seed.py` key
  row): per 06-test-harness.md, once the bot key is `sk_`, seed a Skrift
  `api_keys` row in the main DB instead of legacy `public.api_keys`, keep the
  bad/malformed-key 401 checks. During the dual-verify window the harness
  should seed **both** and assert **both** authenticate (drop the legacy row in
  phase 05).

## Runbook (HUMAN steps) — write to `runbooks/01-key-rotation.md`

1. Deploy the dual-verify build. Bot keeps using the old `sk-` key; verify
   `/api/auth/validate` 200s in prod logs.
2. Mint the service key: in the Skrift admin UI (`/admin/api-keys` → New) or
   via a one-off `kubectl exec` running a snippet that calls
   `api_key_service.create_api_key(..., principal_type="service",
   service_name="discord-bot", scoped_roles=/permissions per 04 scoping)`.
   Record the raw `sk_...` once — it is not recoverable.
3. Rotate the secret:
   `kubectl -n smarter-dev create secret generic smarter-dev-secrets --from-literal=bot-api-key=sk_... --dry-run=client -o yaml | kubectl apply -f -`
   (patch only that key; do not touch other values), then restart the bot
   deployment. Verify bot auth in logs.
4. Soak ≥ 1 week, watching for any legacy-branch auth in logs (add a counter
   log line in the fallback branch to make this observable).
5. Remove the legacy fallback: delete the `sk-` branch in
   `verify_api_key`, the legacy shapes in `security.py` /
   `api_client.py`, deactivate all rows in legacy `public.api_keys`
   (this step also gated by phase 04/05 timing — the fallback must outlive any
   rollback window for phase 02's deploy).

## Definition of done for this phase

- Bot can authenticate with either key format; prod is switched to `sk_`.
- `/admin/api-keys` (Skrift built-in) is live; `api_keys_admin.py` deleted.
- Harness green with updated expectations; full pytest green.
