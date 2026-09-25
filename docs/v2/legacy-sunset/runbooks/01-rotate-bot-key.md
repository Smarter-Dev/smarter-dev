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

Replace **only** the `bot-api-key` entry of `smarter-dev-secrets`. Keep the
old key active in `/admin/api-keys` until step 5 passes: the outgoing bot pod
keeps the old key until it exits, and the old key is the rollback.

Do not use `kubectl create secret generic … --from-literal=… | kubectl apply`.
It builds a Secret holding only `bot-api-key`, and applying that over the live
Secret can drop or replace its other keys (Discord token, media key, Logfire
token, …). It also puts the raw key on a command line, where shell history and
the process list can see it.

Instead, read the key without echoing it, check it against the API, write a
merge patch to a private file, and patch that one field. Paste the whole block
at once: it runs in a subshell, so its settings and traps end with it.

- `set +xv` comes first. `read -s` only hides typing; with tracing on (`set -x`,
  or `SHELLOPTS`/`BASH_ENV` from the environment) every line that expands
  `$key` would print it.
- `set -e` plus the `EXIT` trap remove both temp files on any failure,
  including Ctrl-C, not only at the end.
- The key never appears in an argument list or on screen: `printf` and `[[`
  are shell builtins, `base64` reads it from a pipe, and `curl` reads the
  header from a file (`-H @file`).
- The secret is untouched unless the new key is accepted by
  `/api/auth/validate` and the key names match before and after.
- The validation prints only the status code. `-q` (first) skips `~/.curlrc`,
  which could add `-v` or `--trace` and print the request headers; `-o
  /dev/null` drops the body; `-f` turns any non-2xx into an error that names
  only the status.

```bash
(
  set +xv
  set -euo pipefail
  umask 077
  patch_file=$(mktemp) header_file=$(mktemp) names_before=$(mktemp)
  trap 'rm -f "$patch_file" "$header_file" "$names_before"' EXIT
  trap 'exit 130' INT TERM HUP

  IFS= read -rs -p 'New bot key (sk_...): ' key; echo
  [[ $key == sk_* && ${#key} -ge 20 && ${#key} -le 200 ]] \
    || { echo "not an sk_ key; nothing changed" >&2; exit 1; }
  printf 'Authorization: Bearer %s\n' "$key" > "$header_file"
  printf '{"data":{"bot-api-key":"%s"}}' "$(printf '%s' "$key" | base64 -w0)" > "$patch_file"
  unset key
  [[ -s $patch_file ]]

  # The new key must authenticate before the secret changes (200, or stop):
  curl -q -fsS -o /dev/null -w 'validate: %{http_code}\n' -X POST \
    -H @"$header_file" https://smarter.dev/api/auth/validate
  rm -f "$header_file"

  # Key names only, never values:
  names() {
    kubectl -n smarter-dev get secret smarter-dev-secrets \
      -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}'
  }
  names > "$names_before"
  kubectl -n smarter-dev patch secret smarter-dev-secrets --type merge --patch-file "$patch_file"
  names | diff "$names_before" - && echo "key names unchanged"
)
```

A non-zero exit before the `patch` line means nothing changed. A `diff`
after it means a key name went missing: stop, do not roll, and restore that
entry before anything restarts. On macOS use `base64` without `-w0`. Keep the
old key's value in a password manager until step 5 passes, for rollback.

## 4. Roll the bot deployment

The bot reads the key at startup, so it needs a new pod. Prefer the next
planned bot deploy over a dedicated restart: the rolling handover replaces the
pod once, and the old pod keeps working on the old key until it exits. The
new pod must run code that no longer logs part of the key (PR #103).

If no deploy is planned and the rotation cannot wait:

```bash
kubectl -n smarter-dev rollout restart deployment smarter-dev-bot
kubectl -n smarter-dev rollout status deployment smarter-dev-bot
```

The `smarter-dev-sudo-sweep` CronJob reads the same `bot-api-key`; its next
run picks up the new key with no action.

## 5. Verify the bot and the sudo sweep, then revoke

Both workloads that mount `bot-api-key` must pass before the old key is
revoked.

**Bot** (after the new pod is Ready and the old one has exited):

```bash
kubectl -n smarter-dev logs deploy/smarter-dev-bot --since=30m \
  | grep -E 'AuthenticationError| 401' || echo "no auth errors"
```

- No `AuthenticationError` or 401. The `… service health: healthy` lines do
  not prove the key: `/api/health` is unauthenticated.
- A bot command that calls an authenticated endpoint (e.g. `/bytes balance`)
  succeeds.

**Sudo sweep.** Its code talks to the database and to Discord with the
Discord bot token; it does not call the API with `bot-api-key`. It still
mounts that entry through a required `secretKeyRef`, though, so its pod does
not start if the entry is missing or renamed. Wait for the next scheduled run
(09:13 UTC) after the patch and check that it completed:

```bash
kubectl -n smarter-dev get jobs --sort-by=.metadata.creationTimestamp \
  | grep smarter-dev-sudo-sweep | tail -1        # COMPLETIONS 1/1, created after the patch
kubectl -n smarter-dev logs job/<that job> | grep -E 'sweep summary|sweep failed'
```

A `sweep summary` line passes. `CreateContainerConfigError` on its pod means
the secret entry is missing.

Then revoke the old key in `/admin/api-keys`. Revoke last: until then,
rollback is to patch the old value back the same way and roll the bot. After
revocation, rollback means minting another key.

## 6. Soak

Soak ≥ 1 week, watching for any `legacy-api-key-auth` log lines. Any hit means
something still holds the old `sk-` key — investigate before proceeding.

## 7. Later — remove the legacy verify fallback (gated by phases 04/05)

This is a code change, deferred until after the rollback window for phase 02's
deploy has closed:

- ~~Delete the `sk-` fallback branch in `verify_api_key`~~ **DONE** — the
  phase 04 switchover deleted `smarter_dev/web/api/` outright; the native
  `/api` accepts only Skrift `sk_` keys, so the bot must already be rotated
  (steps 1–6) before that build deploys.
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
