# Site memory API

The site backend calls the Python app with `Authorization: Bearer sk_…`.
The key must have `principal_type=service`, `service_name=site`, and explicit
`site-memory` scoped permission; its owner must also have that permission
(`site-service` role). Bot keys, user keys, session cookies, unscoped keys,
and keys scoped only to `site-privacy` receive 401. `site-privacy` is a
separate permission reserved for the site's privacy endpoints.

The site's deletion handoff calls `POST /api/site/privacy/delete-account/{user_id}`
with its `site-privacy` service key and the shared account UUID. A known account
is deactivated and queued for the same durable purge as Python's `/account/delete`;
the response is `202 {"status":"pending"}` until that worker finishes. A later
`404` means the account no longer exists, including after a completed purge,
and the site may mark its D1 request processed. Invalid UUIDs return 400; an
unknown UUID returns 404. Other responses leave the D1 request pending for retry.
The existing purge removes Skrift identity, linked logins, user-owned chat and
attachments, account-linked records, and sudo membership. Discord privacy
preferences are independent of account deletion and remain in force.

Keep the service key on the server. The site backend is responsible for
validating the signed-in account's linked Discord IDs and guild access before
forwarding requests. Member IDs and `viewer_discord_ids` are trusted assertions
from that backend, not browser-controlled authorization. Successful memory responses use
`Cache-Control: private, no-store`; never cache personalized responses at the CDN.

The guild-scoped paths start with `/api/site/guilds/{guild_id}/memory`. Guild IDs are
strings of 15–20 ASCII decimal digits, matching the memory table column bound.
Member and viewer IDs use the shared Snowflake range of 15–22 ASCII decimal
digits. Times are ISO 8601, in UTC.

| Method and suffix | Response |
| --- | --- |
| `GET /blocks` | `{guild_id, content, behavior, personality, last_dream_at, revision, memory_enabled}`. Removes lines referring to opted-out members using the shared privacy gate, including known id-less tags. Empty blocks are null. An undreamed guild returns null blocks and metadata; a paused guild returns null blocks and metadata with `memory_enabled: false`. |
| `GET /members/{discord_user_id}?limit=25&before=…` | `{notes: [{id, created_at, channel_name, content, source}], next_before}`. Notes tagging that member only, newest first; legacy `name (id N)` tags and Discord mentions also match. Filters blocked-member lines. `source` is `site` or `discord`. Empty results are 200. |
| `POST /members/{discord_user_id}/notes` | Body `{content}`; 200 `{saved, reason, id, created_at}`. A duplicate today or the existing guild daily cap returns `saved: false` and `reason: duplicate` or `daily_cap`. |
| `GET /public?viewer_discord_ids=111111111111111111,222222222222222222` | `{guild_id, personality, behavior, memory, last_dream_at, revision}`. The same `public_guild_memory`, `check_public_blocks`, and `PublicBlock.shown` used by `/chat-agent`: opted-out lines omitted, other members masked, failing blocks empty, and the viewer's own tagged names restored. Omit IDs for the anonymous view. Returns 404 unless this is the guild currently switched on by the admin. |

`GET /api/site/memory/public?viewer_discord_ids=…` returns the same public response without a guild ID in the path: `guild_id` identifies the enabled guild, or the endpoint returns 404 `Public memory not found` when none is public; authentication, rate limiting, masking, and cache headers are unchanged.

`limit` is 1–100. `next_before` is an opaque cursor; pass it unchanged as
`before` for the next page, and stop at null. Ordering is `(created_at, id)`
descending, so tied timestamps neither skip nor repeat notes. Cursors still
work if the dream deletes the anchor note. The `before` cursor is at most 200
characters. Invalid cursors/limits, including
timestamps that cannot be represented in UTC, return 422. The comma-separated
`viewer_discord_ids` query is at most 2300 characters.
Each member-note request examines at most 1,000 candidate rows in at most five
batches of 200. If exact-tag or privacy filtering exhausts that budget, the
response includes a cursor after the last examined row, even when `notes` is
empty. Keep paging until `next_before` is null. An exact-budget final batch
may therefore require one final empty request to discover the end.

The public endpoint and `/chat-agent` share a 300-second in-process cache of
checked, masked blocks, keyed by guild and the three block texts. A text change
clears the cache; the public switch is still read on every request. Viewer
names are restored only after cache lookup and are never cached. Cold requests
read the complete known-name history, just as the existing page does: cache
reuse bounds that work's frequency, not its per-refresh size. Changes only to
privacy/name records can take up to 300 seconds to appear on both surfaces.
No history is truncated for masking.

Notes are temporary: the dream consumes them, so this is not an audit archive.

Member read and write return 404 for an opted-out member or any open purge
request, including a failed purge awaiting review. Writes mentioning another
blocked member also return 404. Other invalid bodies/IDs return 422.

A site note uses the existing `chat_agent_memory_notes` row, reserved
`channel_id=site`, `channel_name=site`, and no engagement ID. This reserved
channel identifies its source without a migration. If the content does not
already reference the URL's member, the API prefixes `<@discord_user_id>`;
the complete stored note must fit the existing `MAX_MEMORY_NOTE_CHARS`
(500 characters). Whitespace-only content is rejected. Use `<id:username>`
for named members as the bot does. The existing duplicate rule, daily cap,
privacy gate and nightly dream apply unchanged. A paused guild still accepts
notes, matching the bot's existing behavior.

The controller uses the native API's Redis multi-tier limiter (10 requests
per second, 180 per minute, 2500 per 15 minutes, per key), rate-limit headers,
and 429 with `Retry-After`. It inherits that limiter's fail-open behavior when
Redis is unavailable. No bot routes or dream rewriting behavior change.

To mint the key, use `/admin/api-keys` with a dedicated site service-owner
account holding `site-service`: principal **service**, service name **site**,
explicit scopes **site-memory** and, if needed, **site-privacy**. Store the raw
key only in the site backend's secret store. See the [key runbook](v2/legacy-sunset/runbooks/01-rotate-bot-key.md#site-service-key).

## Site billing API

All `/api/site/billing` routes require the same site service principal boundary
and an explicit **site-billing** scope, plus the owner's `site-service` role.
Older memory/privacy-only keys receive 401. The Worker must render billing
unavailable on missing permission, missing Python account, or upstream failure.
No Polar credentials belong in the Worker.

- `GET /catalog`: `{launched, offerings, offerings_by_role, hacker, founder}`,
  using the shared Polar pricing context. The public catalog resolves
  `sudo_launch=enabled` only; `disabled` and `admin_only` return an empty teaser
  context without contacting Polar. Admin previews remain on the Python site;
  the service key owner's administrator permission never launches public pricing.
- `GET /users/{user_id}/membership`: `{status, plan, expires_at, will_renew,
  portal_available}`. Status is `none`, `active`, or `expired`; plan is `hacker`,
  `founder`, or null. Uses the account page's current non-revoked membership
  selection. Comp memberships have no portal. No customer IDs leave Python.
- `POST /users/{user_id}/checkout`: JSON `{plan_id, success_url, cancel_url}`.
  `plan_id` is the catalog offering ID or its active price ID, never an arbitrary
  Polar product. Both URLs must be absolute and use exactly `SITE_BASE_URL`'s
  origin (scheme, host and port), without credentials or fragments. The launch
  gate is rechecked. The existing checkout helper forwards `cancel_url` as
  Polar's `return_url` back button, and returns `{url}`.
- `POST /users/{user_id}/portal`: returns `{url}` from the existing portal helper;
  return location is `SITE_BASE_URL/account/billing`. Available for expired
  memberships too, independently of the launch flag.

User IDs are shared Skrift account UUIDs, resolved to an active Python user;
unknown/deactivated users return 404. These endpoints do not create users or
memberships. The Worker must use its authenticated user's UUID, never a form ID.
Malformed IDs/return URLs/unknown plans return 400; provider failures return a
sanitized 503. All API responses are private/no-store. Only the anonymous Worker
`/sudo` catalog page may be publicly cached. Existing Python `/sudo`, webhook,
and role-convergence behavior is unchanged. Opening the success page does not
grant membership; webhooks remain authoritative.

Preview checkout requires its configured origin to match Python `SITE_BASE_URL`;
this API deliberately does not widen the redirect allowlist to arbitrary preview
hosts. Rotate the preview service key to include `site-billing` after deploying
the Python API (see `admin-host.md`). No key rotation or production rollout is
part of this change.
