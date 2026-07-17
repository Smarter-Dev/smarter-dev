# 04 — API Rewrite: FastAPI mount → Litestar controllers

Rewrite `smarter_dev/web/api/` (~21 FastAPI routers mounted at `/api` via
`controllers.py:219 api_mount`) as native Litestar controllers guarded by
Skrift auth, **preserving exact paths, verbs, status codes, and response
shapes** so `smarter_dev/bot/services/api_client.py` (and every bot service on
top of it) needs zero changes. Depends on phases 01 (Skrift keys) and 02
(single DB — injected `db_session` is valid for all tables).

## Strategy: build unregistered, switch over atomically

1. New package `smarter_dev/web/botapi/` (name avoids colliding with the
   legacy `web/api/`). One module per port unit below; every controller sets
   `path = "/api/..."` but is **not** added to `app.yaml` yet — the FastAPI
   mount keeps owning `/api` and the harness/suite stay green.
2. Each unit ships with isolated tests: `litestar.testing.create_test_client`
   with just that controller + a guard/session stub, asserting status codes
   and response JSON against fixtures captured from the FastAPI behavior
   (write the fixture test against the *old* endpoint first where practical —
   the FastAPI TestClient is still importable until switchover).
3. **Switchover commit**: add all `botapi` controllers to `app.yaml`
   `controllers:`, remove `- smarter_dev.web.controllers:api_mount`
   (app.yaml:62) and the `api_mount` handler + `_normalize_mounted_path`
   (controllers.py:36-57, 219-225), delete `smarter_dev/web/api/` and its
   tests, run harness. `API_CHECKS` in
   `scripts/local_harness/expectations.py` should pass **unmodified** — that
   is the parity proof. Any expectation edit in this commit is a red flag to
   justify explicitly.

## Auth model

- Guards: `guards=[auth_guard, APIKeyOnly(), Permission("bot-api")]` on every
  controller (imports from `skrift.auth.guards`). `APIKeyOnly` (guards.py:133)
  restricts to `Bearer sk_...`; session cookies never authenticate the API.
- Register a `bot-api` permission in `smarter_dev/web/roles.py` and grant it
  to the bot's service key via `scoped_permissions` (phase 01 runbook mints
  the key with it). Admin-ish endpoints (admin stats, conversations write
  paths) additionally take `Permission("bot-api-admin")` — grant on the same
  key; the split exists so a future narrow key can be minted without code
  change.
- **Consequence**: the switchover retires the legacy `sk-` fallback for `/api`
  (Skrift's guard only accepts `sk_`). Prod sequencing: bot must already be on
  the `sk_` key (phase 01 runbook step 3) before this deploys. State this in
  the switchover commit message and runbook note.
- 401-parity: Skrift's guard returns its own 401 shape. `API_CHECKS` asserts
  bad/malformed/missing key → 401/403; verify the exact statuses the harness
  expects and, if the guard's default differs, add an exception handler on the
  botapi controllers producing `{"detail": ...}` like FastAPI did — the bot's
  `api_client.py` raises `AuthenticationError` off status codes, and services
  may read `detail` from error bodies (grep `\.json()\[.detail.\]` in
  `smarter_dev/bot/` during implementation).

## Response/behavior parity rules

- Recreate the Pydantic response models by importing the existing schemas
  from `smarter_dev/web/api/schemas.py` — move that module to
  `smarter_dev/web/botapi/schemas.py` at switchover rather than rewriting it.
- Preserve status codes incl. 201s (`admin.py:85`, `handlers.py:163`,
  `admin_handlers.py:127`, `chat_conversations.py:56`) and 204/200 deletes
  (`model_overrides.py:78` returns 204 per harness check).
- Trailing slashes: FastAPI runs with `redirect_slashes=False` and the mount
  normalizes paths (`controllers.py:36`). `squads.py:152/183` and
  `squad_sale_events.py:42` declare `"/"` routes — the real client paths are
  `/api/guilds/{gid}/squads/` etc. Check each `api_client` call site for the
  exact string the bot sends and declare that exact path in Litestar; add a
  test per ported route hitting the literal bot-sent path.
- `X-User-ID` header dependency (`dependencies.py:220 get_current_user_id`) —
  reproduce as a Litestar dependency with the same 400s.
- Kill the FastAPI lifespan's `init_database()` (`api/app.py` lifespan) —
  Litestar controllers use the injected session; the shared-engine bootstrap
  belongs to whatever still uses `get_db_session_context` (bot only).

## Rate-limiting parity

Today: `dependencies.py:262 apply_rate_limiting` →
`multi_tier_rate_limiter.py:317 enforce_multi_tier_rate_limits`, windows
10 req/s, 180 req/min, 2500 req/15 min per API key, emits
`X-RateLimit-*` headers and 429 with escalation info; usage counting is
DB-backed (`_get_usage_count_for_window:91`).

Port choice (pick one at implementation, document in code):
- **A (preferred, least drift)**: port `MultiTierRateLimiter` untouched and
  invoke it from a Litestar `before_request`/dependency on the botapi
  controllers, keyed off the authenticated Skrift key's `key_prefix`; keep
  header + 429 semantics byte-compatible.
- **B**: Skrift's built-in rate limit config (`skrift/config.py` RateLimit,
  `key: "api_key"`) — only if it can express the three tiers and the
  headers; otherwise A.
The bot's client reads `X-RateLimit-*`/`Retry-After` (`api_client.py` rate
limit state) — headers are part of the contract.

## Port units (~10 from 21 routers)

Paths shown are the final full paths (all under `/api`). Line anchors are the
`@router.*` decorators in `smarter_dev/web/api/routers/`.

| Unit | Source routers | Endpoints (verb path) | Notes |
| --- | --- | --- | --- |
| U1 core/auth | `auth.py` (prefix `/auth`, app.py:389), `/health` (app.py:437) | POST `/api/auth/validate`, GET `/api/auth/health`, GET `/api/auth/status`, GET `/api/health` | `/api/health` is unauthenticated today — keep it guard-free. First unit to port; proves the auth pattern. |
| U2 bytes | `bytes.py` (prefix `/guilds/{guild_id}/bytes`, app.py:391) | GET `` (balance, :55), POST `/daily`, POST `/transactions`, GET `/leaderboard`, GET `/transactions`, GET `/config`, PUT `/config`, DELETE `/config`, POST `/reset-streak/{user_id}` | Highest-traffic; harness asserts seeded balance == 1000. |
| U3 squads | `squads.py` (prefix `/guilds/{gid}/squads`), `squad_sale_events.py` (prefix `/guilds/{gid}/squad-sale-events`), `members.py` (prefix `/guilds/{gid}/members`) | squads: GET `/`, POST `/`, GET `/{squad_id}`, PUT `/{squad_id}`, POST `/{squad_id}/join`, DELETE `/leave`, GET `/members/{user_id}`, GET `/{squad_id}/members`; sale-events: GET `/`, GET `/{event_id}`; members: DELETE `/{user_id}` | Trailing-slash canary unit. |
| U4 challenges | `challenges.py` (prefix `/challenges`) | GET `/upcoming-announcements`, `/pending-announcements`, POST `/{id}/mark-released`, `/{id}/mark-announced`, GET `/scoreboard`, `/upcoming-campaign`, `/detailed-scoreboard`, GET `/{id}`, GET `/{id}/input-exists`, GET `/{id}/input`, POST `/{id}/submit-solution` | Static segments (`/scoreboard` etc.) must win over `/{challenge_id}` — Litestar handles this, but add route-order tests. |
| U5 quests | `quests.py` (prefix `/quests`) | GET `/daily/current`, POST `/{daily_quest_id}/submit`, GET `/{daily_quest_id}/input`, GET `/scoreboard`, `/detailed-scoreboard`, `/upcoming-announcements`, POST `/{id}/mark-announced`, `/{id}/mark-active` | |
| U6 messaging | `scheduled_messages.py` (`/scheduled-messages`), `repeating_messages.py` (`/repeating-messages`), `advent_of_code.py` (`/advent-of-code`) | scheduled: GET `/upcoming`, `/pending`, POST `/{id}/mark-sent`, GET `/{id}`; repeating: GET `/due`, POST `/{id}/mark-sent`, POST `/`, GET `/guild/{guild_id}`, GET/PUT/DELETE `/{id}`, POST `/{id}/toggle`; AoC: GET `/active-configs`, GET `/{gid}/config`, GET `/{gid}/threads/{year}/{day}`, POST `/{gid}/threads`, GET `/{gid}/threads` | |
| U7 forum | `forum_agents_simple.py` (prefix `/guilds/{gid}/forum-agents`), `forum_notifications.py` (no prefix) | agents: GET ``, POST `/{agent_id}/responses`, GET `/{agent_id}/responses/count`; notifications: GET `/guilds/{gid}/forum-channels/{fcid}/notification-topics`, GET `.../user-subscriptions`, GET+PUT `/guilds/{gid}/users/{uid}/forum-subscriptions/{fcid}` | |
| U8 telemetry | `chat_conversations.py` (`/chat-conversations`), `image_quota.py` (`/image-generations`), `activity.py` (`/activity`), `model_overrides.py` (no prefix) | chat: POST `/engagements`, POST `/engagements/{id}/end`, POST `/turns`, GET `/usage-leaderboard`; quota: GET `/quota`, POST `/reserve`, POST `/release`; activity: POST `/batch`; overrides: GET/PUT/DELETE `/guilds/{gid}/channels/{cid}/model-override` | model-override DELETE returns 204 (harness-checked). |
| U9 handlers+admin — **SENSITIVE** | `handlers.py` (`/handlers`), `admin_handlers.py` (`/admin/handlers`), `admin.py` (`/admin`) | handlers: POST ``, PUT `/{id}`, GET ``, DELETE `/{id}`, POST `/dispatch`, GET `/active-channels`, GET `/{id}`; admin-handlers: POST ``, PUT `/{id}`, GET ``, DELETE `/{id}`; admin: GET `/stats`, POST/GET `/api-keys`, GET/PUT/PATCH/DELETE `/api-keys/{key_id}`, POST/GET `/conversations`, GET `/conversations/{id}`, GET `/conversations/stats` | `admin.py` `/api-keys` CRUD operates on the **legacy** key table. Decide with Zech before porting: (a) drop these endpoints at switchover (keys are managed in `/admin/api-keys` UI after phase 01 — preferred if nothing external calls them; grep bot + harness first: harness does not check them), or (b) port against `skrift api_key_service` with shape adaptation. Route-order fix needed regardless: `/conversations/stats` (admin.py:572) is declared AFTER `/conversations/{conversation_id}` (:520) — FastAPI matches `{conversation_id:UUID-ish}` loosely; verify current behavior of GET `/api/admin/conversations/stats` before assuming, and make the Litestar port unambiguous. |
| U10 billing — **SENSITIVE** | `polar_webhooks.py` (`/polar-webhooks`), `sudo_converge.py` (`/sudo`) | POST `/polar-webhooks/events` (NO api key — standard-webhooks signature verified against `settings.polar_webhook_secret`; must remain reachable unauthenticated), POST `/sudo/converge` (api key) | Money path: port last, byte-for-byte behavior, extra tests around signature failure (403/`Invalid signature.`), idempotency via `webhook_events_processed`. Coordinate a Polar webhook test-delivery after deploy (runbook note). Both already use the skrift-schema session. |

Dead code, delete without porting: `routers/campaign_signups.py` (208 lines —
never `include_router`ed in `api/app.py`; live replacement is
`smarter_dev/web/campaign_signups_api.py`).

## Cross-cutting deletions at switchover

- `smarter_dev/web/api/` (app.py, dependencies.py, routers/, schemas.py —
  schemas move to botapi), plus FastAPI-only middleware wiring
  (`security_headers.py` / `http_methods_middleware.py` stay only if grep
  shows other consumers; Skrift already applies security headers via
  app.yaml `security_headers:`).
- `verify_api_key` legacy branch (if not already removed by the phase 01
  runbook step) dies with `dependencies.py`. The new guard path must keep a
  security-log equivalent for failed auth: port
  `security_logger.log_authentication_failed` hookup into the botapi auth
  layer or explicitly decide to drop it (note in commit).
- Tests under `tests/web/` importing `smarter_dev.web.api` are replaced by the
  botapi unit tests written during the port (keep coverage parity: every
  endpoint keeps at least happy path + auth failure + one validation/404 case).

## Definition of done

- All units live under Litestar; `smarter_dev/web/api/` deleted; FastAPI no
  longer imported anywhere (`grep -rn "fastapi" smarter_dev/` clean); remove
  the dependency from `pyproject.toml` if unused.
- Harness `API_CHECKS` green **without edits** (except U9 option (a) removals,
  which harness doesn't check anyway); bot code untouched.
- Rate-limit headers + 429 behavior preserved (tests).
