# Feature Parity: Engagement Loops & Server Stats

**Legacy sources:**
- `beginner.codes-bot/docs/features/disboard-bumping.md` (`DisboardBumpReminderExtension`, `DiscordMeBumpReminderExtension`)
- `beginner.codes-bot/docs/features/server-stat-counter.md` (`MemberCounterExtension`, `OnlineCounterExtension`)

**Target:** the smarter-dev handler system (`smarter_dev/web/handler_*.py`, `smarter_dev/bot/plugins/handler_events.py`, `smarter_dev/bot/agents/handler_authoring.py`), extended where needed. No bot-core work — the one candidate (gateway presence aggregation) was dropped along with the online-count features (decision 2026-07-18).

## 1. Overview

This group covers two legacy features that are structurally the same thing: **schedule-driven engagement/stats loops built on persistent counters**.

- **Disboard bumping** — remind a role 2h after the last confirmed `/bump`, detect Disboard's confirmation messages to credit the invoker, keep the bump channel clean, keep a 7-day bump ledger, and rotate a "Bump King" crown role with an announcement. Plus `!bumpers` / `!bumps` / `!update bump king` text commands and a defunct Discord.me sibling reminder.
- **Server-stat counters** — rename a channel to show the member count (`📊Members: 1.2k`). The legacy online-count half (max simultaneous-online record, record broadcasts, `!online stats`, broadcast-channel setter) is **dropped entirely** — see the disposition table.

They are grouped because they share the same implementation pattern in the handler system:

- a **polling schedule handler over persistent state** (reminder poll, rename loop, record poll) — the handler memory store replaces every legacy in-process timer, guild-label blob, and ready-time recovery hack;
- a **detector/command pair over the same state**, which is what forces the one genuinely new state primitive in this plan (guild-scoped shared handler memory);
- **guild-count reads**, **role-ping rails on announcements**, and **rate-limit-aware emits** (channel rename) as shared extensions.

The one capability that could not live in the handler model was the **online-count source of truth**: counting non-offline members requires the `GUILD_PRESENCES` intent and the gateway's presence cache, which only exists in the bot process. Rather than carry a bot-core presence aggregator (plus a privileged intent) for one display feature, the entire online-count family is **dropped** (Zech, 2026-07-18): no aggregator, no `GUILD_PRESENCES`, no record tracker, no `!online stats`. The member-count rename loop survives — it uses REST `with_counts` and needs no privileged intent.

Everything here lands as **admin handlers**. The Disboard handlers need `delete_message`, cross-channel `send_message`, and role transfer — all admin-tier powers — and the stats handlers need `rename_channel` (admin-tier by design, below). Keeping the whole group at one tier also lets shared-memory namespaces stay an admin-only feature initially, which is the cheapest rail against namespace squatting by member-authored handlers.

## 2. Feature disposition table

| # | Capability | Source feature | Disposition | Justification |
|---|---|---|---|---|
| 1 | Bump reminder loop (2h after last bump) | disboard-bumping | handler-extension | 5-min schedule handler polling `last_bump_at`; needs shared memory (E3) to see the detector's state and the role-mention rail (E2) to ping the bumper role. |
| 2 | Startup recovery scan (rebuild timer from channel history) | disboard-bumping | **drop** | Existed only because the legacy timer was in-process. Handler memory persists across restarts; the polling reminder self-heals, and its script treats a missing `last_bump_at` as remind-now, so the cold start (first install / wiped memory) still bootstraps without a channel-history read function nothing else needs. |
| 3 | Disboard bump-confirmation detection | disboard-bumping | handler-extension | Dispatch drops all bot-authored messages today (`event.is_human`); needs the bot-message opt-in plus `embeds`/`interaction_user_id` context fields (E1). |
| 3b | Guild-wide Disboard message routing (legacy credited a confirmed bump in **any** channel; only the delete branch was bump-channel-scoped) | disboard-bumping | **drop** | Deliberate narrowing: Handler 1 is scoped to `channel_ids=[<bump channel>]`, so confirmations elsewhere never dispatch and are not credited. Bumps invoked outside the designated channel lose credit — accepted, since guild-wide scope would let any Disboard traffic fire an admin handler anywhere and complicate the cleanliness branch. Flagged as a parity break for Zech's confirmation (§6). |
| 4 | Bump-channel cleanliness (delete stray messages) | disboard-bumping | handler-extension | `delete_message` already exists at the admin tier; needs E1 so *other bots'* strays also fire the handler. Own-bot exclusion in dispatch spares the standing reminder for free. |
| 5 | Failed/non-confirmation Disboard message deletion | disboard-bumping | handler-extension | A branch of the same detector script (no `bump done!` embed → delete, stop). Same extensions as #3/#4, no new surface. |
| 6 | Bump record persistence with 7-day pruning | disboard-bumping | handler-today | JSON list of `[user_id, ts]` in shared memory (~84 entries max at 2h cadence, well under the 16KB cap). The legacy gzip/base64 guild-label hack is not ported. |
| 7 | Bump King computation with recency tie-break | disboard-bumping | handler-today | Pure Monty logic (dict counting + tie-break) inside the detector script. |
| 8 | Crown role transfer | disboard-bumping | handler-extension | Consumes `add_role`/`remove_role` from the **member-lifecycle group's plan** (E6 summary below). Simplified: remove from the stored previous king only, never enumerate role members. |
| 9 | New Bump King announcement | disboard-bumping | handler-today | Admin handlers can already `send_message` to any guild channel. Embed becomes formatted text (open question Q7). |
| 10 | `!update bump king` admin command | disboard-bumping | handler-extension | Branch of the bump-commands handler gated on the new `author_is_admin` context field (E7). Alternative: drop in favor of asking the admin authoring chat to re-fire — Zech's call (Q6). |
| 11 | `!bumpers` leaderboard command | disboard-bumping | handler-today* | Message-trigger branch reading the ledger and replying with a ranked text list. *Only needs E3 because it lives outside the bump channel (the cleaner deletes human messages there). |
| 12 | `!bumps` recent-bump list command | disboard-bumping | handler-today* | Same handler as #11; `<t:...>` markup works fine in plain `send_message` content. |
| 13 | Channel purge (`_clean_channel`, <1 day bulk delete) | disboard-bumping | **drop** | Superseded by per-message deletion (#4) plus targeted deletion of the previous reminder **and previous confirmation** ids, both stored in shared memory (`reminder_message_id`, `confirmation_message_id` — see #13b). A bulk-purge function would be budget-hostile and serves no remaining case. |
| 13b | Previous-confirmation removal (legacy `_clean_channel` spared only the just-processed confirmation, deleting the prior "Bump done!" each cycle) | disboard-bumping | handler-today | The tracker stores `confirmation_message_id` in shared memory and, on each successful bump, deletes the previous confirmation before storing the new id — exactly one confirmation stays visible, matching legacy. Without this the drop of #13 would let confirmations accumulate indefinitely. |
| 14 | Hardcoded ID configuration (5 legacy snowflakes) | disboard-bumping | handler-today | IDs become authoring-time script constants / handler `settings`; the bump-channel binding is the handler's own `channel_ids` scope. Legacy IDs are Beginner.Codes-specific and die here (Q1). |
| 15 | Discord.me 6-hour reminder (sibling extension) | disboard-bumping | **drop** | Discord.me is effectively defunct. If wanted anyway it is handler-today (`interval_seconds=21600` + E2); the :59:30 alignment was cosmetic and isn't expressible in the schedule vocabulary. |
| 16 | Member-count channel rename loop | server-stat-counter | handler-extension | Schedule handler; needs `rename_channel` emit with a Discord-aligned cap (E4) and `get_guild_member_count()` (E5). |
| 17 | Member-count formatting rule (`1.2k` / `2.0k`) | server-stat-counter | handler-today | Pure string formatting in the rename handler. The legacy `📊Members:` prefix is kept byte-for-byte — the rendered name doubles as the change-gate key, so the exact string (emoji included) is load-bearing. |
| 18 | Redundant-rename suppression (change detection) | server-stat-counter | handler-today | `memory_get("last_counter")` compare-and-set; belt-and-braces with the E4 rename cap. |
| 19 | Ready-time counter re-seed from channel name | server-stat-counter | **drop** | Obsoleted by persistent handler memory; worst case is one redundant rename attempt after a memory wipe, absorbed by the rename cap. |
| 20 | Live online-count presence aggregation | server-stat-counter | **drop** | Dropped with the whole online-count family (Zech, 2026-07-18). It would have required a bot-core gateway plugin plus the `GUILD_PRESENCES` privileged intent — the only bot-core piece in this group — for a single display feature. |
| 21 | Max-online record detection and persistence | server-stat-counter | **drop** | Dropped: depends entirely on #20's online-count source. |
| 22 | New-record broadcast with 30-minute cooldown | server-stat-counter | **drop** | Dropped with #21. |
| 23 | `!online stats` command | server-stat-counter | **drop** | Dropped with #20/#21 — nothing left to report. |
| 24 | `!set max online channel` admin command | server-stat-counter | **drop** | Moot after the #20–#23 drop; was already recommended for dropping (handler channel binding supersedes a runtime config command). |
| 25 | Stat persistence across restarts | server-stat-counter | handler-today | `last_counter` maps onto the existing memory store (`max_online`/`last_broadcast` died with the online-count drop). No new tables for the stats themselves. |
| 26 | Privileged intents prerequisite (presences + members) | server-stat-counter | **drop** | Moot: `GUILD_PRESENCES` was only needed for #20, which is dropped. Member count avoids the members intent entirely via REST `with_counts`. (EXISTING-FEATURES.md line 255 wrongly claims the presences intent is already enabled — still worth correcting.) |

## 3. Handler-system extensions

Six extensions, ordered by how many capabilities hang off them. All handler-level config lands in the existing `settings` JSON column on `ChannelHandler`/`AdminHandler` — no migration for config. New tables/columns are called out explicitly.

### E1. Bot-authored message dispatch opt-in + message context fields

**What:** today `handler_events.on_message` returns immediately for any non-human message, so Disboard's confirmations can never fire a handler. Add an opt-in, plus three context fields.

**Design:**

- Handler setting: `"include_bot_messages": true` (default absent/false). Valid only on `message`-trigger handlers.
- Bot-side dispatch (`handler_events.py`): replace the hard `if not event.is_human: return` with:
  - **always** drop the smarter-dev bot's own messages (`event.message.author.id == bot.get_me().id`) — this is the structural anti-loop invariant and it also implicitly spares the pending bump reminder from the cleaner;
  - for other bot/webhook authors, dispatch with `author_is_bot: true` (activity recording stays human-only).
- `ActiveChannelsCache` / `GET /handlers/active-channels` gain a third set, `bot_message_channels`, so the hot path only dispatches bot messages when some handler in that channel/guild opted in.
- Web dispatch endpoint: when `trigger_context["author_is_bot"]` is true, fire **only** handlers with `include_bot_messages` set.
- New message-context fields (added for all message dispatches, cheap and mostly empty for humans):
  - `author_is_bot: bool`
  - `embeds: [{"title": str, "description": str}]` — titles/descriptions only, each truncated (say 1KB) so contexts stay small
  - `interaction_user_id: str | None` — the invoker of the slash command that produced the message (`message.interaction.user.id`), which is how the Disboard bumper is identified

**Budget/caps:** no new spend — this changes what *fires*, not what a fire may do. The existing per-handler fires/min windows apply unchanged; bot messages in the bump channel are low-volume by construction (the cleaner keeps it empty).

**Lint/judge:** author prompts (`handler_author.md`, `admin_handler_author.md`) document the setting and fields, with an explicit rule: a bot-message handler MUST guard on a specific `author_id` (e.g. Disboard's) — reacting to *arbitrary* bot messages risks two-bot loops the own-bot exclusion cannot prevent. Judge: this falls under `guards_effective`; add a line to the judge prompts naming the two-bot-loop failure mode.

**Migrations:** none (settings JSON, context is transient).

**Consumed by:** #3, #4, #5.

### E2. Role-mention rail on `send_message`

**What:** an explicit `allowed_mentions` policy with a per-handler role allowlist. Note the current emitter payload **omits** `allowed_mentions`, which means Discord parses mentions from content — today any handler that emits `<@&id>` would actually ping. This extension is as much a hardening fix as a feature.

**Design:**

- `DiscordEmitter.create_message(channel_id, content, allowed_mention_role_ids: list[str] | None = None)` — always sends an explicit `"allowed_mentions": {"parse": ["users"], "roles": [...allowlisted ids...]}` (empty roles list when none). `@everyone`/`@here` are never parseable.
- Handler setting: `"allowed_mention_role_ids": ["<role_id>", ...]` — set at authoring time (the admin authoring flow resolves role names via a small role-lister tool, mirroring the existing channel-lister). Runtime `_send_message` passes it straight through; no script-visible parameter, so a script cannot ping a role its handler wasn't configured for.

**Budget/caps:** no new spend; the message itself is already metered. A role ping is loud, so the judge treatment carries the weight.

**Lint/judge:** author prompt: role pings only work for allowlisted roles and must be **rare** — for these features, at most one ping per reminder/announcement cycle guarded by state. Judge: `actions_appropriate` already covers spam-for-frequency; add role pings to its examples.

**Migrations:** none.

**Consumed by:** #1 (bumper-role ping); #9 optionally; (#15 if un-dropped).

### E3. Guild-scoped shared handler memory (namespaces)

**What:** the detector/reminder and record/command pairs each need two handlers over one state blob, but memory is strictly per-handler today. Add named shared namespaces. This is the alternative chosen over multi-trigger handlers (schema surgery on `trigger_type`) and over a script-armable timer (new scheduling surface); it is the smallest primitive that serves both features and future detector/reporter pairs.

**Design:**

- New model + alembic migration: `HandlerSharedMemory(id, guild_id, namespace, memory JSON, updated_at)`, unique on `(guild_id, namespace)`.
- Handler setting: `"shared_namespace": "disboard-bump"`. **Admin handlers only** initially — the authoring pipeline rejects the setting on member handlers, so no member script can read or squat another handler's namespace. (Standard-tier access, read-only or otherwise, is a future decision.)
- Script surface, mirroring `memory_*`: `shared_get(key, default=None)`, `shared_set(key, value)`, `shared_all()`, `shared_delete(key)`. Present only when the handler declares a namespace; calling them without one raises (fail fast, surfaces via `handler_notify`).
- Runtime: the fire loads the namespace row alongside handler memory into a second `HandlerMemory` instance (same 16KB cap, same JSON-only rule, cap name `shared_memory_size`), and persists it after the fire when dirty — exactly the existing load/run/persist pattern in `handlers_jobs.py`.
- **Concurrency:** load-at-start / persist-at-end is last-writer-wins across concurrent fires. Acceptable here (2h bump cadence, 60s stat polls touching disjoint keys is the worst case); document the caveat in the author prompt ("shared memory is not a lock") and note that per-key UPSERT-on-`shared_set` is the upgrade path if contention ever appears.

**Budget/caps:** no per-call spend (same as `memory_*`); the 16KB namespace cap is the rail.

**Lint/judge:** author prompt documents the functions and the `memory_bounded` expectations extend to the shared blob (the judge checklist already has the category). Judge prompt: two handlers sharing a namespace must have a clear writer/reader split per key.

**Migrations:** one new table.

**Consumed by:** #1, #6, #8, #10, #11, #12, #13b.

### E4. `rename_channel` metered emit + rename cap kind

**What:** a channel-rename effect, the one Discord API call in this group with a brutally low rate limit (2 renames / 10 min / channel).

**Design:**

- `DiscordEmitter.rename_channel(channel_id, name) -> bool` — `PATCH /channels/{id}` with `{"name": name[:100]}`.
- Script surface (admin handlers only, alongside the moderation functions): `await rename_channel(channel_id, name)`. Target must be inside the handler's `channel_ids` scope when the scope is non-empty — so a stats handler scoped to its stats channel can rename only that channel.
- **Budget:** spends from the existing `mod_actions` per-fire budget (it is a guild-mutation, and admin handlers already have that pool); no new `HandlerRun` column.
- **Windowed cap:** new key `hcap:rename:{channel_id}`, limit **2 per 600s** — hard-coded to Discord's limit. `WindowedLimiter.hit()` gains an optional `window_seconds` override (it currently fixes the window per-instance at 60s); the runtime raises `CapExceeded("channel_renames_per_10min", ...)` on breach.

**Lint/judge:** author prompt: renames are rate-limited to 2/10min by Discord itself — a rename MUST be change-gated (compare against a memory key first) and rename handlers should poll at ≥5-minute intervals. Judge: `actions_appropriate` + `guards_effective`; add "un-gated rename on a schedule" to the judge prompts as a named reject pattern.

**Migrations:** none.

**Consumed by:** #16.

### E5. Guild-count read function + `discord_reads` budget counter

**What:** one read-only script function and one new per-fire counter to meter it. (A second function, `get_online_member_count()`, was designed here but died with the online-count drop — see #20–#23.)

**Design:**

- `await get_guild_member_count() -> int` — worker-side REST `GET /guilds/{guild_id}?with_counts=true`, returning `approximate_member_count`. Chosen over the gateway cache (avoids intent coupling) and over the synced `GuildMember` table (sync-lagged); "approximate" is fine for a `1.2k`-granularity display.
- **Budget:** new counter on `HandlerBudget`: `max_discord_reads` (default 5, both tiers), `spend_discord_read()`, included in `usage()`.
- Available to both tiers (read-only, cheap), though in this plan only admin handlers use it.

**Lint/judge:** author prompt documents the function. No judge changes beyond the surface listing.

**Migrations:** one nullable-default-0 `discord_reads` column on `handler_runs` (alembic) so the audit stays complete.

**Consumed by:** #16.

### E6. `add_role` / `remove_role` (consumed from the member-lifecycle group)

The crown transfer (#8) uses the role-mutation extension **designed in the lifecycle group's plan** — not redesigned here. Shape, for reference: `AdminActor.add_role(user_id, role_id)` / `remove_role(user_id, role_id)` (`PUT`/`DELETE /guilds/{gid}/members/{uid}/roles/{rid}`), exposed to admin handlers only, spending `mod_actions`, restricted to a per-handler `"assignable_role_ids"` settings allowlist, with the judge treating role mutation as privileged. This plan adds only one consumer-side requirement: the crown role id sits in that allowlist for the bump-tracker handler.

**Consumed by:** #8.

### E7. `author_is_admin` message-context field

**What:** lets a script gate a branch on the author's guild permissions without rolling a user-id allowlist (which would violate the no-rolled-own-auth convention in spirit).

**Design:** bot-side dispatch computes `author_is_admin: bool` from the cached member's permissions (hikari roles → `ADMINISTRATOR` bit; `false` on any cache miss — fail closed) and, for flexibility, `author_role_ids: [str]`. Pure context additions; no budget/cap impact.

**Lint/judge:** judge prompt rule: any branch performing privileged effects on command (e.g. force-recompute that moves roles) MUST check `author_is_admin`. Falls under `actions_appropriate`.

**Migrations:** none.

**Consumed by:** #10.

## 4. Per-feature plans

### 4.1 Disboard bumping

Three **admin handlers**, all sharing `"shared_namespace": "disboard-bump"`. All Discord ids (Disboard bot user `302050872383242240` is the only legacy id that survives; bump channel, bumper role, crown role, announcement channel are chosen fresh at authoring time — Q1) are script constants or settings written by the admin authoring pipeline.

Shared-memory schema:

```
bumps:                   [[user_id, unix_ts], ...]   # newest first, pruned to 7 days
king_id:                 str | None
last_bump_at:            unix_ts | None
reminded:                bool
reminder_message_id:     str | None
confirmation_message_id: str | None   # last "Bump done!" confirmation (exactly one stays visible)
```

**Handler 1 — `disboard-bump-tracker`** (message trigger, `channel_ids=[<bump channel>]`, `include_bot_messages: true`, `assignable_role_ids=[<crown role>]`):

```python
DISBOARD = "302050872383242240"
CROWN_ROLE = "<crown role id>"
ANNOUNCE_CHANNEL = "<announcement channel id>"

if context["author_id"] != DISBOARD:
    # cleanliness: everything non-Disboard is deleted (own-bot messages never
    # dispatch, so the standing reminder is spared structurally)
    await delete_message(context["message_id"])
else:
    done = any("bump done!" in (e.get("description") or "").lower()
               for e in context["embeds"])
    bumper = context.get("interaction_user_id")
    if not done or not bumper:
        await delete_message(context["message_id"])   # cooldown/error notice
    else:
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        bumps = [b for b in (await shared_get("bumps", []))
                 if now - b[1] < 7 * 86400]
        bumps.insert(0, [bumper, now])
        await shared_set("bumps", bumps)
        await shared_set("last_bump_at", now)
        await shared_set("reminded", False)
        old_reminder = await shared_get("reminder_message_id")
        if old_reminder:
            await delete_message(old_reminder)
            await shared_set("reminder_message_id", None)
        # rotate the confirmation: delete the previous "Bump done!" and remember
        # this one, so exactly one confirmation stays visible (legacy
        # _clean_channel spared only the just-processed confirmation — #13b)
        old_confirmation = await shared_get("confirmation_message_id")
        if old_confirmation:
            await delete_message(old_confirmation)
        await shared_set("confirmation_message_id", context["message_id"])
        # king computation: counts + legacy tie-break (challenger must have
        # bumped more recently than the incumbent to take a tied crown)
        new_king = compute_king(bumps, await shared_get("king_id"))
        old_king = await shared_get("king_id")
        if new_king != old_king:
            if old_king:
                await remove_role(old_king, CROWN_ROLE)   # stored king only —
            await add_role(new_king, CROWN_ROLE)          # never enumerate members
            await shared_set("king_id", new_king)
            await send_message(f"👑 <@{new_king}> is the new Bump King!",
                               ANNOUNCE_CHANNEL)
```

Per-fire worst case: 2 deletes (previous reminder + previous confirmation) + 2 role changes (mod_actions pool of 25) + 1 message — comfortably inside the admin budget. Dropped from legacy: the raw content/embed logging (the `HandlerRun` audit covers it), the startup history scan (#2), and `_clean_channel` bulk purge (#13 — per-message deletion plus targeted deletion of the previous reminder and previous confirmation replaces it).

**Scope change (deliberate, #3b):** legacy `on_message` routed Disboard messages **guild-wide** — a confirmed bump in any channel was still credited, with only the delete branch scoped to the bump channel. This handler is scoped to `channel_ids=[<bump channel>]`, so a `/bump` invoked in another channel produces a confirmation that never dispatches and is never credited. This is the intended behavior (bumping belongs in the bump channel; a guild-wide scope would also let arbitrary Disboard traffic fire an admin handler anywhere), but it is a parity break — flagged in the disposition table and drop list for Zech's confirmation.

**Handler 2 — `bump-reminder`** (schedule trigger, `interval_seconds: 300`, installed in the bump channel, `allowed_mention_role_ids=[<bumper role>]`):

```python
now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
last = await shared_get("last_bump_at")
# last is None on first install or after a memory wipe: treat as remind-now,
# so the loop bootstraps itself (replacing the legacy startup history scan's
# "last bump > 2h ago -> remind immediately" behavior — #2)
if (last is None or now - last >= 7200) and not await shared_get("reminded"):
    mid = await send_message(
        "<@&BUMPER_ROLE> It's been 2hrs since the last bump! "
        "Use the /bump command now!")
    await shared_set("reminder_message_id", mid)
    await shared_set("reminded", True)
```

Persistent memory makes this self-healing across restarts — no recovery scan. Worst-case reminder lateness is one poll interval (~5 min on a 2h cycle), which is fine. The `last is None` branch is the cold-start bootstrap: on first install or after a shared-memory wipe the reminder fires on the next poll rather than waiting for someone to bump unprompted. Known behavior delta vs the legacy history scan: if a bump actually happened <2h before a memory wipe, one early reminder goes out — accepted (rare, self-correcting on the next confirmed bump) rather than porting a channel-history read.

**Handler 3 — `bump-commands`** (message trigger, installed in a general/bot-commands channel — NOT the bump channel, where the cleaner would eat the command):

Command surface decision: the read-only queries keep lightweight text forms (`!bumpers`, `!bumps`) as exact-match branches — they are cheap, guarded, and channel-scoped, and porting them as handler branches is idiomatic here. The admin `!update bump king` becomes a branch gated on `author_is_admin` (E7); the alternative of dropping it for an authoring-chat re-fire is Q6.

```python
text = context["message_content"].strip().lower()
if text == "!bumpers":
    # rank 7-day counts; 🥇🥈🥉 for top three, ✨ for the rest; plain text
elif text == "!bumps":
    # group records under "N hours ago" headings with <t:...> markup
elif text == "!update bump king" and context["author_is_admin"]:
    # same recompute/transfer/announce path as the tracker
```

**Not ported:** Discord.me reminder (#15, defunct service), startup scan (#2, replaced by the remind-now cold-start branch), bulk purge (#13, replaced by targeted reminder + confirmation deletion), guild-wide Disboard routing (#3b, deliberate scope narrowing) — see §6.

### 4.2 Server-stat counters

The online-count half of this feature (presence aggregator, record tracker, `!online stats`) is dropped — see #20–#23. What remains is one handler.

**Handler 4 — `member-count-display`** (admin schedule handler, `interval_seconds: 600`, `channel_ids=[<stats voice/display channel>]`):

Interval widened from the legacy 90s: with change-gating, 90s ticks were no-ops anyway, and Discord allows 2 renames/10min — 600s keeps one rename per window even when the count moves every tick.

```python
count = await get_guild_member_count()
value = f"{count / 1000:.1f}k" if count >= 1000 else str(count)
name = f"📊Members: {value}"    # legacy prefix kept verbatim (📊, not 📈)
if await memory_get("last_counter") != name:      # change-gate before rename
    await rename_channel("<stats channel id>", name)
    await memory_set("last_counter", name)
```

Private per-handler memory suffices here (no reader pair), so no namespace. The legacy ready-time re-seed (#19) disappears: `last_counter` persists.

The `📊Members:` prefix matches the legacy format exactly (#17) — deliberately, because the emoji is load-bearing for the first tick after migration: `last_counter` starts empty, so the first fire always attempts a rename, and keeping the legacy string makes that rename resolve to the name the old bot left on the channel (an idempotent PATCH) instead of a visible format change. Any future prefix change is a display decision that costs one rename out of the 2/10min cap.

## 5. Implementation order & TDD notes

Phases are dependency-ordered; each is TDD (happy paths + critical failure paths) against the existing offline test patterns (fake emitter/limiter/agent-runner into `run_handler_script`, injectable author/judge for pipeline tests).

**Phase 1 — host-side primitives (pure, no Discord):**
1. `HandlerSharedMemory` model + migration + runtime `shared_*` functions (E3).
   Tests: namespace isolation across guilds; 16KB `CapExceeded("shared_memory_size")`; dirty-only persistence; functions raise when no namespace declared; last-writer-wins documented via a test that pins the behavior.
2. `discord_reads` budget counter + `HandlerRun.discord_reads` migration (E5 plumbing).
   Tests: breach at 5 raises `CapExceeded("discord_reads")`; counter in `usage()`.
3. `WindowedLimiter.hit(..., window_seconds=)` override + rename cap key (E4 plumbing).
   Tests: 2-per-600s window; window expiry fixed by first hit.

**Phase 2 — emitter + runtime surface:**
4. `rename_channel` (emitter PATCH, runtime function, admin-only, scope check, mod_actions spend, rename cap).
   Failure paths: non-admin handler lacks the function; out-of-scope channel id raises; third rename in a window raises with the cap name.
5. `allowed_mentions` rail on `create_message` + settings pass-through (E2).
   Failure paths: role id NOT in the allowlist does not ping (payload-level assertion); `@everyone` never parseable; default payload now always carries explicit `allowed_mentions` (regression test on existing handlers).
6. `get_guild_member_count` (E5).
   Failure paths: REST error propagates (no silent 0) — write these tests **first**, per the fail-fast rule.

**Phase 3 — dispatch changes (E1, E7):**
7. Bot-message opt-in: own-bot messages never dispatch (the invariant test); bot messages dispatch only to `include_bot_messages` handlers; `bot_message_channels` in the active-channels cache; `embeds` / `interaction_user_id` / `author_is_bot` / `author_is_admin` context fields (admin computed fail-closed on cache miss).

**Phase 4 — authoring surface + the handlers themselves:**
8. Author/judge prompt updates (new functions, settings, context fields, the named reject patterns: un-gated rename, un-guarded bot-message trigger, privileged branch without `author_is_admin`). Re-run the authoring evals.
9. Author the four handlers in the staging guild. Their scripts get direct unit tests via `run_handler_script` with seeded shared memory:
    - detection happy path (embed with "bump done!" → ledger insert, flags reset, previous reminder deleted, previous confirmation deleted, new `confirmation_message_id` stored);
    - confirmation rotation from empty state (no stored `confirmation_message_id` → no delete, id stored);
    - failed-bump path (no matching embed → delete, nothing else);
    - king tie-break (tie → crown moves only on more-recent bump; no-op when top unchanged);
    - reminder loop (fires once per cycle: `reminded` flag; no fire before 2h; cold start with `last_bump_at` absent → reminder fires on the next poll; self-heal from cold memory);
    - rename suppression (unchanged count → zero emits).

Run `semgrep` and `gitleaks` before each phase's commit, per convention.

## 6. Open questions / drop recommendations

**Drop recommendations (each needs Zech's confirmation):**

| Capability | Rationale |
|---|---|
| #2 Startup recovery scan | Superseded by persistent handler memory plus the reminder script's remind-now-on-missing-`last_bump_at` cold-start branch; would otherwise force a channel-history read function nothing else needs. Delta: a bump <2h before a memory wipe yields one early reminder. |
| #3b Guild-wide Disboard routing | Handler 1 is bump-channel-scoped, so confirmations in other channels are not credited (legacy credited them). Deliberate narrowing: keeps arbitrary Disboard traffic from firing an admin handler guild-wide. Parity break — confirm. |
| #13 Bulk channel purge | Superseded by per-message deletion + targeted deletion of the stored `reminder_message_id` and `confirmation_message_id` (#13b keeps the confirmation rotation, so old "Bump done!" messages do not accumulate); a purge function would be budget-hostile. |
| #15 Discord.me reminder | The listing service is effectively defunct in 2026. Trivially handler-today if revived (6h schedule + E2). |
| #19 Ready-time counter re-seed | Persistent `last_counter` memory makes it moot; worst case one redundant rename attempt, absorbed by the rename cap. |

**Decided drops (Zech, 2026-07-18) — no longer open:**

| Capability | Decision |
|---|---|
| #20–#23, #26 — the entire online-count family (presence aggregation, max-online record, record broadcast, `!online stats`, `GUILD_PRESENCES` intent) | Dropped. The only bot-core piece in this group plus a privileged intent was not worth one display feature. |
| #24 `!set max online channel` | Moot after the family drop (was already recommended for dropping: the broadcast channel would have been the handler's channel binding). |

**Open questions:**

1. **Is Disboard bumping wanted for the smarter-dev guild at all?** All legacy ids except Disboard's bot user are Beginner.Codes-specific; new bump channel / bumper role / crown role / announcement channel ids are needed at authoring time.
2. **Bytes-economy integration:** should bumps award bytes instead of (or alongside) the parallel bump ledger, letting `/bytes`-style surfaces supersede `!bumpers`/`!bumps`? This plan keeps the standalone ledger for parity.
3. **Tie-break rule:** keep the legacy crown-moves-on-tie-only-if-more-recent rule (planned), or simplify to strict-majority-changes-crown?
4. **Shared-memory tier:** confirm admin-handlers-only namespaces for now, and last-writer-wins concurrency (with per-key UPSERT as the documented upgrade path).
5. **Embeds out:** announcements/leaderboards were embeds in legacy; this plan ships formatted plain text. Is a `send_embed` extension worth designing across the whole migration (it would serve several groups), or is text fine?
6. **`!update bump king`:** keep as an `author_is_admin`-gated handler branch (planned), or drop it and rely on asking the admin authoring chat / dashboard to intervene when drift happens?
7. **Command UX:** keep `!bumpers` / `!bumps` as text-command handler branches (planned, cheapest parity), or fold the queries into the mention/chat agent for consistency with smarter-dev UX?
8. **Rename target rail:** planned as `rename_channel(channel_id, name)` restricted to the handler's `channel_ids` scope. Alternative, stricter rail: no `channel_id` parameter at all — always rename the bound channel. Preference?
9. **EXISTING-FEATURES.md correction:** line 255 claims `GUILD_PRESENCES` is enabled; `bot/client.py` does not enable it. The intent is no longer needed (online-count family dropped), but the doc line should be fixed.
