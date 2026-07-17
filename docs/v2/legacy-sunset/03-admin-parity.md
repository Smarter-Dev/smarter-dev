# 03 — Admin Parity: /bot-admin → Skrift /admin

Port every live `/bot-admin` feature to Skrift-native Litestar controllers,
then delete the mount and the whole legacy admin package. Pattern to copy is
`smarter_dev/web/bot_admin_skrift.py` (`BotAdminController`): Litestar
`Controller` with `path = "/admin"`, `guards=[auth_guard,
Permission("administrator")]` per route, `tags=[ADMIN_NAV_TAG]` +
`opt={"label", "icon", "order"}` on the nav entry-point route,
`get_admin_context(request, db_session)` merged into template context,
injected `db_session` (main DB, skrift schema — valid for legacy tables after
phase 02; **this phase depends on 02**), templates under `templates/admin/`
(NOT the theme dir — verified: `templates/admin/bot/overview.html`),
registration in `app.yaml` `controllers:`.

## Prerequisites / shared work

1. **Discord data client**: legacy views call `smarter_dev/web/admin/discord.py`
   (447 lines, `DiscordClient` — guilds, guild detail, roles, channels) which
   dies with the package. Extract the fetchers the ported pages need into a new
   `smarter_dev/web/discord_admin_client.py` built on the existing
   `smarter_dev/web/discord_rest.py:DiscordBotClient` (httpx, injectable
   transport → easy to test with MockTransport). Needed calls (verify against
   views usage): list bot guilds, get guild, list roles, list channels.
   The harness's mock Discord (`scripts/local_harness/mock_discord.py`)
   currently patches the legacy `DiscordClient.base_url`
   (`harness_app.py`) — repoint the patch at the new client in the same change.
2. **Form handling**: legacy views hand-parse `await request.form()`. Ported
   controllers use `Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)]`
   like `bot_admin_skrift.py:69`, or Skrift forms (`.claude/skills/skrift-forms`)
   for the bigger forms (forum agents, campaigns). Keep parity of accepted
   fields; do not redesign forms in this phase.
3. **Navigation**: one top-level nav entry already exists ("Bot", order 60,
   `bot_admin_skrift.py:38`). Ported pages hang off `/admin/bot/...` and the
   existing overview page gains links; only genuinely separate areas
   (help conversations) get their own `ADMIN_NAV_TAG` route.
4. Each feature = one commit: controller + templates + tests + app.yaml line +
   harness `SKRIFT_ADMIN_PAGES` rows. `/bot-admin` keeps serving throughout.

## Features NOT ported (verified dead or already replaced)

| Legacy | Why not |
| --- | --- |
| `views.py:706-887 api_keys_list/create/delete` (+ `templates/bot-admin/api_keys*.html`) | Replaced by Skrift built-in key admin (phase 01) |
| `views.py:2210-2505 quests_*` | Not registered in `admin/routes.py`; live replacement `quests_admin.py:QuestsAdminController` already in app.yaml:86 |
| `views.py:3949 campaign_signups_list` | Live replacement `campaign_signups_admin.py:CampaignSignupsAdminController` (app.yaml:78) |
| `views.py:60 dashboard` | Superseded by the Skrift admin dashboard + the Bot overview page; fold any unique stats into the overview |

## Port specs (one table row = one commit)

All target controllers live in new files `smarter_dev/web/bot_admin/<feature>.py`
(package with focused modules — `views.py`'s 4000-line monolith is the
anti-pattern), templates in `templates/admin/bot/<feature>/`, tests in
`tests/web/test_admin_<feature>.py` (Litestar test client, happy path per
route + auth-required + non-admin 403 + validation failure).

| # | Feature | Legacy anchor (`admin/views.py`) | Legacy routes (`admin/routes.py`) | Target controller | Target routes (all under `/admin/bot`) |
| --- | --- | --- | --- | --- | --- |
| 1 | Guild list + detail | `guild_list:213`, `guild_detail:238` | `/guilds`, `/guilds/{guild_id}` | `bot_admin/guilds.py:GuildAdminController` | GET `/guilds`, GET `/guilds/{guild_id}` (merge into/extend existing overview at `bot_admin_skrift.py:40`) |
| 2 | Bytes config | `bytes_config:354` (GET+POST) | `/guilds/{guild_id}/bytes` | `bot_admin/bytes_config.py:BytesConfigAdminController` | GET+POST `/guilds/{guild_id}/bytes` |
| 3 | Squads config + sale events | `squads_config:516`; `squad_sale_events_list:2992`, `_edit:3092`, `_toggle:3165`, `_delete:3188` | `/guilds/{gid}/squads` (GET+POST); `/guilds/{gid}/squad-sale-events` (GET+POST), `.../{event_id}/edit|toggle|delete` (POST) | `bot_admin/squads.py:SquadsAdminController` | same paths re-rooted under `/admin/bot` |
| 4 | Forum agents (+analytics/bulk) | `forum_agents_list:1242`, `_create:1288`, `_edit:1395`, `_delete:1552`, `_toggle:1587`, `_analytics:1622`, `get_forum_response_details:1696`, `forum_agents_bulk:1754`, validator `validate_forum_agent_data:1133` | `/guilds/{gid}/forum-agents[...]`, `/api/forum-responses/{response_id}/details` | `bot_admin/forum_agents.py:ForumAgentsAdminController` | same paths; response-details JSON route becomes GET `/forum-responses/{response_id}/details` |
| 5 | Campaigns + challenges | `campaigns_list:1803`, `campaign_create:1855`, `_edit:2024`, `_delete:2178`, `campaign_challenges:2506`, `challenge_create:2555` | `/guilds/{gid}/campaigns[...]`, `.../{campaign_id}/challenges[/create]` | `bot_admin/campaigns.py:CampaignsAdminController` | same paths |
| 6 | Scheduled messages | `scheduled_messages_list:2666`, `_create:2719`, `_edit:2831`, `_delete:2957` | `/guilds/{gid}/campaigns/{cid}/scheduled-messages[...]` | `bot_admin/scheduled_messages.py:ScheduledMessagesAdminController` | same paths |
| 7 | Repeating messages | `repeating_messages_list:3227`, `_create:3341`, `_edit:3449`, `_delete:3555`, `_toggle:3600` | `/guilds/{gid}/repeating-messages[...]` | `bot_admin/repeating_messages.py:RepeatingMessagesAdminController` | same paths |
| 8 | Per-guild configs (audit-log, AoC, attachment-filter) | `audit_log_config:3647`, `advent_of_code_config:3746`, `attachment_filter_config:3844` | `/guilds/{gid}/audit-logs`, `/guilds/{gid}/advent-of-code`, `/guilds/{gid}/attachment-filter` (GET+POST each) | `bot_admin/guild_configs.py:GuildConfigsAdminController` | same paths |
| 9 | Help conversations | `conversations_list:888`, `conversation_detail:982`, `cleanup_expired_conversations:1042` | `/conversations`, `/conversations/cleanup` (BEFORE detail — keep the route-order fix from routes.py:190), `/conversations/{conversation_id}` | `bot_admin/help_conversations.py:HelpConversationsAdminController` (own nav entry, label "Help Conversations") | GET `/help-conversations`, GET+POST `/help-conversations/cleanup`, GET `/help-conversations/{conversation_id:uuid}` — the `:uuid` path param makes the Starlette ordering bug structurally impossible |

Per-feature template mapping: reuse the legacy template's body content
restyled onto the Skrift admin base (`templates/admin/bot/overview.html` shows
the working pattern); source templates listed in `templates/bot-admin/`
(e.g. `bytes_config.html`, `forum_agent_analytics.html`, ...). Copy the
business content, not `base_sidebar.html`/`base.html`/`login.html`.

Behavior-parity notes discovered in the views (preserve):
- `bytes_config`/`squads_config` POSTs redirect back with flash-style state —
  use `skrift.flash` (`flash_success`/`flash_error`, see `api_keys_admin.py:18`
  for import pattern before it's deleted).
- `forum_agent_analytics:1622` aggregates response stats; port the query, add
  a unit test on the aggregation.
- `cleanup_expired_conversations:1042` supports GET (confirm page) + POST
  (execute) — keep both.

## Removal spec (final commit of this phase)

After all 9 features are live and harness-verified:

1. `app.yaml` (and `app.development.yaml`): remove
   `- smarter_dev.web.controllers:bot_admin_mount` (app.yaml:63).
2. `smarter_dev/web/controllers.py`: delete `bot_admin_mount` (:228-234).
3. Delete `smarter_dev/web/admin/` entirely (`__init__.py`, `app.py`,
   `auth.py`, `discord_oauth.py`, `discord.py`, `routes.py`, `views.py`) —
   this **is** the session-6 auth stack removal: `admin_required`
   (auth.py:29), `login`/`logout`/`discord_oauth_callback`, the standalone
   `SessionMiddleware` (app.py:17-23, `settings.web_session_secret`) all go.
   Grep first: nothing outside the package may import `smarter_dev.web.admin`
   (known external toucher: `scripts/local_harness/harness_app.py` patches the
   legacy DiscordClient — fixed in prerequisite 1).
4. Delete `templates/bot-admin/`.
5. Config: `web_session_secret` stays (check other consumers before removing —
   if only the legacy admin used it, remove the setting and the
   `WEB_SESSION_SECRET` env from `k8s/*.yaml`/`compose.yaml` in phase 05).
6. Tests: delete legacy-admin test modules (grep `tests/` for
   `bot-admin`/`smarter_dev.web.admin`).
7. Harness: replace `LEGACY_ADMIN_PAGES` with a single
   `bot-admin-gone` check (`/bot-admin/` → 404) per 06-test-harness.md, drop
   the forged-cookie client (`expectations.py` + runner code), delete the
   mock-Discord patch if the new client covers it via env-configurable base
   URL.
8. Redirect courtesy (optional, decide at implementation): a tiny route
   answering `/bot-admin` → 301 `/admin/bot` for bookmarks.

## Definition of done

- All 9 features usable at `/admin/bot/...` behind
  `Permission("administrator")`, each with tests.
- `/bot-admin` returns 404; `smarter_dev/web/admin/` and
  `templates/bot-admin/` deleted.
- Harness green with `SKRIFT_ADMIN_PAGES` covering every new page and the
  `bot-admin-gone` check; pytest green.
