# Feature Parity Plan: Staff Communication Channels

**Legacy sources:**
- `beginner.codes-bot/docs/features/dm-relay.md` (`extensions/dm_monitor.py` + `extensions/message_binding.py`)
- `beginner.codes-bot/docs/features/mod-chat.md` (`extensions/private_chat.py`)

**Target:** the agentic handler system (`smarter_dev/web/handler_runtime.py` and friends), per the "lean hard on handlers" directive.

## 1. Overview

This group covers the two legacy features that are staff-facing *communication* tooling rather than enforcement:

1. **DM Relay** — a two-way bridge between users' DMs and staff: inbound DMs are mirrored into a staff log channel (with a one-time "you are monitored" notice to the sender), and staff reply outward with `!!!<message>` / retarget with `!bind` from a designated binding channel.
2. **Mod Chat** — private moderator discussion threads under a configured parent channel: `!modchat` creates a dated private thread and announces it, `!archive` and `!lock` close threads, with `!lock` additionally evicting non-mods.

They are planned together because they share one design surface: **sending outside the handler's home channel** (cross-channel messages, DMs, thread posts), **default mention suppression** on everything the emitter sends, **staff gating** for command-style handlers, and **guild-scoped shared state** for config that more than one handler reads. Planning them separately would produce two ad-hoc versions of the same cap/emit design.

### What already exists (reality check against the code)

The prior per-feature analyses assumed more gaps than actually exist. As of today:

- **Admin handlers already send cross-channel.** `HandlerExecution._send_message(content, channel_id=None)` lets an admin handler (actor set) post to *any* channel in the guild, and the windowed cap is metered against the **destination** channel (`channel_message_key(target)`). Standard handlers raise `CapExceeded("cross_channel_send")`. So "send_channel_message" is not a new extension — the mod-chat announcement and the channel-bind relay are covered by authoring these as **admin handlers**. One caveat: `_send_message` raises on any 4xx (`discord_rest.py` `_request`), so the channel-bind relay's ❌-on-unresolvable-target behavior needs E2's companion failure-semantics change (rows 5/11).
- **Admin handlers are guild-scoped with a channel-list.** `AdminHandler.channel_ids` (empty = all channels) plus the `guild_triggers` path in the bot's `ActiveChannelsCache` means a staff-command handler can be scoped to exactly the private staff channel(s) where it should listen.
- **Admin handlers are the trust tier we need.** They are created only through the admin slash command, reviewed by dual judges, and carry a mod-action budget. Every capability in this group is staff tooling — **all of it should be authored at the admin tier**, which also resolves most of the permission-gating question (see §4).
- **Handler memory does not evict — it fails loud.** `HandlerMemory.set` raises `CapExceeded("memory_size")` at 16KB. The prior analysis worried about eviction re-sending the "you are monitored" notice; the real failure mode is worse (the handler starts erroring), so per-user flags must be stored as a **bounded pruned list**, not an unbounded dict.
- **The emitter has no mention controls.** `DiscordEmitter.create_message` suppresses link-preview embeds only; a handler that echoes `@everyone` or a role mention would ping. That gap is real and this group closes it globally.

What is genuinely missing: a **DM trigger**, a **DM send**, **thread operations**, **thread-aware message dispatch**, **mention/role/permission context fields**, and **cross-handler shared state**. Those are the extensions in §3.

## 2. Feature disposition table

Every capability from the prior analyses, one row each.

| # | Capability | Source | Disposition | Justification |
|---|---|---|---|---|
| 1 | Inbound DM mirroring to staff log channel | dm-relay | **handler-extension** | Needs the new `dm_message` trigger (E1); the mirror post itself is admin cross-channel `send_message`, which exists. |
| 2 | Auto-bind reply target to latest DM author | dm-relay | **handler-extension** | The DM-mirror handler and the relay handler are different handler rows; needs guild-scoped shared memory (E4). Fallback: drop auto-bind, mirror embeds a paste-ready `!bind` line. |
| 3 | One-time "you are monitored" DM notice | dm-relay | **handler-extension** | Needs `send_dm` (E2); the seen-flag is per-handler memory today, stored as a bounded pruned list (16KB cap raises, doesn't evict). |
| 4 | `!set dm logging channel` command | dm-relay | **drop** | Superseded by handler placement: the log channel is baked into the mirror handler at authoring time (the admin author agent already has a channel-listing tool). Re-author to move it. |
| 5 | `!!!<message>` relay command | dm-relay | **handler-extension** | Member-target relay needs `send_dm` (E2) with boolean failure return; channel-target relay is admin cross-channel send *mechanically*, but legacy `send()` returns `False` (→ ❌) on an unresolvable channel too — today `send_message` → `DiscordEmitter.create_message` → `_request` raises on any 4xx, so a stale/deleted bound channel would error the fire. Needs E2's companion change (explicit-target `send_message` returns `False` on 403/404). 📤/❌ reactions exist today. |
| 6 | Staff permission gating on relay commands | dm-relay | **handler-today** | Author as an admin handler scoped to the private staff binding channel: only admins can install it, only staff can post where it listens. E7 (`author_role_ids`) is an optional refinement, not a blocker. |
| 7 | `!bind` target selection command | dm-relay | **handler-today** | Mention parsing from `message_content` is string work in Monty; the target tuple lives in the relay handler's own memory (or E4 guild memory once auto-bind lands). **Not pure parity:** legacy `!bind` was `ban_members`-gated with *no* channel restriction (only `!!!` was bound to the binding channel); scoping Handler B narrows `!bind` to the relay channel — a deliberate drop of guild-wide availability, listed in §6. |
| 8 | `!set binding channel` command | dm-relay | **drop** | Superseded by `AdminHandler.channel_ids` scoping — the binding channel IS the handler's scope, set at authoring time. |
| 9 | Relay attribution footer + mention suppression | dm-relay | **handler-extension** | Footer is pure string formatting in the script (today); suppression must live in the emitter (E3) since scripts can't shape REST payloads. |
| 10 | Transient "Sent to <mention>" 2s auto-delete confirmation | dm-relay | **drop** | Redundant with the 📤 reaction (supported today). No sleep/delayed-delete primitive exists and a 2-second toast doesn't justify one. A *permanent* audit line is handler-today if wanted. |
| 11 | Relay failure handling (unresolvable target → ❌) | dm-relay | **handler-extension** | E2's design: `send_dm` returns `False` on 403/404 (expected outcomes) instead of raising, so the script can branch to ❌ without the fire erroring through `handler_notify`. Same convention applied to the **channel** branch via E2's companion change: `send_message` with an explicit `channel_id` returns `False` on 403/404, so ❌ covers both target kinds — legacy `send()` returned `False` for *any* unresolvable bound target. |
| 12 | Relay state persistence model (legacy labels → smarter-dev state) | dm-relay | **handler-today** | Channel-id labels → handler placement/scope (no state); bind target → handler or guild memory; warned flags → bounded list in the mirror handler's memory. No new models beyond E1/E4 migrations. |
| 13 | `!modchat` — create private mod thread | mod-chat | **handler-extension** | Needs the thread emit family (E5: `create_private_thread`, `add_thread_member`); the announcement to the mod channel is admin cross-channel send (exists). Mentioned-user parsing is string work today (E7 optional). |
| 14 | `!archive` — archive a mod-chat thread | mod-chat | **handler-extension** | Needs thread-aware message dispatch (E6) so a command typed *inside* a thread fires the handler, plus `edit_thread` (E5), plus an E7 `author_role_ids` gate — legacy checks `kick_members` per invoker, and thread membership is not a permission (`!modchat` adds every mentioned user, see row 19). |
| 15 | `!lock` — evict non-mods and close thread | mod-chat | **handler-extension** | Needs `get_thread_members` + `remove_thread_member` + `edit_thread` (E5), E6 dispatch, and the same E7 invoker gate as row 14; loops are bounded by the new thread-ops budget counter. |
| 16 | `!set modchat channel` command | mod-chat | **drop** | Config is set at authoring time: the parent channel id is seeded into the handler's memory (or a script constant) during the authoring conversation. Re-author to change it. |
| 17 | `!set mod role` command | mod-chat | **drop** | Same authoring-time config model; additionally the Guild model / web admin already carry a moderator-role concept — reconcile, don't duplicate a chat setter. |
| 18 | Guild-label persistence (mod-chat-channel / mod-role / mod_channel_id) | mod-chat | **handler-today** | All five mod-chat commands live in ONE admin handler, so its own `memory_*` store holds all three ids. Guild memory (E4) only if the commands are ever split across handlers. |
| 19 | Permission gating (kick_members / administrator) | mod-chat | **handler-extension** | For `!modchat`, admin-tier authoring + channel scoping suffices. For `!archive`/`!lock` it does **not**: the typing location is the *thread*, E6 deliberately dispatches any thread member's message to the parent-scoped handler, and `!modchat` adds every mentioned user (legacy allowed mentioning non-mods because each command was independently `kick_members`-checked) — so without more, the effective gate degrades to "is a thread member", strictly weaker than legacy. The in-thread commands therefore require an E7 `author_role_ids` mod-role gate in the script; E7 is **load-bearing** for rows 14/15, not an optional refinement. The kick_members-vs-administrator split disappears with the dropped `!set` commands. |
| 20 | New-thread announcement to mod channel | mod-chat | **handler-today** | Admin `send_message(content, channel_id)` already exists and already meters the destination channel's window. Requires only that the feature is an admin handler (it is). |
| 21 | No-ping mod-role mention policy in intro message | mod-chat | **handler-today** | Script simply includes the role mention as text; E3 makes this structural (role pings suppressed by default at the emitter), so even a careless edit can't re-enable the ping. |
| 22 | Unconfigured-state inertness | mod-chat | **handler-today** | Script checks `memory_get("mod_chat_channel_id")` etc. and replies with a clear "not configured" message — fail fast, no partial execution. |

## 3. Handler-system extensions

Seven extensions, ordered by how many capabilities consume them. All new emit functions follow the existing pattern: metered against the per-fire `HandlerBudget`, checked against a Redis `WindowedLimiter` window, exposed to Monty via `HandlerExecution.external_functions()`, and (for admin-only functions) gated on `self.actor is not None` exactly like `delete_message`/`ban_user` today.

### E3 — Default mention suppression on the emitter

*Smallest, ships first, benefits every handler in the system.*

- **What:** `DiscordEmitter.create_message` (and every new send in this plan) includes an `allowed_mentions` field: `{"parse": ["users"]}` — user pings work, `@everyone`/`@here`/role pings are dead by default. Handler output structurally cannot mass-ping.
- **Design:**
  ```python
  _ALLOWED_MENTIONS = {"parse": ["users"]}  # no everyone/here/role pings, ever
  payload = {"content": content[:_MESSAGE_MAX], "flags": _SUPPRESS_EMBEDS,
             "allowed_mentions": _ALLOWED_MENTIONS}
  ```
- **Budget/caps:** none — payload shaping only.
- **Lint/judge:** author prompts note that role/everyone mentions render as text and never ping; the judge's `actions_appropriate` check no longer needs to hunt for ping abuse in content.
- **Migration:** none.
- **Consumed by:** rows 9, 21 directly; every handler indirectly.
- **Risk:** near zero. No existing handler legitimately mass-pings (audit `channel_handlers`/`admin_handlers` scripts before shipping to confirm).

### E5 — Thread operations emit family (admin-only)

- **What:** five external functions on `HandlerExecution`, present only when `self.actor` is set, backed by new `AdminActor`/emitter REST methods:
  ```python
  create_private_thread(parent_channel_id, name, auto_archive_minutes=4320) -> str   # thread id
  edit_thread(thread_id, name=None, archived=None, locked=None) -> bool
  add_thread_member(thread_id, user_id) -> bool
  remove_thread_member(thread_id, user_id) -> bool
  get_thread_members(thread_id) -> list   # [{"user_id": str, "role_ids": [str]}]
  ```
  REST mapping: `POST /channels/{parent}/threads` with `{"type": 12, "name": ..., "auto_archive_duration": ...}`; `PATCH /channels/{thread}`; `PUT`/`DELETE /channels/{thread}/thread-members/{user}`; `GET /channels/{thread}/thread-members?with_member=true` (the `with_member` flag returns member objects whose `roles` populate `role_ids`, so `!lock` needs no second fetch).
- **Failure semantics:** `add/remove_thread_member` and `edit_thread` return `False` on 403/404 (unknown thread, missing perms) — expected outcomes the script branches on. `create_private_thread` raises on failure (nothing sensible to do without a thread id) and `get_thread_members` returns `[]` on 404. Infrastructure failures (rate-limit exhaustion, 5xx) raise as today.
- **Budget (reconciled onto `threads-and-member-events.md` §5.3, which shipped first):** the `thread_ops` counter, `spend_thread_op()`, the `HandlerRun.thread_ops` column, and `usage()` wiring **already exist** — do not re-introduce them. This family's mutating functions (`create_private_thread`, `edit_thread`, `add/remove_thread_member`) spend the existing counter; `get_thread_members` is a read and spends `discord_reads` instead (consistent with `list_threads`; `ADMIN_MAX_DISCORD_READS = 5` covers the one membership fetch a `!lock` sweep needs). Shipping this family raises `ADMIN_MAX_THREAD_OPS` from 10 to 25 in the same change — a `!lock` sweep is 1 + N removals; a giant thread breaches loud mid-sweep, which is the correct behavior.
- **Windowed cap (reconciled):** all mutating ops here ride the shipped `guild_thread_ops_key(guild_id)` / `GUILD_THREAD_OPS_PER_MIN = 30` window like every other thread mutation. `create_private_thread` **additionally** gets the tighter `guild_thread_creates_key(guild_id)` / `GUILD_THREAD_CREATES_PER_MIN = 2` — private-thread creation is the spammy primitive and the shared 30/min is too loose for it specifically.
- **Verification rail (inherited):** `edit_thread`, `add/remove_thread_member`, and `get_thread_members` take script-supplied thread ids and go through `AdminActor._verify_thread_target` (is-a-thread + in-this-guild, cached per fire) exactly like the shipped close/lock/reopen/delete family. `close_thread`/`lock_thread`/`reopen_thread` already shipped — `edit_thread` adds only rename/auto-archive on top and shares `_patch_thread`'s 404 → `False` contract.
- **Run record:** already shipped (`threads-and-member-events.md` §6); no new migration for the counter.
- **Lint/judge:** admin author/judge prompts gain the vocabulary; judge guidance: membership loops must be bounded by the member list itself (never `while`), and `edit_thread(archived=True)` after posting the closing message, not before (a post into an archived thread 403s).
- **Consumed by:** rows 13, 14, 15.

### E6 — Thread-aware message-trigger dispatch

> **SHIPPED** by `threads-and-member-events.md` §4 (2026-07-17), with one context-shape
> difference: the shipped fields are `thread_id`, `thread_name`, and `is_thread`
> (boolean) — there is no `thread_parent_channel_id` field, because the dispatch
> `channel_id` *is* the parent. Scripts written against this sketch should read
> `is_thread` instead of null-checking `thread_parent_channel_id`. The rest of this
> section is retained for the original rationale only.

- **What:** `!archive`/`!lock` are typed *inside* a thread. Today `on_message` dispatches on the raw `event.channel_id`; a thread's id differs from its parent's, so channel-scoped handlers never fire and context can't distinguish threads.
- **Design (bot-side, `handler_events.py`):** resolve the message channel from the gateway cache (`bot.cache.get_thread(channel_id)` / `get_guild_channel`); when it is a thread:
  - dispatch key = the thread's `parent_id` (so handlers scoped to the parent channel fire; guild-wide admin handlers already fire via `guild_triggers`),
  - add context fields: `"thread_id"`, `"thread_parent_channel_id"`, `"thread_name"` (all `None`/absent for non-thread messages).
  Worker side: the fire's `channel_id` stays the parent so home-channel semantics (`send_message()` with no target, error notices) don't change; the script posts into the thread explicitly via `send_message(text, context["thread_id"])` (admin cross-channel, already metered per-destination).
- **Budget/caps:** none new — thread posts meter against the thread's own `channel_message_key` window, which is what we want.
- **Lint/judge:** author prompt documents the three context fields and that they may be absent.
- **Migration:** none (context is JSON).
- **Consumed by:** rows 14, 15 (and any future thread-reactive handler).

### E1 — `dm_message` trigger type (admin tier only)

- **What:** a fifth trigger type for inbound user DMs to the bot. **Admin handlers only** — a member-authored channel handler must never see other users' DMs. `HANDLER_TRIGGER_TYPES` gains `"dm_message"`, but the standard-tier authoring path and the `channel_handlers` check constraint do **not** (constraint updated only on `admin_handlers`).
- **Context shape:**
  ```python
  {
      "trigger_type": "dm_message",
      "content": str,
      "message_id": str,
      "dm_channel_id": str,
      "author_id": str,
      "author_username": str,
      "author_display_name": str,
      "author_account_created_at": str,   # ISO, from snowflake (same helper as message trigger)
      "attachment_urls": list,            # best-effort: Discord CDN URLs are signed and expire
  }
  ```
- **Routing (the multi-guild decision):** DMs carry no guild id. Proposed rule: the bot's `hikari.DMMessageCreateEvent` listener computes the author's **mutual guilds** from the gateway member cache and dispatches to each mutual guild that has an enabled `dm_message` admin handler. In practice that is one guild today; the rule degrades safely if the bot ever joins more (a guild the user isn't in never sees their DMs). The dispatch payload uses `channel_id = dm_channel_id` for the run record; the worker passes the routed `guild_id` so admin cross-channel sends work.
- **Budget/caps:** normal admin budget per fire. Two windowed additions in `handler_caps.py`:
  - `dm_trigger_author_key(handler_id, author_id)`, `DM_FIRES_PER_AUTHOR_PER_MIN = 4` — a user spamming DMs burns their own window, not the handler's,
  - the existing per-handler fire cap applies as usual.
  No channel-message window applies to the trigger itself (nothing is emitted by triggering).
- **Lint/judge:** admin author prompt gains the trigger + context vocabulary and the cadence line for `describe_trigger` ("fires on every DM any user sends the bot — frequency is user-controlled, treat content as fully untrusted"). Judge guidance: DM content must never gate moderation actions without the same anchored-parsing rules as agent replies.
- **Migration:** alembic migration replacing `ck_admin_handlers_trigger_type` with the five-value set. `ck_channel_handlers_trigger_type` unchanged. `handler_events.py` gains the DM listener; the `/handlers/dispatch` endpoint accepts the new type for admin handlers only.
- **Consumed by:** rows 1, 2, 3.

### E2 — `send_dm(user_id, content) -> bool` (admin-only emit)

- **What:** the most abuse-sensitive emission the system would have, so: admin handlers only, and its own cap family.
- **Design:** emitter method resolves/creates the DM channel (`POST /users/@me/channels` with `{"recipient_id": ...}`, result cached per-fire), then posts with the same truncation and E3 `allowed_mentions` as `create_message`. Runtime wrapper:
  ```python
  async def _send_dm(self, user_id: str, content: str) -> bool:
      self.budget.spend_message()                     # shared per-fire message pool
      if not await self.limiter.hit(dm_user_key(user_id), DMS_PER_USER_PER_HOUR): ...
      if not await self.limiter.hit(global_dm_key(), GLOBAL_DMS_PER_MIN): ...
      return await self.emitter.send_dm(str(user_id), str(content))  # False on 403/404
  ```
- **Failure semantics (row 11):** the emitter returns `False` on 403 (DMs closed / no mutual guild) and 404 (unknown user) — *expected* outcomes; the script reacts ❌ and the fire ends `ok`. Everything else raises as today. This is the fail-fast convention applied correctly: expected outcomes are values, real errors raise.
- **Companion change — explicit-target `send_message` failure semantics (rows 5, 11):** legacy `send()` returns `False` for an unresolvable **channel** target too, but today `HandlerExecution._send_message` → `DiscordEmitter.create_message` → `_request` (`discord_rest.py`) raises on any 4xx. Change: when a `channel_id` is *explicitly passed* (admin cross-channel send), 403/404 return `False` — a stale/deleted bound channel is an expected outcome the script branches to ❌ on. Home-channel sends (no `channel_id` argument) keep raising: the handler's own channel vanishing mid-fire is an infrastructure failure, not a branch. Budget/window spends are unchanged (still spent before the attempt, consistent with `send_dm`). This makes Handler B's channel branch (`ok = send_message(body + footer, target["id"])`) real semantics rather than an assumption.
- **Caps:** `DMS_PER_USER_PER_HOUR = 30`, `GLOBAL_DMS_PER_MIN = 10`. Cap breaches raise `CapExceeded` (they are rails, not expected branches). **Why 30, not the originally-drafted 5:** legacy imposes *no* rate limit on relay replies, and the feature's primary use case is an ongoing staff↔user DM conversation of repeated `!!!` replies — at 5/user/hour the 6th reply in an hour would error the fire through `handler_notify`, and Handler A's one-time "monitored" notice shares the same per-user window. Each relay reply is individually typed by a staff member, so the human is the throughput limiter; the per-user window is a runaway-loop rail that must sit comfortably above any real conversation, while `GLOBAL_DMS_PER_MIN` and the per-fire message budget remain the actual abuse rails for unsolicited DM drip.
- **Lint/judge:** admin judge guidance — `send_dm` to the *triggering* DM author (`context["author_id"]` on a `dm_message` fire) is low-risk; `send_dm` to ids derived from anything else demands explicit justification in the handler description.
- **Migration:** none beyond cap constants (Redis keys are schemaless).
- **Consumed by:** rows 3, 5, 11.

### E4 — Guild-scoped shared memory (`guild_memory_get` / `guild_memory_set`)

- **What:** the DM-mirror handler (trigger `dm_message`) and the relay handler (trigger `message`) are necessarily two handler rows (one `trigger_type` column each), and per-handler memory is private. Auto-bind (row 2) is the one legacy behavior that needs state to cross that boundary.
- **Design:** new table via alembic:
  ```
  guild_handler_memory(id UUID PK, guild_id VARCHAR(20), key VARCHAR(64), value JSON,
                       updated_at timestamptz, UNIQUE(guild_id, key))
  ```
  Per-key rows (not one blob) so two concurrent fires writing different keys never conflict; same-key writes are last-write-wins via upsert. Rails: value ≤ 4KB serialized, ≤ 64 keys per guild (`CapExceeded("guild_memory_size")` beyond). Exposed as `guild_memory_get(key, default=None)` / `guild_memory_set(key, value)` — **admin handlers only**, loaded/persisted by the worker around the fire like `HandlerMemory` (read set snapshotted before, dirty keys upserted after, consistent with "emitted effects stay").
- **Budget/caps:** reads/writes are DB-only, no Discord emission — uncapped per-fire beyond the size/count rails (matching `memory_*`, which spend nothing).
- **Lint/judge:** judge treats guild memory like handler memory for the `memory_bounded` check (no per-user/per-message keys without pruning).
- **Consumed by:** row 2 (and row 18 if mod-chat commands are ever split across handlers).
- **Decision point:** if Zech prefers zero new tables, drop auto-bind instead — the mirror message includes a paste-ready `!bind <@user_id>` line and everything else stands (see §6).

### E7 — Message-context enrichment (`author_role_ids` **required** for in-thread gating; rest optional)

- **What:** add to the `message` (and thread-message) trigger context in `handler_events.py`:
  ```python
  "author_role_ids": [str, ...],          # from event.member.role_ids
  "author_is_admin": bool,                # computed from cached role permissions
  "mentioned_user_ids": [str, ...],       # from msg.user_mentions_ids
  "mentioned_role_ids": [str, ...],
  "mentioned_channel_ids": [str, ...],    # parsed <#id> from content (hikari has no field)
  ```
- **Why `author_role_ids` is required, not optional:** the DM-relay rows and `!modchat` gate by *admin-tier authoring + channel scoping* (rows 6, 19), which suffices when the typing location is a staff-only channel. It does **not** suffice for `!archive`/`!lock`: they are typed *inside the thread*, E6 dispatches any thread member's message to the parent-scoped handler, and `!modchat` adds every mentioned user to the thread (legacy allowed mentioning anyone because each command carried its own `kick_members` check). Without a per-invoker check, a mentioned non-mod thread member could archive or lock the thread — a silent weakening of legacy's permission model. `author_role_ids` restores a per-invoker gate (`mod_role_id in context["author_role_ids"]`); the handler system exposes roles rather than raw Discord permission bits, so "holds the mod role" is the smarter-dev translation of legacy's `kick_members`. The mention fields remain optional refinements that replace hand-rolled parsing with trustworthy fields. Cheap, bot-side only, no migration — but `author_role_ids` (populated for thread messages too, from `event.member`) ships **in Phase 2 before the mod-chat handler is authored**, not later.
- **Lint/judge:** author prompt documents the fields; judge guidance: permission-style gates must check ids from context, never trust names typed in message content; judge flags thread-scoped mutating commands (`edit_thread`, `remove_thread_member`) whose script has no invoker role gate.
- **Consumed by:** rows 14, 15, 19 (required — `author_role_ids`); rows 6, 13 (refinements — mention fields).

**Deliberately NOT designed:** a `send_channel_message` function (exists as admin `send_message(content, channel_id)`), a delete-after/sleep primitive (row 10 dropped), embed support (both features degrade cleanly to markdown text — the mirror "embed" and mod-chat "intro embed" carry no functional payload; revisit only if plain text proves ugly in practice).

## 4. Per-feature plans

Both features become **admin handlers** authored through the existing admin pipeline (`run_admin_creation_pipeline`). That single decision does most of the legacy permission model's work: only admins can install or edit the handlers (dual-judge reviewed), and scoping via `channel_ids` puts the command listeners where only staff can type. Legacy `!`-prefix commands are **kept as message-text commands** inside the handlers — staff muscle memory, zero new interaction surface, and the handler system has no slash trigger (moving them to slash commands would push everything to bot-core for no gain). Exact prefixes are an authoring-time choice; sketches below keep the legacy ones.

### 4.1 DM Relay

Three admin handlers plus one shared guild-memory key.

**Handler A — `dm-mirror` (trigger: `dm_message`, scope: n/a).**
Description an admin would give: *"When anyone DMs the bot: post the message into #staff-dm-log with the sender's name and id, point the reply relay at that sender, and on their first-ever DM reply telling them DMs are monitored by staff."*

Script sketch (Monty):

```python
author_id = context["author_id"]
lines = [
    "**New DM from @" + context["author_username"] + " (" + author_id + ")**",
    context["content"] if context["content"] else "*(no text)*",
]
for url in context["attachment_urls"]:
    lines.append("attachment: " + url)
lines.append("reply in <#RELAY_CHANNEL_ID> with `!!!<message>`, or retarget there first with `!bind <@" + author_id + ">`")   # !bind only works in the relay channel (see Handler B narrowing note)
send_message("\n".join(lines), "STAFF_DM_LOG_CHANNEL_ID")   # baked in at authoring

guild_memory_set("relay_bind_target", {"kind": "member", "id": author_id})

warned = memory_get("warned_ids", [])
if author_id not in warned:
    delivered = send_dm(author_id, "Heads up: DMs to this bot are relayed to the staff team.")
    if delivered:
        warned.append(author_id)
        memory_set("warned_ids", warned[-500:])   # bounded — memory caps raise, they don't evict
```

Notes: the log channel id is a script constant the admin author resolves with its channel-lister tool (this *is* the replacement for `!set dm logging channel`). The warned-list prune means a user who last DM'd 500 senders ago gets the notice again — harmless, and it keeps the 16KB memory rail unbreachable. Attachment URLs expire (signed CDN) — mirror is best-effort by design.

**Handler B — `dm-relay-commands` (trigger: `message`, scope: `["<#staff-relay-channel>"]`).**
Description: *"In the staff relay channel: `!bind @user` or `!bind #channel` sets the relay target; `!!!<text>` sends the text to the current target with a 'Sent by' footer, reacting 📤 on success and ❌ on failure."*

Script sketch:

```python
content = context["message_content"]

def parse_mention(text, open_tokens):
    # first <@id> / <@!id> / <#id> in text -> (kind, id) or None
    for token, kind in open_tokens:
        start = text.find(token)
        if start != -1:
            end = text.find(">", start)
            digits = text[start + len(token):end]
            if digits.isdigit():
                return {"kind": kind, "id": digits}
    return None

if content.startswith("!bind"):
    target = parse_mention(content, [("<@!", "member"), ("<@&", None), ("<@", "member"), ("<#", "channel")])
    if target is None or target["kind"] is None:
        send_message("Mention a user or a channel: `!bind @user` / `!bind #channel`")
    else:
        guild_memory_set("relay_bind_target", target)
        add_reaction(context["message_id"], "📤")
elif content.startswith("!!!"):
    target = guild_memory_get("relay_bind_target")
    body = content[3:].strip()
    if target is None or not body:
        add_reaction(context["message_id"], "❌")
    else:
        footer = "\n-----\n*Sent by " + context["author_name"] + "*"
        if target["kind"] == "member":
            ok = send_dm(target["id"], body + footer)          # False on 403/404 (E2)
        else:
            ok = send_message(body + footer, target["id"])     # False on 403/404 (E2 companion change)
        add_reaction(context["message_id"], "📤" if ok else "❌")
```

Mention suppression is the emitter's job (E3) — the footer/body cannot mass-ping regardless of what staff type. The transient 2-second "Sent to" toast is dropped (row 10); the reaction is the confirmation. If E4 is rejected, `guild_memory_*` becomes `memory_*` here, auto-bind is dropped, and staff always `!bind` from the mirror's paste-ready line.

**Deliberate narrowing (needs Zech's sign-off, see §6):** legacy `!bind` was gated only by `ban_members` and worked from *any* channel — only `!!!` was restricted to the binding channel. Folding `!bind` into Handler B means it only works inside the relay channel scope; typed anywhere else it silently does nothing. That's why Handler A's mirror line names the relay channel explicitly (`<#RELAY_CHANNEL_ID>`) right next to the paste-ready `!bind` — so staff aren't invited to type it into the DM-log channel and be surprised. One predictable place beats guild-wide listening, and the handler system has no per-invoker Discord-*permission* check to replicate `ban_members` anyway (E7 checks roles, not permissions).

**Legacy state mapping (row 12):** `dm-logging-channel` → constant in Handler A; `message-binding-channel-id` → Handler B's `channel_ids` scope; `message-bind-target` → `guild_memory["relay_bind_target"]`; `dm-logging-channel-warning` → bounded `warned_ids` list in Handler A's memory. No data migration from the legacy bot — state starts fresh.

### 4.2 Mod Chat (private threads)

**One admin handler — `mod-chat` (trigger: `message`, scope: staff channels + the mod-chat parent channel; thread messages reach it via E6 parent-channel dispatch).** One handler (not five) so all config lives in its own memory — no guild memory needed. Config keys (`mod_chat_channel_id`, `mod_role_id`, `mod_notice_channel_id`) are seeded into handler memory at authoring time; changing them is a re-authoring conversation, which replaces both `!set` commands.

Description: *"Staff mod-chat threads: `!modchat` creates a private thread under the mod-chat channel named 'Mod Chat: YYYY-MM-DD', adds the invoker and anyone they mentioned, posts an intro (naming — not pinging — the mod role), and announces the thread in the mod notice channel. Inside such a thread, `!archive` archives+locks it with an -ARCHIVED name suffix, and `!lock` first removes every member without the mod role, then archives+locks with -LOCKED. All three commands only act for invokers who hold the mod role."*

**Invoker gate (rows 14/15/19):** channel scoping cannot gate the in-thread commands — `!archive`/`!lock` are typed inside the thread, and `!modchat` adds every *mentioned* user to it, so "is a thread member" includes non-mods by design. The script therefore checks `mod_role_id in context["author_role_ids"]` (E7) on every command, mirroring legacy's per-invoker `kick_members` check. Applying the same gate to `!modchat` also removes any requirement that the parent channel be staff-only (legacy worked with any configured channel for exactly this reason).

Script sketch:

```python
content = context["message_content"]
parent_id = memory_get("mod_chat_channel_id")
mod_role_id = memory_get("mod_role_id")
notice_channel_id = memory_get("mod_notice_channel_id")
thread_id = context.get("thread_id")
in_mod_thread = thread_id is not None and context.get("thread_parent_channel_id") == parent_id
is_command = content.startswith("!modchat") or content.startswith("!archive") or content.startswith("!lock")
invoker_is_mod = mod_role_id is not None and str(mod_role_id) in context.get("author_role_ids", [])

if not is_command:
    pass
elif mod_role_id is None:
    send_message("Mod chat isn't configured (no mod role) — ask an admin to re-author me.")   # fail fast, row 22
elif not invoker_is_mod:
    pass    # per-invoker gate (E7): legacy kick_members check ⇒ mod role here; non-mod thread members added via !modchat mentions get no traction

elif content.startswith("!modchat"):
    if parent_id is None:
        send_message("Mod chat isn't configured — ask an admin to re-author me with a parent channel.")
    else:
        today = date.today()
        name = "Mod Chat: " + today.isoformat()                      # ISO, not legacy DD-MM-YYYY
        new_thread = create_private_thread(parent_id, name, 4320)    # 3-day auto-archive
        send_message("Private mod discussion — mod role: <@&" + str(mod_role_id) + ">", new_thread)
        add_thread_member(new_thread, context["author_id"])
        for user_id in context.get("mentioned_user_ids", []):        # E7 mention fields; else parse <@id> from content
            add_thread_member(new_thread, user_id)
        if notice_channel_id is not None:
            send_message("New mod chat thread created: <#" + new_thread + ">", notice_channel_id)

elif content.startswith("!archive") and in_mod_thread:
    send_message("This thread has been archived.", thread_id)
    edit_thread(thread_id, context.get("thread_name", "mod-chat") + "-ARCHIVED", True, True)

elif content.startswith("!lock") and in_mod_thread:
    for member in get_thread_members(thread_id):
        if str(mod_role_id) not in member["role_ids"]:
            remove_thread_member(thread_id, member["user_id"])
    send_message("This thread has been closed — only mods have access.", thread_id)
    edit_thread(thread_id, context.get("thread_name", "mod-chat") + "-LOCKED", True, True)
```

Adaptation decisions: thread names use ISO `YYYY-MM-DD` (flagged in §6); the intro role mention renders as text and can never ping (E3) — preserving the legacy commented-out-ping behavior structurally; intro "embed" degrades to markdown; ordering matters — closing messages post *before* `edit_thread(archived=True)`; per-fire ceilings: `!modchat` with N mentions spends 1 + N thread ops + up to 3 messages (fits admin budget for N ≤ ~10, breaches loud beyond); `!lock` on a thread with >24 non-mods breaches `thread_ops` mid-sweep — visible in the error notice, rerunnable. `mod_notice_channel_id` should point at the same channel smarter-dev's existing mod-notification stack uses (reconcile, don't duplicate — row 17 / EXISTING-FEATURES.md moderator-role overlap).

**Bot-core work: none for either feature.** Everything lands in `handler_events.py` (dispatch/context), the web tier (runtime/emitter/caps/models/migrations), and prompts.

## 5. Implementation order & TDD notes

Ship in four phases, each independently valuable. Tests-first throughout; the handler modules are already built for it (injectable emitters/limiters/authors/judges, pure helpers).

**Phase 1 — E3 mention suppression.** One payload change + prompt line.
- Tests first: `create_message` payload contains `allowed_mentions == {"parse": ["users"]}`; content with `@everyone`/`<@&id>` still sends verbatim as text; truncation unchanged.
- Pre-ship check: grep installed handler scripts for intentional role pings (expect none).

**Phase 2 — Mod Chat** (no DM machinery needed, so it lands first of the two features).
1. ~~`HandlerBudget.spend_thread_op` + `thread_ops` in `usage()`~~ — **shipped** by `threads-and-member-events.md`; only the `ADMIN_MAX_THREAD_OPS` 10 → 25 raise remains (one-line change + test).
2. ~~`HandlerRun.thread_ops` column + alembic migration~~ — **shipped**.
3. Emitter/actor thread REST methods — tests with a fake `_request`: correct routes/payloads; 403/404 → `False`/`[]` (expected outcomes); 5xx → raises (fail fast); `get_thread_members` passes `with_member=true` and maps `role_ids`; all thread-id-taking methods go through the shipped `_verify_thread_target` rail.
4. Runtime wiring — tests: functions absent without `actor`; mutations spend `thread_ops`, `get_thread_members` spends `discord_reads`; breach mid-`!lock`-sweep yields `cap_exceeded` with prior removals kept ("effects stay"); `create_private_thread` hits the shared guild thread-op window AND the tighter thread-create window.
5. ~~E6 thread-aware dispatch~~ — **shipped** (`is_thread` context shape; see the E6 note above).
6. E7 context fields — pure builder tests. **`author_role_ids` is load-bearing here** (the `!archive`/`!lock` invoker gate, rows 14/15/19) and must land before step 7; verify it is populated for thread messages too (`event.member` on the thread message). The mention fields remain the optional part.
7. Prompt updates, then author the `mod-chat` handler in a test guild. Critical failure paths to cover in script-level tests (run the sketch through `run_handler_script` with a fake emitter): unconfigured keys → explanatory message, no thread ops; `!archive` outside a mod thread → no-op; **non-mod thread member (added via `!modchat` mention) types `!archive`/`!lock` → no-op, zero thread ops**; `edit_thread` ordering (message before archive); `!lock` with all-mods membership → zero removals.

**Phase 3 — DM Relay core (E1 + E2).**
1. Alembic migration for the `admin_handlers` trigger constraint (channel-handler constraint untouched — test both).
2. `send_dm` emitter method — tests: DM-channel create + message post sequence; per-fire DM-channel cache; 403/404 → `False`; caps: per-user hour window, global minute window, spends from the shared message pool; 30 sequential sends to one user pass, the 31st raises `CapExceeded` (a realistic relay conversation never breaches).
3. E2 companion change to explicit-target `send_message` — tests: explicit `channel_id` + 403/404 → `False` (no raise, no `handler_notify`); home-channel send (no `channel_id`) still raises on 4xx; budget/window spends unchanged on the `False` path; 5xx raises for both forms.
4. `dm_message` bot listener + routing — pure routing function `route_dm_guilds(author_mutual_guild_ids, guilds_with_dm_handlers) -> list`; tests: single mutual guild, multiple, none (drop, log), bot's own messages ignored; per-author fire window.
5. Dispatch endpoint accepts `dm_message` for admin handlers only (403 path tested).
6. Prompts (trigger vocabulary, `describe_trigger` cadence line, judge DM-content-is-untrusted guidance).
7. Author `dm-mirror` (auto-bind line stubbed to `memory_set` until Phase 4) and `dm-relay-commands` (own-memory bind target). Script-level failure paths: DMs-closed → `False` → ❌ reaction and fire outcome `ok` (NOT an error notice — assert `handler_notify` never fires); bound channel deleted → `send_message` returns `False` → ❌ reaction, fire outcome `ok`; `!!!` with no bind target → ❌; `!bind` with no mention → usage message; warned-list prune keeps memory under 16KB across 1000 synthetic senders.

**Phase 4 — E4 guild memory + auto-bind (skippable if dropped).**
1. `guild_handler_memory` table + alembic; upsert semantics.
2. `GuildHandlerMemory` store class mirroring `HandlerMemory` (per-key rows, 4KB/value, 64 keys/guild, `CapExceeded` rails) — pure tests first, then worker load/persist-around-fire tests including "write survives a later script error".
3. Concurrency test: two concurrent fires writing different keys both persist; same key → last-write-wins, no exception.
4. Re-author both relay handlers onto `guild_memory_*`; end-to-end: DM in → mirror posts → `!!!` reply reaches the DM author with footer → 📤.

**Cross-cutting failure paths (every phase):** cap breach mid-fire leaves prior effects and records the cap name; expected REST failures return values, never raise; infrastructure failures raise and produce a throttled error notice; migrations round-trip.

## 6. Open questions / drop recommendations

### Needs Zech's call

1. **DM routing rule (E1).** Proposed: dispatch to every mutual guild with an enabled `dm_message` admin handler. Alternative: a single designated "home guild" setting. Mutual-guild routing is proposed because it needs no new config and is correct for the current single-guild reality.
2. **Auto-bind vs. Phase 4 (E4).** Is auto-bind-to-latest-DM-author worth a new table + shared-memory surface? The fallback (mirror includes a paste-ready `!bind <@id>` line; relay handler keeps its own memory) costs one copy-paste per conversation switch and deletes an entire extension. Recommendation: ship Phases 1–3, live with manual bind for a week, then decide.
3. **`send_dm` cap values.** Proposed 30/user/hour and 10/min global. "Tight is right" applies to the *global* window and per-fire budget (the unsolicited-drip abuse shape) — but the per-user window must comfortably exceed a real staff↔user conversation, because legacy imposes no relay rate limit and the ongoing `!!!` back-and-forth is the feature's primary use case (at the originally-drafted 5/hour, the 6th reply in an hour would error the fire; the one-time monitored notice shares the same window). Confirm 30, or pick another number — but not single digits.
4. **E7 context enrichment.** `author_role_ids` is no longer optional — it is the required invoker gate for the in-thread `!archive`/`!lock` commands (rows 14/15/19) and ships in Phase 2. The remaining open part is only the *mention* fields (`mentioned_user_ids` etc.): include them in Phase 2 anyway? Recommended — cheap, and makes `!modchat` cleaner than hand-parsing.
5. **Command prefixes.** Sketches keep `!!!`, `!bind`, `!modchat`, `!archive`, `!lock` for muscle memory. Any renames are authoring-time choices, zero code.
6. **Thread name format.** Proposed ISO `YYYY-MM-DD` over legacy `DD-MM-YYYY` (sorts correctly in the thread list). Confirm.
7. **Attachment mirroring.** Signed CDN URLs expire, so the mirror's attachment links go stale. Accept best-effort (proposed), or require media re-upload — which would be new emitter surface (file upload) and is not worth it for a staff log.
8. **Mod-channel reconciliation.** `mod_notice_channel_id` should be the same channel the existing moderation stack notifies. Confirm which config is canonical so it isn't stored twice.

### Drop recommendations (final call is Zech's)

| Legacy behavior | Rationale |
|---|---|
| `!set dm logging channel` | Superseded: the log channel is chosen during the authoring conversation (admin author has a channel-lister tool). Changing it = re-authoring, which is already the system's edit model. |
| `!set binding channel` | Superseded by `AdminHandler.channel_ids` scoping — the binding channel is the handler's scope. |
| `!set modchat channel` | Superseded: seeded into the mod-chat handler's memory at authoring time. |
| `!set mod role` | Same, plus smarter-dev's Guild model / web admin already carry a moderator-role concept — reconcile with that rather than adding a chat setter. |
| Transient "Sent to <mention>" 2s auto-delete confirmation | Redundant with the 📤 reaction. Would require a sleep/delete-after primitive solely for a 2-second toast. A permanent audit line is handler-today if ever wanted. |
| `!bind` guild-wide availability (legacy: gated only by `ban_members`, usable from any channel) | Narrowed to the relay channel: Handler B's `channel_ids` scope is the only place `!bind` (and `!!!`) listens, so `!bind` typed elsewhere — e.g. in the DM-log channel — does nothing. Rationale: one predictable staff location beats guild-wide listening, and the handler system has no per-invoker Discord-*permission* check to replicate `ban_members` (E7 checks roles, not permission bits) — guild-wide `!bind` would otherwise be gated by nothing at all. Mitigation: Handler A's mirror line names the relay channel (`<#RELAY_CHANNEL_ID>`) right beside its paste-ready `!bind` snippet, so staff aren't led into typing it where it's ignored. |

Nothing else from either legacy feature is omitted: all 22 analyzed capabilities are dispositioned in §2, and every non-dropped one is covered by §4's two feature plans plus §3's extensions.
