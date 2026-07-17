# 03 — Legacy `/bot-admin` Removal (operator notes)

Phase 03 deleted the legacy Starlette admin mounted at `/bot-admin`. Every
feature now lives under Skrift-native `/admin/bot/...` behind
`Permission("administrator")`. This file records the operator-facing
consequences; there are no deploy-time human actions beyond a normal deploy.

## What changed at runtime

- The `/bot-admin` ASGI mount is gone. **Any `/bot-admin` or `/bot-admin/...`
  URL now returns `404`** (no redirect was added — the legacy paths do not map
  1:1 onto the new `/admin/bot` paths, and the Definition of Done requires a
  clean 404). Update bookmarks to the new locations:

  | Old | New |
  | --- | --- |
  | `/bot-admin/` | `/admin/bot` |
  | `/bot-admin/guilds` | `/admin/bot/guilds` |
  | `/bot-admin/guilds/{gid}/bytes` | `/admin/bot/guilds/{gid}/bytes` |
  | `/bot-admin/conversations` | `/admin/bot/help-conversations` |
  | (all other `/bot-admin/...`) | same suffix under `/admin/bot/...` |

- Admin auth is now Skrift session auth (the same login as the rest of
  `/admin`). The legacy standalone Discord-OAuth session stack
  (`SessionMiddleware` keyed on `WEB_SESSION_SECRET`) is deleted.

## Leftover config (removed later, in phase 05)

`WEB_SESSION_SECRET` / `settings.web_session_secret` was consumed **only** by
the deleted legacy admin. The setting field is left in place this phase so the
app still boots without a manifest change; phase 05 removes the
`web_session_secret` setting and the `WEB_SESSION_SECRET` env from
`k8s/deploy.yaml`, `k8s/deploy-worker.yaml`, `k8s/migrate-job.yaml`, and any
`compose.yaml`. No secret VALUE needs to change.

## Verification

- `GET /bot-admin/` → `404`, `GET /bot-admin/guilds` → `404`.
- `GET /admin/bot` and the ported feature pages render for an admin session.
- Smoke harness: `BOT_ADMIN_GONE_PAGES` (404 assertions) replaces the old
  `LEGACY_ADMIN_PAGES`; `SKRIFT_ADMIN_PAGES` covers the ported pages.
