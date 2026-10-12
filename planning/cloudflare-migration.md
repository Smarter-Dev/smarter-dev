# Moving the public site to Cloudflare Workers

Date: 2026-10-12, revised the same day after Zech's direction. Surveyed from
530fcc31 (smarter-dev) and Skrift 0.2.1a3, then checked against origin/main
2bfe4859 for the routes added since (terms, chat-agent page, opt-out,
sign-up deletion).

Direction set by Zech on 2026-10-12:

- The public site moves to a Cloudflare Worker at `smarter.dev`. The current
  Skrift/Litestar app stays at an admin sub-domain for the Discord bot admin,
  the bot API and the bot's worker tier.
- Nothing product-shaped is launched. The pricing page is behind
  `sudo_launch` and off; chat, search and the resources agent have no users.
  The only live public surfaces are the waitlist home page, the resources
  content pages, privacy, the Discord redirect, signups and click tracking,
  plus login and the account pages for the accounts that exist.
- The blog is dropped from the new site. The OAuth2 consent page is not
  needed.
- The Skrift app grows an API that exposes the chat agent's memories to the
  new site.
- Chat comes later as a clean rebuild: one chat, no separate resources chat.
  A decision step (Jev or OpenAI's Decisions API) decides per turn whether
  the resources pipeline runs and pulls cards in. Chat keeps the UI
  generation the resources chat has today (`sdanswer` blocks).
- Live communication is to be decided with Gym courses (heavy LLM) and Lab
  workspaces (terminals, code editing, LLM) in view. The user dashboard is
  a SPA with an always-present AI assistant.

## 1. What exists today

One Python process (`main.py` -> Skrift ASGI app, Litestar) serves everything
on `smarter.dev` through one Traefik ingress. Pages are Jinja templates in
`themes/smarterdev` with vanilla JS; `spa-nav.js` does soft navigation by
asking the server for `data-sk-partial` regions. Live updates reach the
browser over one SSE stream, Skrift's `/notifications/stream`, fanned out
through Redis.

### Live and public: parity required

| Route | Backed by | Port as |
|---|---|---|
| `/` (waitlist; pricing teaser when `sudo_launch` is on) | `home_controller.py` | static page + flag |
| `/resources` + 7 topic pages | `*_data.py`, 7,349 lines of Python literals | export to JSON/MDX once; static |
| `/privacy` (with the AI-assistant opt-out toggle, #92), `/terms` (#93), `/discord`, 404/500 | `privacy_controller.py`, `chat_bot_opt_out.py`, `terms_controller.py` | static prose; the opt-out toggle is a Worker endpoint over `chat_bot_blocked_users` |
| `/chat-agent` public page with a live, masked look at the agent's memory (#103 to #108) | `chat_agent_page_controller.py`, `chat_agent_public.py` | static prose; the memory peek reads the memory API (section 5) with the same masking |
| `/v2/api/campaign-signups` (+ Resend confirmation), `/v2/api/track-click` | small controllers | Worker endpoints |
| `/auth/login`, `/auth/{discord,github,google}/{login,callback}`, email-link account linking, logout | Skrift `AuthController`, theme login page | see section 2 |
| `/account` (handle, bio, timezone), `/account/security` (passkey list/remove, delete account, delete-your-data by kind, delete own campaign sign-ups #91), `/account/billing` | `account_controller.py`, `account_signups.py`, `account_identifiers.py` | Worker pages over the same tables |

### Built but unlaunched: free to redesign

| Surface | Size | Fate |
|---|---|---|
| `/sudo` pricing, Polar checkout, success/cancel, `billing/`, webhooks, daily sweep | ~2k lines | rebuilt on the Worker with the Polar TypeScript SDK; role converge via the Skrift API |
| Chat `/chat*` + `/v2/api/chat/*` (~25 endpoints), `chat.js` 2,605 lines, `chat/jobs.py` 3,785, runtime, documents, subagents, spend | ~16k lines | not ported; replaced by the clean rebuild (section 4) |
| Resources agent `/v2/api/agent/*`, `resources_agent.py` 1,772, `sdanswer.js` | ~3k lines | pipeline and block protocol reused inside the new chat; no standalone surface |
| Search dashboard `/dashboard/*`, `/s/{token}`, OpenSearch | ~2k lines | not ported unless wanted; decide with the dashboard |
| Blog `/blog*`, blogging agent | | dropped from the new site; stays reachable only on the admin host until retired |
| `/oauth/authorize` consent | Skrift | not needed |
| `public_views.py` campaigns views, `templates/scan/*` | | dead code, not registered in `app.yaml` |

### Stays on the Python app

`/admin/*` (Skrift admin, bot admin, usage, chat conversations, flags),
`/api/*` for the bot and the proactive-agent (`sk_` keys), the Skrift worker
tier, crons (retention, handler sweep, dream session), `/storage` and the
media service. This app becomes the bot service with an API, not the site.

## 2. Accounts and sessions

Facts:

- Accounts have no passwords. `skrift.users` holds email, name, picture.
  Identity is `skrift.oauth_accounts (provider, provider_account_id)` for
  Discord, GitHub and Google. Passkey enrollments exist; passkey sign-in is
  off. Entitlements are `user_roles` -> role permissions.
- The session is a client-side encrypted cookie, no server store. Litestar
  `ClientSideSessionBackend`: AES-GCM, key = `sha256(secret_key)`, 12-byte
  random nonce, JSON payload, AAD `{"expires_at": ...}`, base64, chunked
  into `session-0`, `session-1`, ... at 4,032 bytes. `Domain=.smarter.dev`,
  HttpOnly, SameSite=Lax, 30-day rolling. Payload keys: `user_id`,
  `user_name`, `user_email`, `user_picture_url`, `idle_last_seen`, `flash`,
  OAuth state.

Consequences:

1. The Worker reads the same Postgres through Hyperdrive and holds the same
   session secret; it verifies and mints the same cookie with WebCrypto
   AES-GCM. Existing logins keep working on both hosts. No export, no
   re-link.
2. OAuth callbacks are registered as
   `https://smarter.dev/auth/<provider>/callback`. Proxying `/auth/*` from
   the Worker to the Python origin keeps the registrations and the login
   hooks (Discord role converge) unchanged. A Worker-owned login adds a
   second callback URI per provider and calls converge through the API.
3. Users who only ever linked GitHub or Google need those providers kept on.

## 3. Backend: where the site's logic lives

With nothing launched there is no parity debt for chat, search or billing.
What the new site needs from Python is what the bot owns: identity (shared
tables), agent memories, Discord role converge, and the resources catalog
tables. Everything the site product will be (unified chat, dashboard
assistant, Gym, Lab) is interactive and connection-shaped, which is what
Workers, Durable Objects and Sandboxes are built for, and not what the
Python chat runtime (queue job per turn, Redis cancel pub/sub, SSE fan-out)
is shaped for.

Recommendation: **the site gets its own TypeScript backend on Workers; the
Python app stays as the bot service and exposes an API.**

- Site Worker: Hono (or the framework's server routes) for HTTP, Durable
  Objects for per-user and per-conversation state and WebSockets,
  Hyperdrive to the existing Postgres for users, profiles, memberships and
  the resources catalog, Polar SDK for billing, AI Gateway in front of the
  model providers.
- Python app: bot, admin, `/api` for the bot, memory API (section 5),
  `/api/sudo` converge, workers for the Discord agents. Web pods shrink to
  admin + API.
- Schema ownership: `users`, `oauth_accounts`, `user_roles` stay Skrift's.
  Site-owned tables (new chat, Gym, Lab, dashboard) go in a site schema the
  Python app never reads. `user_profiles` and `sudo_memberships` move to
  site ownership; the bot reads memberships through the API if it needs to.

Cost of the alternative (keep the Python chat runtime behind a proxy): the
Worker would still need SSE passthrough, CSRF and entitlement plumbing for a
chat that is about to be rebuilt anyway.

## 4. Chat rebuild

Direction: one chat; a decision step decides per turn whether the resources
pipeline runs; the answer can carry `sdanswer` blocks.

**Decision step.** Two candidates, both plain HTTPS and callable from a
Worker:

- Jev: TypeSafe's classifier model at `api.typesafe.ai`. The bot already
  runs it as the proactive watcher (`smarter_dev/bot/proactive/watcher.py`,
  `JevWatcherRunner`: `boolean_threshold` 0.5, `minimum_confidence` 0.2,
  abstain path). The same predicate shape ("does this turn want curated
  resources?") and the same thresholds carry over.
- OpenAI Decisions API: `POST /v1/decisions`, beta since 2026-10-06, runs on
  gpt-6-luna, three question types (predicate -> probability, choice ->
  option + probabilities, score -> ordered level), $0.10 per million input
  tokens, no output charge.

Recommendation: a provider-neutral `decide()` seam with Jev first, since it
is already paid for and tuned on the bot side, and Decisions as the second
implementation for an A/B on the same traffic. The step runs before the
model turn, in the conversation's Durable Object, and its verdict is stored
on the turn so the decision can be audited.

**Resources pipeline.** Today: reframer -> researcher (`search_resources`,
`read_source` over `resource_*` tables) -> gap-filler (web search) ->
author, each a pydantic-ai stage in `resources_agent.py`. In the rebuild it
becomes a tool path the main turn takes when the decision says yes: the
catalog tools read `resource_*` through Hyperdrive, the web search uses the
Worker's own search provider, and the author step is the main model with
the resources system prompt appended. No separate conversation table.

**UI generation.** The `sdanswer` protocol: at most one fenced JSON block
per answer, top-level `cards` (2 to 4 cards) or `path`, card kinds
`article`, `snippet`, `collection`, `tradeoff`, `prereq`, `gotcha`,
`links`, `hint`, `tip`, `warning`; the prompt in `resources_agent.py`
(lines ~245 to 404) and the renderer in `sdanswer.js` (`renderCards`,
`renderPath`). Port both into one shared package: a zod schema that
validates blocks server-side (today an invalid block is silently dropped),
the prompt section, and the React/Svelte renderer. This is the seam the
dashboard assistant, Gym and Lab also render through.

**Transport for chat** follows section 6: the turn streams over the user's
WebSocket; the turn's text and blocks persist in site tables.

## 5. Agent memory API on the Skrift app

What exists: a three-layer, per-guild memory for the Discord chat agent.

- `chat_agent_guild_memory`: one row per guild with `content` (first-person
  markdown, rewritten nightly), `behavior`, `personality`.
- `chat_agent_memory_notes`: per guild and channel, one note per thing the
  agent chose to keep, with `engagement_id`; people appear as
  `<id:username>` tags.
- `chat_agent_memory_revisions`: a past night's dream output per guild.
- Written by the bot through `POST /api/guilds/{guild_id}/chat-memory/notes`
  and read through `GET /api/guilds/{guild_id}/chat-memory` (bot API guards,
  `sk_` key). Rewritten nightly by `scripts/dream_session.py`. Exempt from
  retention; blocked users (`chat_bot_blocked_users`) are redacted whole;
  purge requests exist.

What the site needs, proposed as new native controllers on the Python app
under a site service key (`principal_type=service`, scoped permission
`site-memory`):

- `GET /api/members/{discord_user_id}/memory?guild_id=`: the guild's three
  blocks plus the notes that tag this user, newest first, with the blocked
  and purge rules applied. The site resolves `discord_user_id` from the
  user's Discord `oauth_accounts` row.
- `POST /api/members/{discord_user_id}/memory/notes`: a note originated by
  the site assistant (source `site`), so what a learner is working on
  reaches the Discord agent's next dream.
- `GET /api/guilds/{guild_id}/memory/blocks`: the three blocks alone, for
  the assistant's system prompt.
- `GET /api/guilds/{guild_id}/memory/public`: what the `/chat-agent` page
  shows today, masked exactly as `chat_agent_public.py` does, gated by the
  per-guild public-page switch in the admin.

The dream session should keep deleting only notes it read; site-originated
notes join the same stream. The admin view at `/admin/bot/.../chat-memory`
already renders this data and is the reference for what the site may show.

## 6. Live communication

Today: SSE from Skrift notifications, Redis fan-out, one stream per page,
chat turn text and document streams and tool chips all ride it.

For the dashboard SPA with an always-present assistant, Gym and Lab:

- **WebSockets to Durable Objects, one per signed-in user session.** The
  Hibernation API keeps clients connected while the object sleeps (idle
  after about 10 s, no duration billing while asleep), wakes on a message,
  and `serializeAttachment` restores per-connection state on wake. Messages
  up to 32 MiB. One socket multiplexes assistant turns, chat streams, Gym
  progress and Lab events, with ordered delivery and a resume cursor on
  reconnect. Cloudflare's Agents SDK is built on exactly this and is the
  candidate base for the assistant object.
- **Lab terminals need WebSockets regardless.** Cloudflare Sandbox SDK
  (Containers and Sandboxes GA since 2026-04-13) gives isolated containers
  with PTY terminals over WebSocket, file APIs, background processes and
  exposed ports from a Worker; one sandbox per Lab workspace, the editor in
  the browser against the file API.
- **SSE stays for one-way, unauthenticated or page-scoped streams** (a
  public shared answer rendering, a progress bar) where a socket is more
  than needed. Workers pass SSE through without buffering.

Recommendation: WebSocket + Durable Object as the primary channel, SSE as a
fallback for read-only pages. Design the message envelope once (channel,
sequence, payload) so chat, Gym and Lab share it.

## 7. Site shape

- `smarter.dev` -> one Worker: static marketing and content pages (home,
  resources, privacy), server routes for signups, tracking, billing,
  account; the dashboard SPA at `/app` served from Worker static assets
  with its API and WebSocket in the same Worker; Durable Objects for
  sessions, conversations, Lab workspaces; Sandboxes for Lab.
- `admin.smarter.dev` -> current k8s app unchanged, plus the memory API.
  Add the host to the ingress; `allowed_redirect_domains` already covers
  `*.smarter.dev`; the cookie domain already covers it.
- Framework: the content half fits Astro; the SPA half wants React (xterm,
  Monaco or CodeMirror, AI SDK UI all have first-class React bindings). One
  Vite SPA under Astro's Cloudflare adapter, or Astro for content plus a
  separate SPA bundle in the same Worker. Zech's call.

## 8. Phases

1. Site skeleton: content pages from exported data, signups, click
   tracking, login (proxied or Worker-owned), account pages reading the
   same tables, cookie read in the Worker. DNS cutover; Python app at
   `admin.smarter.dev`.
2. Site backend: Hyperdrive, site schema, Polar billing and webhooks,
   converge through the Skrift API, the memory API on the Python side,
   dashboard SPA shell with the assistant Durable Object over WebSocket.
3. Chat rebuild: decision step, resources tool path, `sdanswer` package,
   streaming over the session socket.
4. Gym courses, then Lab workspaces on Sandboxes.

## 9. Not verified

- Prod user count and provider mix, CMS page count: a read-only DB Job.
- Whether the session secret may be shared with Cloudflare.
- The OpenAI Decisions API is in beta; pricing and types from its launch
  notes, not from use.

## 10. Decisions since this plan was written (Oct 12 2026)

Zech's calls during phase 1, recorded here so sections 2, 3, 7 and 8 are
read with them. Where they conflict, this section wins.

- **Data: Cloudflare D1, not Hyperdrive to the shared Postgres.** The site
  owns its data in D1 (`users`, `oauth_accounts`, `user_profiles`,
  `campaign_signups`, `tracked_link_counters`; resources content is JSON
  in the repo). The Python app keeps sudo memberships and billing, opt-out
  and deletion, and chat memory, and serves them over the site API. The
  cutover copies the site tables into D1 with ids preserved behind a short
  freeze. Reason: take load off the overloaded managed Postgres.
- **Session: the site mints its own cookie (`sd_session`, host-only).** It
  does not write Skrift's `session-*` cookie. It does *read* it: a user
  signed in on the admin host (`.smarter.dev` cookie) is signed in on the
  site too, resolved to the D1 user by id, then by email, else created.
  This is the permanent single sign-on between the two hosts and also how
  the preview host gets a signed-in user before cutover.
- **Login is Worker-owned** (Discord, GitHub, Google with Skrift's account
  resolution rules), not proxied. The provider apps keep
  `https://smarter.dev/auth/<provider>/callback`, which becomes the
  Worker's at cutover; `https://admin.smarter.dev/auth/<provider>/callback`
  is added for the Python app at cutover. No temporary or dev callbacks.
- **Framework: React Router v7 framework mode on the Cloudflare Vite
  plugin**, SSR throughout (the plugin has no prerender), `/app` as a
  client-rendered route group. One framework for content and dashboard.
- **Preview before cutover.** The Worker ships to `next.smarter.dev`
  (`wrangler deploy --env preview`, its own D1, noindex and robots
  Disallow driven by `SITE_PUBLIC_ORIGIN`). Production `smarter.dev` is
  not touched, and the production Worker and D1 are not deployed or
  migrated, until Zech's explicit go in the cutover task. The site repo is
  `Smarter-Dev/smarter-dev-site`.
- **Secrets** for the preview Worker come from the k8s secret
  `smarter-dev-secrets` (provider client ids and secrets, Resend key, the
  web session secret as `SKRIFT_SESSION_SECRET`); the site service key is
  minted per `docs/admin-host.md`.
- Phase 1 scope is otherwise as section 8 lists; "prerendered" there means
  bundled at build time with edge caching, and "Hyperdrive" in section 3
  and phase 2 reads as D1 plus the site API.
