# Feature Parity Plan: Automated & Command Moderation

**Legacy sources:**
- `beginner.codes-bot/docs/features/auto-mod.md` (`codes-auto-mod`) — the event-driven spam/scam/TLD/invite engine
- `beginner.py-bot/docs/prod-functionality/01-moderation.md` (`py-moderation`) — the `!`-prefix mod-command cog (lookup, ban/kick/purge/mute/warn, history/whois, rejoin alerts, ModAction audit)

**Implementation target:** the smarter-dev handler system wherever possible (per the migration direction), with a small bot-core slice for privileged slash commands.

---

## 1. Overview

This group is core moderation over the message stream plus the manual mod-command suite. The two legacy features are planned together because they share one escalation surface (`delete_message`, `timeout_user`, `ban_user` — all already admin-handler external functions), one audit story (every auto- and manual action lands in the existing `ModerationAction` table via `ModerationActionOperations`), the same staff-exemption/permission-gating needs on message triggers, and the same mod-log-channel posting pattern.

The shape of the port:

- **Auto-mod is almost entirely admin handlers.** Guild-wide admin handlers (`AdminHandler`, `channel_ids = []`) already fire on every guild message, already have `delete_message` / `timeout_user` / cross-channel `send_message`, and already carry per-fire budgets (`admin_budget()`: 5 messages, 25 mod actions, 120s wall clock) plus windowed caps (`ADMIN_FIRES_PER_MIN = 120`). What's missing is *inputs* (author roles/permissions, channel category on the message context; a `message_edit` trigger) and two small emit surfaces (`delete_webhook`, a role-ping rail on `send_message`).
- **The mod-command suite is half superseded.** `/warn` and `/timeout` already exist as slash plugins writing `ModerationAction` rows; the Muted-role system is obsoleted by native timeouts. What remains is: bot-core `/ban`, `/kick`, `/purge` following the `warn.py`/`timeout.py` pattern, and handler-side *read views* (`!history`/`!whois`/`!lookup` re-imagined as handlers in the mod channel) that need metered read functions over `ModerationAction` and member data — the same reads the rejoin alert needs.
- **One audit system.** Manual slash commands write `ModerationAction` with `source="manual"`; the auto-mod handler's actions get recorded with a new `source="handler"`; the moderation triage agent already writes `source="ai"`. A new `mod_action` trigger lets a handler own mod-log-channel formatting for all three.

Note on `docs/EXISTING-FEATURES.md`: it documents an `automod.py` plugin (`AutoModRegexRule`/`AutoModRateLimit`). **No such plugin exists in `smarter_dev/bot/plugins/`** — that section is stale (carried over from the previous codebase). The spam engine below owns rate-limiting outright; nothing overlaps.

---

## 2. Feature disposition table

### codes-auto-mod

| Capability | Disposition | Justification |
|---|---|---|
| Bot and staff exemption gate | handler-extension | Bot messages already never fire triggers (`handler_events.py` `is_human` check); the manage-messages/staff exemption needs `author_role_ids` + `author_has_manage_messages` context fields — script-side gate after that. |
| Edited-message `@everyone`/`@here` catch | handler-extension | No `message_edit` trigger exists; needs a new admin-tier trigger dispatched from `hikari.GuildMessageUpdateEvent`. Effects (reply + delete) exist today. |
| Blocked TLD filter (`.gay`/`.xxx`) | handler-today | Admin handler: string-detect links, `delete_message`, `send_message` in-channel + to a mod-log channel. Exact staff exemption additionally wants the context fields above. |
| Webhook killer | handler-extension | Destroying a leaked webhook is a REST DELETE not in the emitter; needs a new metered admin-only `delete_webhook(url)` external function. |
| Discord invite filter with staff/private-chat exemptions | handler-extension | Effects fully covered today; exemption inputs need `channel_parent_id` + `author_role_ids` context fields. |
| Rolling per-author message buffer | handler-today | Handler memory (16KB JSON, `memory_*` functions) holds a compact ~2-minute pruned buffer; persistence across restarts is an upgrade over legacy in-process state. |
| Message-rate metric (>5 in 5s) | handler-today | Pure computation over the memory buffer. |
| Channel-spread metric (>3 channels in 15s) | handler-today | Pure computation; requires the handler to be a guild-wide admin handler (it is). |
| Duplicate-message metric (len>15) | handler-today | Pure computation over buffer + last-warning timestamp in memory. |
| Everyone/here mention metrics (plain + nitro-scam) | handler-today | `message_content` is already in context; keyword/mention flags computed in-script. |
| Scam-link metric + link parsing helpers | handler-today | Pure string logic (`d*.gift`, `t.me`, excluding `discord.gift`); simple enough for `startswith`/substring if Monty lacks `re`. |
| Soft warning with escalation state (`_warned`) | handler-today | `send_message` + `{member_id: last_warned_iso}` in handler memory, pruned per fire. |
| Auto-timeout escalation (24h + mods ping + forward) | handler-extension | `timeout_user(user_id, 86400)` + cross-channel send exist today; a *reliable, railed* role ping needs the `allowed_mentions` emitter rail (`ping_role_id` param). Forward is approximated by quoting sanitized content. |
| Violation deletion + scam-link webhook logging | handler-today | `delete_message` exists; replace the external `SCAM_LINKS_WEBHOOK` with a `send_message` of sanitized links to a scam-log channel (secrets must not live in scripts — see open questions). |
| Mute re-entrancy / already-timed-out suppression | handler-today | `{member_id: muted_until_iso}` in handler memory; the handler is the only auto-muter so self-maintained state suffices. |
| Dev-alt verbose debug output | drop | Superseded by durable `HandlerRun` records and throttled error notices (`handler_notify.py`). |
| `disallowed-prefixes.txt` dead file | drop | Referenced by nothing in the legacy repo; dead leftover. |

### py-moderation

| Capability | Disposition | Justification |
|---|---|---|
| `!lookup` member search | handler-extension | Becomes a mod-channel admin handler; needs `search_guild_members(query, limit)` read function + `author_has_manage_messages` context gate. |
| `!lookup` rows show each match's top role | handler-extension | Part of the same read function: `search_guild_members` returns `top_role_name` per member, resolved host-side from one `GET /guilds/{gid}/roles` per call (highest-positioned of the member's roles). Handler D renders it per row. |
| `!lookup` "There are N more members who matched" overflow count | handler-extension | Discord's member-search endpoint returns at most the requested limit and no total, so the legacy exact count is unobtainable. Adapted: the read function over-fetches (window of 100) and returns `overflow_count` — exact within the window, a floor ("N+ more") when the window fills. Documented divergence (§3.7). |
| `!ban` (alias `!boop`) | bot-core | Privileged, interactive (DM-first, guards, non-member targets); implement as `/ban` following the `warn.py` pattern, writing `ModerationAction(source="manual")`. |
| `!kick` | bot-core | Same shape as `/ban`; `/kick` slash command with privileged-target guard and audit row. |
| `!purge` — count mode | bot-core | Bulk delete needs `MANAGE_MESSAGES` + channel-history REST paging + interactive confirmation; `/purge` slash command reusing the agent purge logic in `mod_tools.py`. |
| `!purge` — per-user mode with privileged-target guard | bot-core | Same command, optional `user` option; admin-override guard for privileged targets is new logic in the plugin. |
| `!mute` (Muted role + duration) | drop | Superseded by existing `/timeout` (native Discord timeouts, near-identical duration parsing, `ModerationAction` logging). 28-day native cap accepted (see open questions). |
| `!unmute` | drop | Superseded by native timeout removal; `/timeout` does not currently expose removal — add a small `remove` path to the existing plugin rather than porting this command. |
| Persisted scheduled unmute task | drop | Native timeouts expire on their own; no scheduler needed. (If ever needed, the `timer` trigger is the mechanism.) |
| `!warn` | drop | Superseded by existing `/warn` slash command. Verify the departed-user gap (legacy warned users who had left; `/warn` currently rejects non-members). |
| `!history` mod-action log view | handler-extension | Mod-channel admin handler; needs `list_mod_actions(user_id, limit)` read function + `author_has_manage_messages` gate. Data already lives in `ModerationAction`. |
| `!history` depth (50 records) + "Jump To Action" links | handler-extension | Same read function: `list_mod_actions` also returns `channel_id`/`trigger_message_id` (already on the `ModerationAction` row — the `mod_action` trigger in §3.5 exposes both) so the script builds `discord.com/channels/...` jump links; Handler D fetches 50 (matching legacy) but renders newest-first only up to the 2000-char message cap with an "…N older" tail note — legacy used a ~6000-char embed. Documented divergence. |
| `!whois` / `!history` profile card | handler-extension | Same handler; needs `get_member_info(user_id)` read function (works on departed users, `in_guild=False`). |
| Member-rejoin mod alert (`on_member_join`) | handler-extension | Needs the new `member_join` admin trigger + reuses `list_mod_actions`. |
| `ModAction` audit persistence | drop | Superseded by `ModerationAction` + `ModerationActionOperations`. Never port the pickle format; optional one-off import script if legacy history matters. |
| Mod-action-log channel posting | handler-extension | New `mod_action` trigger fired on `ModerationAction` creation; a handler in the mod-log channel owns formatting for manual, AI, and handler actions alike. |
| Role-name permission gating with silent failure | drop | Existing plugins gate on real Discord permissions with ephemeral denials — strictly better. No magic role names, no silent failure. |
| Cog-level error swallowing | drop | Contradicts fail-fast; `handler_notify.py` + plugin logging already do this right. |

---

## 3. Handler-system extensions

Seven extensions, ordered by how many capabilities they unblock. All new triggers and external functions are **admin-tier only** — member `ChannelHandler`s keep exactly today's surface, so `HANDLER_TRIGGER_TYPES` (the member vocabulary) does not change.

### 3.1 Message-trigger context enrichment (no migration)

**What:** three new fields on the `message` trigger context, computed bot-side in `handler_events.py::on_message` before dispatch (all from the gateway cache — no extra REST):

```python
{
    ...existing fields...,
    "author_role_ids": ["644390354157568014", ...],   # str list, [] if member missing
    "author_has_manage_messages": False,               # guild-level MANAGE_MESSAGES or ADMINISTRATOR
    "channel_parent_id": "833078627935322152",         # parent category id or None
}
```

`author_has_manage_messages` is computed with `lightbulb.utils.permissions_for(event.member)` (guild-level; the legacy per-channel permission simulation is deliberately not ported — express "staff" as roles/permission, per the invite-filter plan). Fields are added for *all* message dispatches (member and admin handlers see them; they're inert data).

**Budget/caps:** none — pure context data.

**Lint/judge:** document the new fields in the author prompts (`handler_author.md`, `admin_handler_author.md`) so the author uses them as cheap guards; the judge's `guards_effective` check already rewards exactly this.

**Consumed by:** staff-exemption gate, TLD filter, invite filter, the whole spam engine (skip staff), `!lookup`/`!history` permission gating (via the same fields on the command-message trigger).

### 3.2 `allowed_mentions` rail + `ping_role_id` (no migration)

**What:** today `DiscordEmitter.create_message` sends no `allowed_mentions`, so Discord's default parses **everything** — any handler that echoes user content could ping `@everyone`. Fix and extend in one change:

- `DiscordEmitter.create_message(channel_id, content, ping_role_id=None)` always sends `"allowed_mentions": {"parse": ["users"]}`; when `ping_role_id` is given, `{"parse": ["users"], "roles": [str(ping_role_id)]}`.
- `HandlerExecution._send_message` gains a `ping_role_id=None` parameter, **passed through only when `self.actor` is set** (admin handlers). Standard handlers get the suppressing default and no pass-through.

**Budget/caps:** unchanged — still one `spend_message()` per send.

**Lint/judge:** admin author prompt documents `ping_role_id` as "for mod escalation only"; the judge's `actions_appropriate` category covers ping frequency. Standard-tier prompt is untouched.

**Consumed by:** auto-timeout escalation (mods-role ping); mod-log formatting handler; also hardens every existing handler against mention injection.

### 3.3 `message_edit` trigger (admin-tier; alembic migration)

**What:** new trigger type dispatched from `hikari.GuildMessageUpdateEvent` in `handler_events.py` (human authors only, same `is_human`-equivalent check). Context:

```python
{
    "trigger_type": "message_edit",
    "message_id": "...",
    "message_content": "<new content>",      # what the message says NOW — legacy only checks this
    "old_content": "<old content or ''>",    # best-effort from cache; '' when uncached
    "author_id": "...", "author_name": "...",
    "author_role_ids": [...], "author_has_manage_messages": False,
    "author_account_created_at": "...", "author_joined_at": "...",
    "channel_parent_id": ...,
}
```

**Model/migration:** introduce `ADMIN_HANDLER_TRIGGER_TYPES = HANDLER_TRIGGER_TYPES + ("message_edit", "member_join", "mod_action")` in `models.py`; alembic migration widens `ck_admin_handlers_trigger_type` only (the `channel_handlers` constraint stays as-is). `handler_runs` needs no change.

**Dispatch plumbing:** `ActiveChannelsCache` and the `/handlers/active-channels` + `/handlers/dispatch` endpoints already key on `(guild, trigger_type)` for guild-wide admin handlers — a new trigger string flows through untouched; only the admin authoring path must offer it.

**Caps:** edits are rarer than messages; reuse `HANDLER_FIRES_PER_MIN_MESSAGE`-class ceilings via a new branch in `fires_per_min_for_trigger`. `describe_trigger` gets a branch ("fires on EVERY message edit — high frequency…") so the judge reasons about cadence.

**Consumed by:** edited-message `@everyone` catch (and, later, any edit-based evasion checks the spam engine wants).

### 3.4 `member_join` trigger (admin-tier; same migration as 3.3)

**What:** dispatched from `hikari.MemberCreateEvent`. Guild-scoped like other admin events; there is no triggering channel, so `channel_id` is empty in the fire payload (exactly like time triggers today — `admin_handlers_jobs.py` already handles that) and the script targets channels explicitly via `send_message(content, channel_id=...)`. Context:

```python
{
    "trigger_type": "member_join",
    "user_id": "...", "username": "...",
    "joined_at": "<ISO>", "account_created_at": "<ISO>",  # from snowflake
    "is_pending": True,   # membership screening not yet accepted
}
```

**Caps (join storms):** a raid can produce hundreds of joins/minute. Add `HANDLER_FIRES_PER_MIN_MEMBER_JOIN = 30` in `handler_caps.py` and a branch in `fires_per_min_for_trigger`; fires beyond the window are declined at dispatch (the rejoin alert misses some raiders during a storm — acceptable, the raid itself is a louder signal). `describe_trigger` branch: "fires on every member join; joins can spike hundreds/min during a raid."

**Consumed by:** member-rejoin mod alert. (Also the natural future home for the stale-doc username filtering — out of scope here.)

### 3.5 `mod_action` trigger (admin-tier; same migration as 3.3)

**What:** fired when a `ModerationAction` row is created. Hook point: `ModerationActionOperations.create_action` callers are the web API — after commit, the API layer enqueues admin-handler fires for handlers with `trigger_type == "mod_action"` in that guild (same enqueue path the dispatch endpoint uses). Context mirrors the row:

```python
{
    "trigger_type": "mod_action",
    "action_type": "ban",            # warn | kick | ban | unban | timeout | purge
    "target_user_id": "...", "target_username": "...",
    "moderator_user_id": ..., "moderator_username": ...,   # None for AI/handler actions
    "reason": "...", "duration_seconds": ...,
    "source": "manual",              # ai | manual | audit_log | handler
    "channel_id": ..., "trigger_message_id": ...,
    "created_at": "<ISO>",
}
```

**Loop rail (hard):** a `mod_action`-triggered fire runs with `max_mod_actions=0` in its budget (a `mod_action` handler formats and posts; it never bans). `admin_handlers_jobs.run_admin_handler_fire` sets this when `trigger_type == "mod_action"`. This makes handler-action → audit-row → handler-action loops structurally impossible.

**Consumed by:** mod-action-log channel posting — one authored handler formats *all* actions (manual slash commands, AI triage, auto-mod handler) into the mod-log channel, replacing per-command hardcoded embeds. `mod_monitor`'s existing `response_channel_id` reporting stays as-is; unification is optional later.

### 3.6 `delete_webhook` external function (admin-only; no migration)

**What:** in `handler_runtime.py`, added to the `actor is not None` block:

```python
async def _delete_webhook(self, webhook_url: str) -> bool:
    """DELETE a leaked discord.com/api/webhooks/<id>/<token> URL. False on 404."""
    self.budget.spend_mod_action()
    return await self.actor.delete_webhook(str(webhook_url))
```

`AdminActor.delete_webhook` validates the URL host-side against
`^https://(canary\.|ptb\.)?discord(app)?\.com/api/webhooks/\d+/[\w-]+$`
(reject anything else with `AdminActionError` — the sandbox must never turn this into an arbitrary-URL DELETE), issues the token-authenticated `DELETE /webhooks/{id}/{token}` (no bot auth needed; keep the bot-token client anyway for rate-limit handling), returns `False` on 404 (already dead), `True` on 204.

**Budget/caps:** `spend_mod_action()` — it is a destructive action; the 25-per-fire admin cap comfortably covers a message containing several leaked webhooks.

**Lint/judge:** admin author prompt documents it with the URL constraint; judge `actions_appropriate` guidance: only call on URLs extracted from the triggering message.

**Consumed by:** webhook killer.

### 3.7 Metered read functions: `list_mod_actions`, `get_member_info`, `search_guild_members` (admin-only; no migration)

**What:** three read functions, admin-tier only, sharing one new budget counter.

```python
list_mod_actions(user_id, limit=10) -> list[dict]
# [{action_type, reason, source, moderator_username, duration_seconds,
#   channel_id, trigger_message_id, created_at}, ...]
# newest first; backed by ModerationActionOperations over the worker DB session.
# channel_id / trigger_message_id come straight off the ModerationAction row
# (the same fields the mod_action trigger context in §3.5 exposes; either may be
# None, e.g. actions with no triggering message) so scripts can build legacy-style
# "Jump To Action" links: https://discord.com/channels/{guild_id}/{channel_id}/{trigger_message_id}

get_member_info(user_id) -> dict
# {user_id, username, nickname, joined_at, account_created_at, is_pending,
#  role_ids, role_names, in_guild}
# REST GET /guilds/{gid}/members/{uid}; on 404 falls back to GET /users/{uid}
# with in_guild=False (departed-user support, matching legacy Snowflake fallback)

search_guild_members(query, limit=10) -> dict
# {"members": [{user_id, username, nickname, joined_at, top_role_name}, ...],
#  "overflow_count": N}
# REST GET /guilds/{gid}/members/search?query=...&limit=100 — always over-fetch
# Discord's window, slice to `limit` host-side. top_role_name is resolved
# host-side from one GET /guilds/{gid}/roles per call: the highest-positioned of
# the member's roles, "@everyone" when they have none (legacy showed top role
# per row). overflow_count = matches seen beyond `limit`; exact while the
# 100-fetch window doesn't fill, a FLOOR once it does — Discord's endpoint
# returns no total, so the legacy exact "There are N more members who matched"
# is unobtainable (documented divergence; render as "N+ more" at the floor).
# One spend_lookup covers the search + roles fetch pair.
# NOTE: Discord's endpoint is PREFIX match on username/nick, not the legacy
# substring match — documented divergence; all-digit queries also try get_member_info.
```

**Budget:** new counter on `HandlerBudget`: `max_lookups` / `lookups` with `spend_lookup()`; `DEFAULT_MAX_LOOKUPS = 0` (standard handlers never see these functions anyway), `ADMIN_MAX_LOOKUPS = 10`. Add `lookups` to `usage()` and a nullable-default `lookups` column on `handler_runs` (part of the 3.3 migration).

**Wiring:** `list_mod_actions` needs a DB session in the runtime — inject an async callable into `HandlerExecution` (like `agent_runner`), constructed in `admin_handlers_jobs.py` where the session context lives. The REST reads go on `AdminActor`.

**Lint/judge:** author prompt documents them as mod-channel tools; judge guidance: reads must be behind a cheap guard (a command-prefix match or `member_join`), never on every message.

**Consumed by:** `!lookup`, `!history`, `!whois` replacements; member-rejoin alert.

### 3.8 Handler-action audit writes (cross-cutting; no new surface)

**What:** the admin moderation functions (`ban_user`, `kick_user`, `timeout_user`, `delete_message`, `delete_webhook`) start writing `ModerationAction` rows with `source="handler"`, so auto-mod escalations show up in `!history`, the rejoin alert, and the web mod dashboard alongside manual and AI actions. Implementation: `HandlerExecution` gets an optional async `audit` callable (injected from `admin_handlers_jobs.py`, wrapping `ModerationActionOperations.create_action`); each `_ban_user`/`_timeout_user`/`_kick_user` calls it after the REST call succeeds. `delete_message`/`delete_webhook` are recorded only when part of an escalation is too noisy — record ban/kick/timeout only (deletes stay visible in `HandlerRun.mod_actions`).

Requires widening the `ModerationAction.source` doc/values to include `"handler"` (no schema change — plain `String(20)`).

**Consumed by:** unified audit story; the `mod_action` trigger then makes auto-mod actions appear in the mod-log channel for free.

---

## 4. Per-feature plans

### 4.1 codes-auto-mod → three guild-wide admin handlers

All authored via the existing admin pipeline (author → lint → dual judge). Hardcoded legacy IDs become **handler memory config**: the admin seeds them at authoring time ("use channel #mod-log for logs") and the author bakes them in as script literals, or the script reads them from `memory_get("config")` so they're editable by re-authoring. The admin author agent already has a `list_channels` tool to resolve names → ids. Legacy IDs map to smarter-dev-guild equivalents chosen at authoring time (the six beginner.codes IDs are not ported).

All three handlers open with the same cheap gate (satisfying the exemption capability):

```python
if context["author_has_manage_messages"]:
    ...skip (return-equivalent: wrap logic in the else branch)...
```

**Handler A — `content-filters` (trigger: `message`, all channels).** TLD filter + invite filter + webhook killer in one handler (one fire per message instead of three):

1. Extract links from `message_content` (substring scan; `re` only if Monty exposes it — open question, patterns are simple enough for `split`/`endswith`).
2. Blocked TLD (`.gay`, `.xxx` — list in memory config): `delete_message(context["message_id"])`, `send_message` in-channel telling the author (mention by id — no reply-to in the emitter, acceptable), `send_message(log_line, channel_id=modlog_channel)` with author, links, channel, content (truncated ~800 chars, links sanitized with spaces).
3. Leaked webhook URLs: `delete_webhook(url)` per match, then one in-channel `send_message` explaining what was killed.
4. `discord.gg/` invite: exempt when `context["channel_parent_id"] == private_chat_category` or any staff role in `context["author_role_ids"]` or `author_has_manage_messages` (the legacy "can send in staff channel" check is re-expressed as "has a staff role"); otherwise delete + in-channel notice + mod-log line.

Budget check: worst case ≈ 3 deletes/webhook-kills + 3 messages — inside `ADMIN_MAX_MESSAGES = 5` and `ADMIN_MAX_MOD_ACTIONS = 25`. Fires on every message but is pure string logic with no agent/web spend.

**Handler B — `spam-engine` (trigger: `message`, all channels).** The rolling buffer plus all six metrics, warning state, mute state:

Memory layout (all pruned every fire; budgeted to stay far under 16KB):

```python
buffer: [[ts_epoch_int, author_id, channel_id, content_hash_or_head, flags], ...]
        # flags: bitmask-ish small ints for everyone/here, nitro/gift keyword, scam link
        # pruned to entries newer than 120s AND max ~150 entries (hard cap)
warned: {author_id: last_warned_epoch}     # pruned > 2 min
muting: {author_id: muted_until_epoch}     # pruned when expired
```

Per fire: skip if staff (gate above) or `author_id` in `muting` and not expired (re-entrancy capability); append entry; compute metrics for this author only — >5 msgs/5s, >3 distinct channels/15s, >1 duplicate of len>15 since last warning (or 60s), any everyone/here mention in 15s, nitro-scam variant, scam link (`https://d…gift/`, `https://t.me/`, excluding `discord.gift`). Then:

- **Mute path** (warned <2 min ago, or nitro-scam, or scam link): `timeout_user(author_id, 86400)`; `send_message(f"...", channel_id=mod_channel, ping_role_id=mods_role)` with a jump link built as `https://discord.com/channels/{guild}/{channel}/{message_id}` and the sanitized offending content (first ~900 chars) quoted in the same message (native "forward" not needed); set `muting[author_id]`.
- **Warn path**: `send_message("Please stop " + ", ".join(reasons))` in the offending channel; set `warned[author_id]`.
- **Deletion**: if the everyone-mention or scam-link metric fired, `delete_message` the offender and `send_message` the sanitized links to the scam-log channel (replacing `SCAM_LINKS_WEBHOOK` — open question if the external feed must survive).

The mute/kick/ban get audited automatically via extension 3.8 (`source="handler"`, reason "Auto-moderation violation"), which is an upgrade over legacy (no audit at all).

Sizing: 150 entries × ~5 compact fields ≈ 6–8KB serialized; warned/muting maps are tiny. The judge's `memory_bounded` category is exactly this check — the authoring request must state the prune rules explicitly.

**Handler C — `edit-ping-catch` (trigger: `message_edit`, all channels).** If `"@everyone" in message_content or "@here" in message_content` (and author not staff): `send_message` telling the author not to mention everyone, `delete_message(context["message_id"])`. Three lines of logic on the new trigger.

**Dropped from codes-auto-mod:** dev-alt debug output (superseded by `HandlerRun` records), `disallowed-prefixes.txt` (dead).

### 4.2 py-moderation → bot-core slash commands + mod-channel handlers

**Prefix commands are not ported verbatim.** Manual privileged actions become slash commands (matching `/warn`, `/timeout`); read-only views become admin handlers installed in the mod channel that respond to plain-text commands there (keeping them conversationally re-authorable — a mod can ask for a different card layout without a deploy).

**Bot-core: `/ban` (plugins/ban.py).** Follows `warn.py` exactly: `BAN_MEMBERS` permission gate with ephemeral denial; required `user` + `reason` options; refuse targets with `MODERATE_MEMBERS`/`ADMINISTRATOR` (the modernization of the legacy manage-messages guard); DM-first with "*Unable to DM user*" appended to the audit reason on `Forbidden`; `rest.ban_user(guild, user, reason=..., delete_message_seconds=0)` — hikari's `USER` option resolves by ID so non-member bans work (legacy digit-run parsing is obsolete); confirmation embed; `ModerationAction(action_type="ban", source="manual")`. No `!boop` alias.

**Bot-core: `/kick` (plugins/kick.py).** Same skeleton with `KICK_MEMBERS`; target must be a current member (ephemeral error otherwise); `action_type="kick"`.

**Bot-core: `/purge` (plugins/purge.py).** Options: `count` (int, 1–100 — the legacy >1000-means-user-ID magic is replaced by an explicit optional `user` option), optional `user`. Gate on `MANAGE_MESSAGES`. Per-user mode keeps the legacy guard: cannot purge a `MANAGE_MESSAGES` holder's messages unless the invoker has `ADMINISTRATOR`; bots exempt. Reuse the paging/bulk-delete logic from `mod_tools.py::purge_messages` (extract a shared pure helper rather than duplicating; note bulk-delete rejects messages >14 days old — filter and report the shortfall instead of failing). Ephemeral confirmation (replaces the legacy 15s self-deleting message). Audit row `action_type="purge"` with `reason=f"purged {n} messages in #channel"` — which also flows to the mod-log via the `mod_action` trigger.

**Bot-core (small): `/timeout remove`.** Add a `remove` duration keyword (or subcommand) to the existing `timeout.py` that PATCHes `communication_disabled_until: null` and records `action_type="untimeout"` — this is the `!unmute` replacement.

**Handler D — `mod-lookup` (trigger: `message`, scoped to the mod channel(s) via `channel_ids`).** Replaces `!lookup`/`!history`/`!whois` with plain-text commands in the mod channel:

- Guard: message starts with `!lookup `, `!history `, or `!whois ` (cheap prefix guard → judge-friendly); else do nothing.
- `!lookup <text>`: `search_guild_members(text, 15)`; format count + rows (name, nickname, joined, top role, id — matching the legacy row shape; `top_role_name` comes from §3.7); when `overflow_count > 0` append "There are N more members who matched" — rendered "N+ more" when the fetch window filled, since the exact total is a floor (§3.7); note prefix-match semantics.
- `!whois <id-or-mention>` (digits extracted in-script): `get_member_info(id)` → profile card: joined/humanized or "NO LONGER A MEMBER" (`in_guild=False`), Accepted Rules from `is_pending`, sus-role flag from `role_names` (guild flavor, trivially expressed in-script), user id.
- `!history <id>`: profile card + — only when `context["author_has_manage_messages"]` — `list_mod_actions(id, 50)` (legacy depth) formatted newest-first (type, date, reason, source), each row carrying a "Jump To Action" link built as `https://discord.com/channels/{context["guild_id"]}/{channel_id}/{trigger_message_id}` when both fields are non-null (rows without a triggering message render linkless — legacy behaved the same when the pickled link was absent). Without the permission it behaves as `!whois`, matching legacy. Rendering divergence: legacy packed up to 50 rows into a ~6000-char embed; the emitter caps plain messages at 2000 chars, so the script stops rendering before the cap and appends "…and N older actions" for the remainder.

Since the handler is `channel_ids`-scoped to the mod channel and gated on the permission field, casual members never reach the read functions. Output >2000 chars must be truncated in-script (the emitter truncates silently at 2000).

**Handler E — `rejoin-alert` (trigger: `member_join`, guild-wide).** `actions = list_mod_actions(context["user_id"], 5)`; if non-empty, `send_message` to the mod-log channel: "Member Rejoined — {username} ({user_id}) has {n}+ prior mod actions, most recent: {type} on {date}". One lookup + at most one message per join; the join-storm fire cap bounds the rest.

**Handler F — `mod-log-formatter` (trigger: `mod_action`, guild-wide).** Formats every audit row into the mod-log channel: action, target, moderator (or "auto-mod"/"AI"), reason, duration, source. Replaces the legacy per-command embed posting and covers `/ban`, `/kick`, `/purge`, `/warn`, `/timeout`, AI triage, and the spam engine uniformly. Runs with `max_mod_actions=0` (hard rail, §3.5). Optional skip: `if context["source"] == "handler" and context["action_type"] == "timeout"` when the spam engine already posted its own escalation ping, to avoid double-posting — decided at authoring time.

**Dropped from py-moderation:** `!mute`/`!unmute`/scheduled unmute (native timeouts + `/timeout remove`), `!warn` (existing `/warn`), `ModAction` pickle table (existing `ModerationAction`), role-name gates with silent failure, cog error swallowing.

---

## 5. Implementation order & TDD notes

TDD throughout: write the failing test first; happy paths + the listed failure paths. Handler *scripts* are testable end-to-end by running `run_handler_script` with a fake emitter/actor/limiter (the runtime already supports full injection) — every authored-handler sketch above gets a script-level test before it's installed in prod.

**Phase 1 — bot-core commands (independent, immediate value).**
`/ban`, `/kick`, `/purge`, `/timeout remove`.
Tests first: permission-denied ephemeral; privileged-target refusal; DM-failure path appends the note and still bans; non-member ban succeeds / non-member kick errors; purge per-user admin-override guard; >14-day-old message filtering; every command writes the correct `ModerationAction` row (`source="manual"`). Critical failure paths: REST 403/404 mid-action (fail loud with ephemeral error, do **not** write the audit row for an action that didn't happen), DB write failure after a successful ban (log + surface, never swallow).

**Phase 2 — context enrichment + mention rail (small, unblocks everything).**
§3.1 fields in `handler_events.py`; §3.2 emitter/runtime change.
Tests: context fields present/absent-member fallbacks; `permissions_for` admin implies manage-messages; emitter payload always carries `allowed_mentions`; `ping_role_id` rejected (ignored/raises) for standard handlers; existing emitter tests updated for the new default. Failure path: member missing from cache → `author_role_ids=[]`, `author_has_manage_messages=False` (fail closed on exemption = the message gets scanned; never fail open).

**Phase 3 — auto-mod handlers A and B (handler-today once Phase 2 lands).**
Author `content-filters` and `spam-engine` via the admin pipeline.
Tests (script-level, fake emitter/actor): TLD hit → delete + 2 sends; staff exemption skips; invite in private category passes; each spam metric at threshold−1/threshold/threshold+1; warn→repeat-within-2-min→mute escalation; nitro-scam immediate mute; scam-link delete + scam-log post; muting-state suppression; **memory pruning under load** (simulate 500 fires, assert serialized memory < 12KB and no `memory_size` cap breach); budget worst-case (multi-violation message stays under caps). Critical failure path: `delete_message` 404 (already deleted) must not abort the remaining logic — catch narrowly in-script or verify `AdminActor` behavior and decide (see open questions on 404 tolerance).

**Phase 4 — new external functions (§3.6, §3.7, §3.8) + budget counter.**
`delete_webhook` URL validation (reject non-Discord hosts, path traversal, missing token → `AdminActionError`); 404 → `False`; `spend_lookup` cap breach raises `CapExceeded("lookups", ...)`; `list_mod_actions` ordering/limit and rows carrying `channel_id`/`trigger_message_id` (None passed through, not dropped); `get_member_info` 404 → `users/{id}` fallback with `in_guild=False`; `search_guild_members` empty result; `search_guild_members` `overflow_count` exact below the fetch window / floor when the window fills; top-role resolution (highest position wins, `@everyone` fallback for roleless members); audit callable writes `source="handler"` rows for ban/kick/timeout and is **not** called when the REST call fails. Alembic migration for the `handler_runs.lookups` column.

**Phase 5 — new triggers (§3.3–3.5) + migration.**
Alembic: widen `ck_admin_handlers_trigger_type`; add `lookups` if not done in Phase 4. Bot dispatch for `GuildMessageUpdateEvent` / `MemberCreateEvent`; API-layer enqueue for `mod_action`; `fires_per_min_for_trigger` and `describe_trigger` branches; admin author/judge prompt updates; `admin_handlers_jobs` sets `max_mod_actions=0` for `mod_action` fires.
Tests: dispatch fires only for admin handlers with the matching trigger; bot-authored edits ignored; uncached edit → `old_content=""`; join-storm cap declines fire #31 in the window; **`mod_action` loop rail** — a `mod_action`-triggered script calling `timeout_user` breaches `CapExceeded("mod_actions")` immediately; creating a `ModerationAction` from a `mod_action`-triggered fire is impossible by construction (assert no enqueue recursion in the API test).

**Phase 6 — handlers C, D, E, F.**
Script-level tests as in Phase 3: `!history` permission branching, departed-user card, rejoin alert only on prior history, formatter output truncation at 2000 chars, `!history` jump links (rendered when both `channel_id` and `trigger_message_id` present, omitted when either is None) and the pre-cap rendering stop with the "…and N older actions" tail, `!lookup` top-role column and overflow line ("N more" vs "N+ more" at the fetch-window floor).

**Cross-cutting risk to design for in Phase 3 (flagging now):** the spam engine does a read-modify-write of handler memory on every guild message; two concurrent worker fires can interleave (last-writer-wins loses buffer entries — `admin_handlers_jobs` loads memory before running and persists after). Mitigation options: (a) serialize fires per handler id (Redis lock keyed `handler:{id}` around the job body — simplest), (b) accept occasional lost entries (legacy lost *everything* on restart; the metrics are heuristics). Recommend (a) for the spam engine since warning/mute state races cause double-pings, the exact thing `_muting` existed to prevent. Test: two concurrent simulated fires never double-`timeout_user` the same author.

---

## 6. Open questions / drop recommendations

### Needs Zech's decision

1. **`SCAM_LINKS_WEBHOOK` external feed.** Plan replaces it with a scam-log channel post (keeps secrets out of scripts). If the external webhook has a live cross-server consumer, we need a `post_webhook`-style metered function with host-side secret storage — not designed here. **Recommend: drop the webhook, use a channel.**
2. **Monty regex surface.** If the sandbox exposes `re`, the legacy patterns port directly; if not, the TLD/invite/scam patterns are simple enough for `split`/`endswith`/`in` logic (Handler A/B sketches assume string ops). Verify during Phase 3 authoring; no design change either way.
3. **Legacy `ModAction` history import.** A one-off script unpickling legacy rows into `ModerationAction` JSON would let `!history` and the rejoin alert see pre-migration history. Cheap but optional. **Recommend: only if the beginner.py guild's history is still operationally useful.**
4. **28-day native timeout cap.** Dropping Muted-role mutes means no mute longer than 28 days; longer punishments become bans. **Recommend: accept.**
5. **`/warn` for departed users.** Legacy `!warn` worked on users who had left; current `/warn` rejects non-members. Warning someone who can't see the warning is mostly an audit note — if wanted, it's a small change to `warn.py` (skip the member fetch, record the row, note "user not in guild"). **Recommend: skip unless mods ask.**
6. **404 tolerance on `delete_message`.** Legacy ignored `NotFound` (message already gone). Decide whether `AdminActor.delete_message` should swallow 404 and return a "already deleted" result (recommended — it's the common race with other mods/bots) or whether scripts handle the error. Affects Phase 3 tests.
7. **Spam-engine memory vs. a windowed-counter primitive.** The 16KB memory cap fits the pruned buffer today (§4.1 sizing), but a very busy guild could squeeze it. If it becomes a problem, the escape hatch is a host-side windowed per-author counter (Redis, like `handler_caps`) exposed as a read function — deferred until measured.
8. **`mod_action` formatter vs. per-command embeds.** The plan centralizes mod-log posting in Handler F. Alternative: bake embeds into each slash command and skip extension 3.5 entirely (less new surface, less flexible). **Recommend: the trigger** — it also covers AI and handler actions, which per-command embeds never will.

### Drop recommendations (final call is Zech's)

| Item | Rationale |
|---|---|
| Dev-alt verbose debug output | Superseded by durable `HandlerRun` records + throttled error notices; a metrics-dump `send_message` gated on an author id is authorable in minutes if ever wanted. |
| `disallowed-prefixes.txt` | Dead file, referenced by nothing in the legacy repo. |
| `!mute` / `!unmute` / persisted unmute scheduler | Superseded by `/timeout` + native expiry; `!unmute` becomes the small `/timeout remove` addition. |
| `!warn` | Superseded by existing `/warn` (modulo open question 5). |
| `ModAction` pickle table | Superseded by `ModerationAction`; the pickle format is a security hazard and is not ported under any outcome. |
| Role-name permission gates with silent failure | Existing permission-based gates with ephemeral denials are strictly better; silent failure hides mod tooling breakage. |
| `cog_command_error` swallowing | Contradicts fail-fast; existing logging + `handler_notify` already handle errors correctly. |
