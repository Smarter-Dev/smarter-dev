# Site memory API

The site backend calls the Python app with `Authorization: Bearer sk_…`.
The key must have `principal_type=service`, `service_name=site`, and explicit
`site-memory` scoped permission; its owner must also have that permission
(`site-service` role). Bot keys, user keys, session cookies, unscoped keys,
and keys scoped only to `site-privacy` receive 401. `site-privacy` is a
separate permission reserved for the site's privacy endpoints.

Keep the service key on the server. The site backend is responsible for
validating the signed-in account's linked Discord IDs and guild access before
forwarding requests. Member IDs and `viewer_discord_ids` are trusted assertions
from that backend, not browser-controlled authorization. Successful memory responses use
`Cache-Control: private, no-store`; never cache personalized responses at the CDN.

All paths start with `/api/site/guilds/{guild_id}/memory`. Guild IDs are
strings of 15–20 ASCII decimal digits, matching the memory table column bound.
Member and viewer IDs use the shared Snowflake range of 15–22 ASCII decimal
digits. Times are ISO 8601, in UTC.

| Method and suffix | Response |
| --- | --- |
| `GET /blocks` | `{guild_id, content, behavior, personality, last_dream_at, revision, memory_enabled}`. Removes lines referring to opted-out members using the shared privacy gate, including known id-less tags. Empty blocks are null. An undreamed guild returns null blocks and metadata; a paused guild returns null blocks and metadata with `memory_enabled: false`. |
| `GET /members/{discord_user_id}?limit=25&before=…` | `{notes: [{id, created_at, channel_name, content, source}], next_before}`. Notes tagging that member only, newest first; legacy `name (id N)` tags and Discord mentions also match. Filters blocked-member lines. `source` is `site` or `discord`. Empty results are 200. |
| `POST /members/{discord_user_id}/notes` | Body `{content}`; 200 `{saved, reason, id, created_at}`. A duplicate today or the existing guild daily cap returns `saved: false` and `reason: duplicate` or `daily_cap`. |
| `GET /public?viewer_discord_ids=111111111111111111,222222222222222222` | `{guild_id, personality, behavior, memory, last_dream_at, revision}`. The same `public_guild_memory`, `check_public_blocks`, and `PublicBlock.shown` used by `/chat-agent`: opted-out lines omitted, other members masked, failing blocks empty, and the viewer's own tagged names restored. Omit IDs for the anonymous view. Returns 404 unless this is the guild currently switched on by the admin. |

`limit` is 1–100. `next_before` is an opaque cursor; pass it unchanged as
`before` for the next page, and stop at null. Ordering is `(created_at, id)`
descending, so tied timestamps neither skip nor repeat notes. Cursors still
work if the dream deletes the anchor note. The `before` cursor is at most 200
characters. Invalid cursors/limits, including
timestamps that cannot be represented in UTC, return 422. The comma-separated
`viewer_discord_ids` query is at most 2300 characters.
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
