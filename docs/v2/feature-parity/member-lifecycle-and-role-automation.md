# Member Lifecycle & Role Automation

**Feature group:** legacy-bot migration — onboarding (beginner.codes `NewMemberExtension`),
celebration & engagement (`boosters.py` / `premium_members.py` / `birthday.py`), and the
beginner.py `!sus` command.

**Source docs (ground truth):**
- `beginner.codes-bot/docs/features/onboarding.md`
- `beginner.codes-bot/docs/features/celebration-engagement.md`
- `beginner.py-bot/docs/prod-functionality/02-sus-command.md`

## 1. Overview

These three legacy features all ride the same two rails that the handler system does not
have yet: **member gateway events** (join, rules acceptance, role changes) as triggers, and
**role mutation** (add/remove a role on a member) as an emit. The third shared need is a
**durable one-shot timer a script can arm itself** — onboarding promotes a member 2 days
after rules acceptance, and `!sus` un-flags a member 1 day after flagging. Both legacy bots
solved that with volatile in-memory timers plus restart-reconciliation hacks (ready-event
sweeps, label backfills, DB-pickled scheduler rows); a persisted `schedule_timer` makes the
timer re-arm machinery obsolete in one move. One caveat the timers do NOT fix: a gateway
event the bot never *received* (a member joins or accepts rules while the bot is down)
never arms a timer at all — the legacy ready-sweeps also repaired that failure mode, and
E6 carries that responsibility forward.

Designing the member-event triggers as one family and add_role/remove_role as one metered
extension (with `ban_user` gaining its message-purge window as the high-privilege sibling)
gives us three features for the price of one coherent design instead of three divergent
ones. Almost everything here lands as **handler-extension**: the extensions are small and
well-railed, and the handlers themselves are short scripts an author would produce
conversationally. Only a large pile of legacy restart-repair code and config commands gets
dropped — each with the superseding mechanism named.

A pleasant discovery from reading the actual code: several "open questions" from the
per-feature analyses are already answered by the existing system.

- **Guild-wide output-channel binding** — admin handlers already send to any channel
  (`send_message(content, channel_id)` works when an `AdminActor` is present,
  `handler_runtime.py:_send_message`), and per-channel handlers have their home channel.
  For guild-scoped triggers on standard handlers, the handler's own channel *is* the
  output binding — no new mechanism needed.
- **`ban_member`** — `ban_user(user_id, reason)` already exists for admin handlers
  (`admin_actions.py`), metered by `spend_mod_action` under `ADMIN_MAX_MOD_ACTIONS`. The
  extension shrinks to one optional parameter (`delete_message_seconds`).
- **Shared memory across handlers** — the durable-timer design removes the biggest
  consumer (join handler + promotion sweep no longer share a joined-timestamp store; the
  timer payload carries the member id). What remains (milestone counter reset) is thin
  enough to drop rather than build a shared-memory namespace for.

**Overlap check:** `docs/EXISTING-FEATURES.md` describes an `automod.py` plugin doing
join-time username filtering (regex + account age + avatar) — **`bot/plugins/` contains no
`automod.py` today; that doc is stale.** No live smarter-dev feature bans on join,
announces role gains, tracks member-count milestones, or does staged onboarding roles.
Bytes role rewards and squads prove the bot-core REST role-mutation pattern we're lifting
into `AdminActor`, and `timeout.py`/`warn.py`/`mod_monitor` are precedent for moderation
surfaces, but none overlap in behavior.

## 2. Feature disposition table

Dispositions: `handler-today` (authorable right now), `handler-extension` (needs an
extension from §3), `bot-core`, `drop` (recommend not porting — final call is Zech's).

### codes-onboarding

| Capability | Disposition | Justification |
| --- | --- | --- |
| Bot-account detection heuristic (`_is_bot`) | handler-extension | Pure string/date logic in the script, but needs member_join context fields (username, nickname, account_created_at, has_custom_avatar); regex re-expressed as string ops (Monty has no `re`). |
| Auto-ban bot accounts on join (`auto_ban_bots`) | handler-extension | member_join trigger (E1) + `delete_message_seconds` on ban_user (E2); admin handler logs to the mod channel via existing cross-channel send. |
| Bot re-check on rules acceptance (`member_accepts_rules`) | handler-extension | member_rules_accepted trigger (E1); ban path shared with join auto-ban; preserve-or-unify asymmetry is a script constant (open question Q5). |
| Onboarding step 1: holding role on rules acceptance | handler-extension | member_rules_accepted trigger (E1) + `add_role` (E2); the legacy 3 s race-workaround sleep is dropped (worker fires are already delayed past the gateway event). |
| Onboarding step 2: promotion after 2 days | handler-extension | `schedule_timer(2 days, {user_id})` (E3) + add_role/remove_role (E2); replaces the legacy volatile `call_later` AND the proposed memory-scan sweep — one handler, restart-proof. |
| Per-member joined-timestamp persistence (`joined` label) | drop | Superseded: the timer payload carries the member id, so no per-member store exists to size-cap or share; Discord's own `joined_at`/context covers audit needs. |
| Ready-time reconciliation (`onboard_new_members`) — timer re-arm portion | drop | Re-arming volatile timers from `joined` labels is obsolete by construction with DB-persisted timers (E3). |
| Ready-time reconciliation (`onboard_new_members`) — downtime recovery + bot-ban checkpoint | handler-extension | The sweep did more than re-arm timers: it onboarded holding-role holders whose acceptance the bot never processed and re-ran the bot check (third ban checkpoint) on them. Persisted timers do NOT fix missed gateway events. Ported as the daily `onboarding-reconcile` admin handler using `get_role_members` (E5, now in scope) — see E6. |
| Restart role backfill (`assign_missing_roles`) | bot-core (thin, E6) | Recovers members who went `pending → not pending` during bot downtime — a **missed `member_rules_accepted` event**, not a lost timer, so no handler can be triggered by it. E6's startup replay synthesizes the missed trigger through the normal dispatch path — it assigns no roles itself (trigger synthesis only, confirmed 2026-07-18); the onboarding handler is the single authority for role assignment. Only the "Restarted and added roles to N members" announcement is dropped. |
| Member-count milestone announcements (`check_for_highscore`) | handler-extension | member_join trigger with `guild_member_count`/`guild_human_member_count` context (E1/E5); high-water mark + re-baseline logic fits per-handler memory; Giphy replaced by a hardcoded gif list + `choice()`. |
| Milestone suppression during lockdown | drop | smarter-dev has no lockdown concept to gate on; if a lockdown feature lands later, add a memory flag then. |
| `!reset member counter` command | drop | Rare admin op; deleting/re-creating (or conversationally re-authoring) the milestone handler resets its memory — no shared-memory extension warranted for this alone (Q6). |
| `!set welcome channel` command | drop | Configures a flow that is disabled in prod; channel placement is the handler system's own model. |
| Welcome messages / unwelcomed tracking / welcome-reward flow | drop | Consciously disabled in prod ("testing a proper introductions channel"); if ever revived it's a trivial member_join handler-today once E1 exists. |

### codes-celebration-engagement

| Capability | Disposition | Justification |
| --- | --- | --- |
| Server-boost announcement | handler-extension | member_role_change trigger with `is_boost_role_added` + boost-stat context fields (E1/E5). Legacy embed shows **both** total boosts AND the number of boosting members (distinct figures — one member can boost multiple times); context carries `premium_subscription_count` and `boosting_member_count` so the handler preserves both. Authored as a standard handler *in* the booster channel (placement = channel config). |
| Boost re-fire guard (only on not-boosting → boosting transition) | handler-extension | Once-per-transition semantics are baked into member_role_change dispatch (bot computes the delta from its cache; no delta, no fire) — scripts never re-implement it. |
| `!set booster channel` command | drop | Superseded by per-channel handler placement: author the announcement handler in the channel that should receive it. |
| `!test booster channel` command | drop | Superseded by the authoring pipeline's review loop; if a live test is wanted, an admin-gated `test boost announcement` message branch in the same handler is handler-today. |
| Premium-member announcement | handler-extension | Same member_role_change trigger; match `added_role_ids` against a role-id constant (switching off legacy name-matching, Q9); `role_member_counts` context for the total line. |
| `!test premium member message` command | drop | Same rationale as the booster test command. |
| Server-birthday annual message (Nov 13, 01:00 UTC) | **handler-today** | Existing `schedule` trigger with `daily_time: "01:00"`; script self-filters to Nov 13 and computes the ordinal from `date.today().year - 2020`. Zero extensions — author it now. |
| Birthday timer restart re-arm | drop | Obsolete by construction: schedule triggers are DB-backed and re-enqueued by the worker (`handlers_jobs._reschedule`). |
| Missing-booster-channel failure mode | drop | Structurally eliminated: handlers always have a bound channel, and erroring fires already post a throttled notice (`handler_notify.py`). |

### py-sus-command

| Capability | Disposition | Justification |
| --- | --- | --- |
| `!sus` self-flag (default target) | handler-extension | Guild-wide admin message handler; needs `add_role` (E2) + `schedule_timer` (E3). |
| Privileged multi-target `!sus` via mentions | handler-extension | Needs `mentioned_user_ids` + `author_role_ids` message-context fields (E4); privilege maps to the guild moderator role, not `view_guild_insights` (Q10). |
| Non-privileged mentions ignored (self-only fallback) | handler-extension | Pure script branch once `author_role_ids` exists (E4). |
| Re-sus does not extend the timer | handler-today | Presence check on the handler's memory expiry map — skip add_role/schedule_timer, still send the message. |
| Privileged user, no mentions → silent no-op | handler-today | Pure script branch; recommend NOT keeping bug-compatibility — let privileged users self-sus (Q11). |
| `!list_sus` viewer | handler-today | Renders the memory expiry map (pruned of past-due entries) as plain text; embed → markdown downgrade accepted for a joke feature (Q12); ground-truth `get_role_members` deferred (Q7). |
| Scheduled automatic role removal (`remove_sus`) | handler-extension | Timer refire with payload `{user_id}` (E3) + `remove_role` returning False on member-gone instead of erroring the fire (E2). |
| Pending removal persists across restarts | handler-extension | Acceptance requirement on E3, not a behavior to script: timers live in the persistent worker job store (JSON payloads, never pickle) and are proven by a restart-recovery test. |
| Moderation cross-reference: `!history`/`!whois` "Are They Sus?" field reads the 🚨sus🚨 role | handler-today (by construction) | The handler mutates the **real role**, not just memory, so any surface reading the role stays ground-truth accurate — the accepted memory-vs-role drift (Q7) is confined to `!list_sus` output and never touches moderation reads. Flip side for Q15: if `!sus` is not ported at all, any ported whois/history equivalent has a permanently empty sus field. |

## 3. Handler-system extensions

Six extensions, ordered by how many capabilities they unblock. All schema changes go
through alembic; all new limits get named constants in `handler_caps.py` /
`handler_budget.py`; author/judge prompts and `describe_trigger` learn every new trigger
and function so the judge can keep reasoning about frequency and blast radius.

### E1 — Member-event trigger family: `member_join`, `member_rules_accepted`, `member_role_change`

Three *narrow* trigger types instead of one overloaded `member_update`. Narrow triggers
keep dispatch filtering exact (a rules-acceptance handler never fires on a nickname
change), keep the judge's frequency reasoning honest, and make `guards_effective` checks
meaningful.

**Bot-side dispatch** (`bot/plugins/handler_events.py`): new listeners on
`hikari.MemberCreateEvent` and `hikari.MemberUpdateEvent`. The bot already runs with the
privileged `GUILD_MEMBERS` intent. All delta computation happens bot-side against the
cached previous member state, as pure functions
(`member_update_deltas(old_member, new_member) -> list[tuple[trigger_type, context]]`) so
they are trivially TDD-able:

- `member_join` — fires on every human join (bots included, flagged, so anti-raid handlers
  can see them; `is_bot` in context).
- `member_rules_accepted` — fires only on the `pending: true -> false` transition.
- `member_role_change` — fires only when the role *set* actually changed; context carries
  the delta.

**Cache-miss policy is per-trigger, because the fail-safe direction differs.** For
`member_role_change`, a missing `old_member` skips the event (no delta ⇒ no fire) — a
boost announcement not sent is a recoverable non-event, and this skip is what makes the
re-fire guard structural. For `member_rules_accepted` the same skip would be
fail-**dangerous**: silently dropping the pending→false transition leaves the member
permanently un-onboarded with no recovery path (no holding role, no timer, nothing ever
looks at them again). So rules acceptance is **at-least-once**: on a cache miss, the
trigger fires iff the new member is not pending AND holds no roles beyond `@everyone` —
a member onboarded any earlier time always holds a role, so warm-guild nickname/avatar
updates cannot misfire. Duplicate fires are therefore possible and documented; the judge
requires `member_rules_accepted` handlers to be idempotent (re-adding a role is a Discord
no-op; a duplicate promotion timer refires into two no-ops). Members this heuristic still
misses (event lost entirely, not just the cache) are caught by E6.

**Context shapes** (all ids as strings, times ISO-8601 UTC, matching existing contexts):

```python
# member_join
{
    "trigger_type": "member_join",
    "member_id": "...", "username": "...", "display_name": "...",
    "is_bot": False,
    "account_created_at": "...",          # from the snowflake, as message triggers do
    "has_custom_avatar": True,
    "guild_member_count": 12345,          # Discord's count (includes bots), cheap
    "guild_human_member_count": 11987,    # non-bot count from the member cache (legacy
                                          # milestone semantics); best-effort, short-TTL cached
}

# member_rules_accepted
{
    "trigger_type": "member_rules_accepted",
    "member_id": "...", "username": "...", "display_name": "...", "nickname": None,
    "account_created_at": "...", "has_custom_avatar": True,
    "joined_at": "...",
}

# member_role_change
{
    "trigger_type": "member_role_change",
    "member_id": "...", "member_display_name": "...",
    "added_role_ids": ["..."], "added_role_names": ["..."],
    "removed_role_ids": ["..."], "removed_role_names": ["..."],
    "is_boost_role_added": True,          # premium_subscriber_role match done bot-side
    "premium_subscription_count": 14,     # guild boost count (total boosts), frozen at dispatch
    "boosting_member_count": 9,           # members holding premium_subscriber_role, from cache
                                          # (≤ total boosts: one member can boost multiple times)
    "role_member_counts": {"role_id": 42, ...},  # counts for added roles, from cache
}
```

**Tier availability & routing:** available to BOTH tiers. These are guild-scoped events
with no channel, so `dispatch_event` gains a branch: for member-event trigger types the
standard-tier query matches `ChannelHandler.guild_id` instead of `channel_id`, and each
matched handler fires with its own home channel as the output (the fire job already uses
`record.channel_id`). Admin handlers match as today (guild + scope). This is the answer to
the output-channel-binding question: *per-channel authoring places the announcement*.
`active_channels` adds `(guild_id, trigger)` entries for standard member-event handlers so
the bot's cheap dispatch guard keeps working, and the bot calls `_dispatch` with
`channel_id=""` for member events.

**Caps:** `fires_per_min_for_trigger` returns 10 for member-event triggers (same as
message). New per-guild dispatch window for join bursts:
`GUILD_MEMBER_EVENTS_PER_MIN = 60` on key `hcap:memberevt:{guild_id}` — checked in
`dispatch_event` before enqueueing, so a raid degrades to declined dispatches rather than a
fire queue explosion (the milestone/high-water logic self-heals on the next join).

**Migration:** one alembic revision — drop and recreate
`ck_channel_handlers_trigger_type` and `ck_admin_handlers_trigger_type` with the extended
tuple, and extend `HANDLER_TRIGGER_TYPES` in `models.py`. Member-event triggers join
`HANDLER_EVENT_TRIGGERS` behavior (single-fire dispatch; they coexist with time triggers).

**Lint/judge:** `describe_trigger` gains honest frequency lines ("fires on EVERY member
join — bursts hard during raids"; "fires only when a member's roles actually change —
low frequency"). Judge `guards_effective` examples updated: a member_join handler that
messages unconditionally on every join is spam at raid frequency and must be rejected
unless the channel is explicitly a join-log.

**Consumed by:** bot heuristic, join auto-ban, rules re-check, holding role, milestones
(member_join / member_rules_accepted); boost + premium announcements, re-fire guard
(member_role_change).

### E2 — Role mutation: `add_role` / `remove_role` (+ ban message-purge window)

**Admin-tier only.** Role grants change what a member can do; that stays behind the
admin-authored, dual-judged tier — standard handlers never see these functions (mirroring
how `ban_user`/`kick_user` already work: the functions are only injected when
`self.actor is not None`).

`AdminActor` gains two REST methods (same shape as bytes/squads role code, now in the
worker tier):

```python
async def add_role(self, user_id: str, role_id: str, reason: str | None = None) -> bool
    # PUT  /guilds/{guild_id}/members/{user_id}/roles/{role_id}
async def remove_role(self, user_id: str, role_id: str, reason: str | None = None) -> bool
    # DELETE /guilds/{guild_id}/members/{user_id}/roles/{role_id}
```

**Member-gone semantics (critical failure path):** a 404 Unknown Member returns `False`
instead of raising — the `!sus` removal and the 2-day promotion must be silent no-ops when
the member left. Any other REST failure raises `AdminActionError` (fail fast). This is an
explicit, tested contract, not a blanket `except`.

**ban_user gains the purge window** (the onboarding auto-ban needs it):

```python
async def ban_user(self, user_id: str, reason: str | None = None,
                   delete_message_seconds: int = 0) -> str
    # PUT /guilds/{guild_id}/bans/{user_id}  json={"delete_message_seconds": ...}
```

**Budget:** a new counter, separate from `mod_actions` (role changes are routine
lifecycle plumbing; bans are not — sharing a pool would let a promotion burst starve a ban
or vice versa): `max_role_changes` / `spend_role_change()`, defaults
`DEFAULT_MAX_ROLE_CHANGES = 0`, `ADMIN_MAX_ROLE_CHANGES = 10` (promotion = 2 ops/fire,
multi-target sus ≤ a handful). `usage()` and `HandlerRun` gain `role_changes` (column in
the same migration as E1).

**Windowed cap:** `GUILD_ROLE_CHANGES_PER_MIN = 30` on `hcap:rolechg:{guild_id}` —
enforced in the runtime wrapper before the REST call, breaching raises `CapExceeded`
mid-flight like channel messages do.

**Rails / judge:** role ids must appear as **literal string constants** in the script
(judge instruction + a lint check that `add_role`/`remove_role` second arguments are
string literals — same spirit as the opaque-blob ban: reviewable targets only). Judge
`actions_appropriate` extends: granting a role must be conditional on trigger context, the
role's purpose must be stated in the request, and `ban_user` calls require a non-empty
`reason`. The admin dual-judge panel already covers this tier. Optional hard rail (Q8): an
`allowed_role_ids` list in the handler's `settings`, enforced host-side in the runtime —
recommended, cheap, and makes the judge's job verifiable.

**Consumed by:** join auto-ban + rules re-check (ban window), holding role, promotion,
sus flag/unflag.

### E3 — `schedule_timer(delay_seconds, payload)` — persisted one-shot self-refire

The script-facing function that retires every restart-reconciliation hack in this group:

```python
schedule_timer(delay_seconds: int, payload: dict) -> True
```

**Semantics:** enqueues a fire of the *same* handler at `now + delay_seconds` via
`worker_submit(HandlerFirePayload | AdminHandlerFirePayload, scheduled_for=...)` — exactly
the machinery recurring schedules already ride (`handlers_jobs._reschedule`). The refire's
context is:

```python
{"trigger_type": "timer", "payload": {...}, "scheduled_at": "<ISO when armed>"}
```

so one handler can serve an event trigger AND its own delayed follow-ups by branching on
`context["trigger_type"]` (the fire job runs the script with whatever context the payload
carries; nothing checks it against the row's trigger_type — verified in
`handlers_jobs.run_handler_fire`). Payload must be JSON-serializable (validated at call
time via the same rule as `HandlerMemory.set` — non-JSON raises `ValueError`, failing the
fire loud) and size-capped at 4 KB. **JSON, never pickle** — the legacy DB-pickled
scheduler is exactly what we are not rebuilding.

**Bounds:** `delay_seconds` clamped to `[60, 30 * 86400]` (below/above raises
`ScheduleError` through the cap path). Per-fire budget counter `max_timers` /
`spend_timer()`: `DEFAULT_MAX_TIMERS = 2`, `ADMIN_MAX_TIMERS = 5` (multi-target sus).
Windowed per-handler arming cap: `HANDLER_TIMERS_PER_HOUR = 30` on
`hcap:timersched:{handler_id}` with a 3600 s window (the limiter already parameterizes
`window_seconds`; the error-notice throttle uses the same trick). `HandlerRun` gains a
`timers_scheduled` column.

**Deletion/disable safety:** no new cancellation bookkeeping. A refire of a deleted or
disabled handler already no-ops (`run_handler_fire` returns `"missing"`), so orphaned
timers are harmless — documented behavior, covered by a test. No new table needed.

**Persistence requirement (acceptance criterion, not a nice-to-have):** timers must
survive worker restarts. `skrift.workers` ships both a SQLAlchemy-backed and a Redis
job store; the recurring-schedule chain already depends on scheduled jobs surviving, but
**phase 1 includes verifying the deployed backend is the persistent one** and a
restart-recovery integration test (arm a timer, cycle the worker, assert the fire).

**Lint/judge:** author prompt documents the trigger_type-branch pattern; judge checks
that a script calling `schedule_timer` handles `context["trigger_type"] == "timer"`
(otherwise the refire is a guaranteed error-notice) — this slots into `sandbox_valid` /
`guards_effective`.

**Consumed by:** onboarding promotion (2-day), sus removal (1-day), and it retires:
joined-timestamp store, the timer re-arm halves of ready-time reconciliation and restart
role backfill (their downtime-recovery halves move to E6), sus restart re-arm.

### E4 — Message-trigger context additions: `mentioned_user_ids`, `author_role_ids`

Two cheap fields added where the message context is built
(`handler_events.py:on_message`):

```python
"mentioned_user_ids": [str(uid) for uid in msg.user_mentions_ids],  # user mentions ONLY
"author_role_ids": [str(rid) for rid in event.member.role_ids],
```

`mentioned_user_ids` containing only *user* mentions reproduces the legacy
skip-non-Member-mentions edge by construction. `author_role_ids` lets scripts gate on the
guild's moderator role (the smarter-dev analog of `view_guild_insights`; the Guild model
already carries a moderator-role concept). We expose the raw role list rather than a
derived `author_is_moderator` boolean so authoring doesn't need per-guild config wiring —
the moderator role id becomes a script constant the admin states while authoring, which
the judge can see. No migration; no cap impact (context is free). Judge note: privilege
gates must compare against role-id constants, never role names.

**Consumed by:** privileged multi-target sus, non-privileged fallback; generally useful to
every future admin message handler (mod-gated commands).

### E5 — Guild-stat context fields (not read functions)

The counts the celebration/milestone handlers need — `guild_member_count`,
`guild_human_member_count`, `premium_subscription_count`, `role_member_counts` — are
**frozen into the trigger context at dispatch** (shapes in E1), computed bot-side from the
gateway cache where the data already lives. No new metered read function, no
budget/cap treatment, no worker REST fan-out; the worker tier stays gateway-free. The
trade-off (counts are dispatch-time snapshots, not mid-script-fresh) is exactly what the
legacy announcements displayed anyway. If a future feature needs fresh mid-script reads,
that's a separate metered `get_*` gather-function design — explicitly out of scope here.

**In scope after all (admin-tier read):** `get_role_members(role_id) -> list[dict]`
gather function — originally deferred behind Q7, now **required** by E6's daily
`onboarding-reconcile` sweep. Worker-tier implementation (no gateway): paginated
`GET /guilds/{gid}/members` filtered by role id, result hard-capped (first 200 matches,
each `{"member_id", "display_name", "username", "joined_at", "account_created_at",
"has_custom_avatar", "pending"}`), metered like a web read, admin-tier only. Holding-role
populations are small, so the cap is a rail, not a constraint. Q7 narrows to whether
`!list_sus` *also* switches to it for ground truth — a nice-to-have, no longer a blocker.

### E6 — Missed-event recovery: startup rules-acceptance replay + daily reconcile sweep

Persisted timers (E3) fix lost *timers*; a gateway event the bot never **received** is a
different failure mode. A member who joins or accepts rules while the bot (or the dispatch
path) is down gets no holding role and no promotion timer, and nothing in E1–E5 ever looks
at them again. Legacy repaired this at ready-time with `assign_missing_roles` +
`onboard_new_members`; the port needs an equivalent, split by mechanism:

**Startup replay (bot-core, deliberately thin — trigger synthesis ONLY).** Confirmed by
Zech (2026-07-18): the bot-core piece performs **no role mutations and no feature behavior
of any kind** — it exists solely to fire the appropriate handlers for events the bot
missed. All role assignment lives in the onboarding handler's script via `add_role` (E2).
On shard ready, after member chunking completes, a pure function
`find_missed_rules_acceptances(members) -> list[context]` selects cached members with
`pending == False` and no roles beyond `@everyone`, and dispatches a synthetic
`member_rules_accepted` for each through the **normal** dispatch path (`_dispatch`). The
onboarding handler stays the single authority — its rules-acceptance bot re-check runs on
every replayed member, which also preserves the legacy ready-sweep's third bot-ban
checkpoint for role-less members. Replay contexts carry `"is_reconciliation": True` so
scripts and the judge can distinguish them. Replays *pace themselves* against
`GUILD_MEMBER_EVENTS_PER_MIN` (wait for window headroom rather than being declined) — a
decline here would lose the member a second time, and a post-downtime backlog draining
over a few minutes is exactly the right behavior. This is genuine bot-core per the
preference order: "all non-pending role-less members" is gateway-cache-only state that no
honest handler read function can express, and the logic is a selector + re-dispatch, not
feature behavior.

**Daily reconcile sweep (handler-extension).** Admin handler `onboarding-reconcile`
(sketch in §4.1), trigger `schedule` with a `daily_time`, using
`get_role_members(HOLDING_ROLE)` (E5): for each holder, ban if the bot heuristic matches
(the legacy ready-sweep checkpoint), promote immediately if `joined_at` is more than
2 days past (covers a lost or never-armed promotion timer), otherwise do nothing — the
persisted timer handles the happy path. Idempotent by construction: no timer re-arm, no
duplicate bookkeeping, safe to run every day forever.

**Consumed by:** downtime recovery for onboarding (replaces `assign_missing_roles` and
the non-timer half of `onboard_new_members`); the cache-miss at-least-once policy in E1
is its event-time complement.

## 4. Per-feature plans

All legacy hardcoded ids (roles, channels) become **script constants stated during
authoring** — the author echoes them into the script, the judge sees them, and the handler
lives in (or targets) the channel they referred to. Legacy `!`-prefix commands are ported
as message-trigger branches only where the command is a real member-facing feature
(`!sus`, `!list_sus`); pure config/test commands are dropped in favor of the authoring
pipeline itself.

### 4.1 Onboarding & new members (beginner.codes)

Three admin handlers plus one standard handler. All admin handlers are guild-scoped
(`channel_ids: []`) except where noted.

**Admin handler `join-gate`** — trigger `member_join`:

```python
# constants: MOD_LOG_CHANNEL = "719311864479219813" (or current mod-log)
FRUIT = ["mango", "pear", "cherry", ...]  # 16 names

def looks_generated(username):
    # legacy regex ^[A-Za-z0-9]+_\d[A-Za-z0-9_.]+$ re-expressed as string ops
    # (Monty has no `re`): split on first "_", head alnum and non-empty,
    # tail starts with a digit and uses only [A-Za-z0-9_.]
    ...

def is_bot_account(ctx):
    name = ctx["username"].lower()
    display = (ctx["display_name"] or "").lower()
    age_days = days_between(ctx["account_created_at"], datetime.now(utc))
    if looks_generated(ctx["username"]) and age_days < 180 and not ctx["has_custom_avatar"]:
        return True
    if "announcement" in name or "announcement" in display:
        return True
    return any(name.endswith(f) or display.endswith(f) for f in FRUIT)

if is_bot_account(context):
    ban_user(context["member_id"], reason="bot-account heuristic on join",
             delete_message_seconds=3600)
    send_message("banned suspected bot account " + context["username"], channel_id=MOD_LOG_CHANNEL)
```

Rollout note: the fruit-name rule is a 2021-era heuristic with real false-positive risk in
2026. **Author it in log-only mode first** (report to mod-log, no ban) and flip the ban
line on after a week of observed hits (Q4).

**Admin handler `onboard-and-promote`** — trigger `member_rules_accepted`, branching on
refire:

```python
HOLDING_ROLE = "888160821673349140"
FULL_ROLE = "644325811301777426"

if context["trigger_type"] == "member_rules_accepted":
    # legacy asymmetry: re-check only the regex+age+avatar rule (Q5)
    if pattern_one_matches(context):
        ban_user(context["member_id"], reason="bot-account heuristic at rules acceptance",
                 delete_message_seconds=3600)
    else:
        add_role(context["member_id"], HOLDING_ROLE, reason="onboarding: rules accepted")
        schedule_timer(2 * 86400, {"member_id": context["member_id"]})
elif context["trigger_type"] == "timer":
    member_id = context["payload"]["member_id"]
    added = add_role(member_id, FULL_ROLE, reason="onboarding: 2-day promotion")
    if added:
        remove_role(member_id, HOLDING_ROLE, reason="onboarding: 2-day promotion")
```

No joined-timestamp store, no restart re-arm logic. `add_role` returning `False`
(member left) skips the removal — silent no-op, the required failure semantics. The
legacy 3-second sleep and the "promote immediately if 2 days already elapsed" branch both
disappear from *this* handler (the overdue-promotion branch lives on in the reconcile
sweep below, where it belongs). This handler must be idempotent — E1's at-least-once
rules-acceptance and E6's startup replay can both deliver a duplicate fire, and it is:
re-adding the holding role is a Discord no-op and a duplicate timer refires into two
no-ops.

**Admin handler `onboarding-reconcile`** — trigger `schedule` (`daily_time`), the E6
downtime-recovery sweep, replacing the non-timer half of legacy `onboard_new_members`:

```python
HOLDING_ROLE = "888160821673349140"
FULL_ROLE = "644325811301777426"
MOD_LOG_CHANNEL = "719311864479219813"

for member in get_role_members(HOLDING_ROLE):
    if is_bot_account(member):                      # legacy ready-sweep ban checkpoint
        ban_user(member["member_id"], reason="bot-account heuristic in reconcile sweep",
                 delete_message_seconds=3600)
        send_message("banned suspected bot account " + member["username"],
                     channel_id=MOD_LOG_CHANNEL)
    elif days_between(member["joined_at"], datetime.now(utc)) >= 2:
        # promotion timer lost/never armed — promote now
        if add_role(member["member_id"], FULL_ROLE, reason="onboarding: reconcile promotion"):
            remove_role(member["member_id"], HOLDING_ROLE, reason="onboarding: reconcile promotion")
    # else: not yet due — the persisted timer will handle it; touch nothing
```

Members who accepted rules during downtime and never got the holding role are outside any
role sweep's reach — they are recovered by the E6 **startup replay** (bot-core), which
re-dispatches synthetic `member_rules_accepted` events into `onboard-and-promote`.
Between the two, every pending→not-pending member provably enters onboarding: seen live →
E1 trigger; cache-missed → E1 at-least-once fire; missed entirely → startup replay; stuck
in holding → daily sweep.

**Standard handler `member-milestones`** — authored in the announcements channel, trigger
`member_join`, memory: `{"highest": int}`:

```python
count = context["guild_human_member_count"]
high = memory_get("highest", 0)
if count > high:
    memory_set("highest", count)
    if count // 250 > high // 250:
        if count % 10000 == 0:
            send_message(FESTIVE_BANNER.format(count))
            send_message(choice(CELEBRATION_GIF_URLS))
        else:
            send_message("We just passed " + str(count) + " members! 🎉")
elif count < high - 20:
    memory_set("highest", count)   # legacy re-baseline after purges/raids
```

Giphy API → hardcoded gif URL list + `choice()` (already available). Lockdown suppression
dropped (nothing to gate on). Counter reset = delete/re-create or re-author the handler.

**Dropped outright:** the timer re-arm halves of ready reconciliation / restart backfill
(their downtime-recovery halves are ported via E6 above) and the restart-backfill
announcement line; welcome flow + its config command (§6).

### 4.2 Celebration & engagement (beginner.codes)

**Standard handler `boost-announcement`** — authored in the booster channel, trigger
`member_role_change`:

```python
if context["is_boost_role_added"]:
    boosts = context["premium_subscription_count"]
    boosters = context["boosting_member_count"]
    send_message(
        "💖 **" + context["member_display_name"] + " Has Boosted The Server!!!**\n"
        "That makes " + str(boosts) + " boosts from " + str(boosters)
        + " boosting members — thank you! 🚀"
    )
```

Both legacy stats are preserved: **total boosts** (`premium_subscription_count`) and
**number of boosting members** (`boosting_member_count`, E1/E5) — distinct figures, since
one member can boost multiple times. The transition guard lives in the trigger (E1 delta
semantics), not the script. Plain markdown replaces the pink embed + GitHub-raw thumbnail
— that *formatting* downgrade is the only one, pending Q13 (a `send_embed` emitter is
deliberately NOT part of this group's extensions); no stat content is lost.

**Standard handler `premium-member-announcement`** — same channel and trigger:

```python
PREMIUM_ROLE_ID = "<current role id — resolve at authoring time>"
if PREMIUM_ROLE_ID in context["added_role_ids"]:
    total = context["role_member_counts"].get(PREMIUM_ROLE_ID, 0)
    send_message(
        "✨ **" + context["member_display_name"] + " Has Become a Premium Member!!!** "
        "<:foxaw:...>\nThat makes " + str(total) + " premium members!"
    )
```

Matching switches from role **name** to role **id** (rename-proof, spoof-proof — Q9). The
author's emoji-lister tool resolves `foxaw`.

**Standard handler `server-birthday`** — **handler-today, ship immediately**: authored in
the announcements channel, trigger `schedule`, settings `{"daily_time": "01:00"}`:

```python
today = date.today()
if today.month == 11 and today.day == 13:
    n = today.year - 2020
    send_message(BIRTHDAY_GIF_URL)
    send_message("🎂 Happy " + ordinal(n) + " birthday to the server!! 🎉")
```

Two sends fit the 3-message budget; daily no-op fires are one cheap date check, 364 days a
year, well under every cap. `memory_set("last_fired_year", ...)` optional as a double-fire
guard. Restart re-arm machinery: nothing to port. Note: the emitter suppresses link-preview
embeds (`_SUPPRESS_EMBEDS`), so gif *links* won't unfurl — pick a plain-text banner or
accept a bare URL (Q14).

**Dropped:** both `!set`/`!test` commands and the missing-channel failure mode (§6).

### 4.3 `!sus` (beginner.py)

**One admin handler `sus`** — trigger `message`, scope all channels
(`ADMIN_FIRES_PER_MIN = 120` absorbs guild-wide message volume; the first line is a cheap
prefix guard). Memory: `{user_id: expiry_iso}`.

```python
SUS_ROLE = "<🚨sus🚨 role id — resolve at authoring time>"
MOD_ROLE = "<guild moderator role id>"

if context["trigger_type"] == "timer":
    user_id = context["payload"]["user_id"]
    remove_role(user_id, SUS_ROLE, reason="sus expired")  # False if they left — fine
    memory_delete(user_id)
elif context["trigger_type"] == "message":
    text = context["message_content"].strip()
    if text.startswith("!list_sus"):
        now = datetime.now(utc)
        active = [uid for uid, exp in memory_all().items() if parse(exp) > now]
        for uid, exp in memory_all().items():          # prune drift
            if parse(exp) <= now:
                memory_delete(uid)
        lines = ["🚨 **Sus Members** 🚨"] + ["<@" + u + ">" for u in active]
        send_message("\n".join(lines) if active else "*No One Is Sus*",
                     channel_id=context_channel_id)
    elif text.startswith("!sus"):
        is_mod = MOD_ROLE in context["author_role_ids"]
        targets = context["mentioned_user_ids"] if (is_mod and context["mentioned_user_ids"]) \
                  else [context["author_id"]]
        for uid in targets[:3]:                        # bound the fan-out explicitly
            if memory_get(uid) is None:                # re-sus does NOT extend the timer
                add_role(uid, SUS_ROLE, reason="sus")
                schedule_timer(86400, {"user_id": uid})
                memory_set(uid, (datetime.now(utc) + timedelta(days=1)).isoformat())
            send_message("🚨 <@" + uid + "> is sus 🚨", channel_id=context_channel_id)
```

Decisions baked in (flagged in §6): privilege = guild **moderator role** instead of
`view_guild_insights`; privileged-no-mention falls through to **self-sus** (dropping the
bug-compatible silence); role resolved by **id**, not name; `!list_sus` reads handler
memory (drift if mods hand-edit the role — acceptable for a joke, Q7 for the fix);
plain-text list instead of the embed. Memory math: ~40 bytes/entry ⇒ the 16 KB cap holds
~400 concurrent sus members — plenty. Multi-target capped at 3 by the script (and by
`ADMIN_MAX_TIMERS`/`ADMIN_MAX_ROLE_CHANGES` regardless).

**Downstream consumer** (source doc "Dependencies / Cross-reference"): moderation's
`!history` / `!whois` commands render an **"Are They Sus?"** field read from the 🚨sus🚨
role. Because this handler mutates the *real role* — not just its memory map — that field
and every other role reader stays ground-truth accurate for free; the Q7 memory-vs-role
drift trade-off is confined to `!list_sus` output and never touches moderation surfaces.

Whether to port this at all is Zech's call (Q15) — but every extension it needs is already
demanded by onboarding, so the marginal cost is one authoring conversation.

## 5. Implementation order & TDD notes

Each phase is red-green: pure functions first, endpoint/integration behavior second,
critical failure paths in the same PR as the happy path. Semgrep + gitleaks before every
commit; schema changes via `alembic revision --autogenerate` reviewed by hand.

**Phase 0 — ship what needs nothing (now):** author the `server-birthday` handler through
the existing pipeline. Zero code. Proves the daily_time path in anger.

**Phase 1 — E3 `schedule_timer` + E2 role mutation.** Smallest blast radius, unblocks sus
end-to-end and half of onboarding.
- Tests first: budget counters (`spend_timer`/`spend_role_change` raise at cap; defaults 0
  for standard tier), delay clamping (59 s and 31 days raise), non-JSON payload raises
  `ValueError`, 4 KB payload cap, timer context shape (`payload` + `scheduled_at`),
  windowed arming cap (31st arm in an hour declines).
- `AdminActor.add_role/remove_role`: happy path; **404 Unknown Member returns False**;
  403/other raises `AdminActionError` (fail fast); `ban_user` sends
  `delete_message_seconds` in the body.
- Integration: arm a timer → refire delivers payload to the same handler; refire of a
  deleted handler returns `"missing"` and emits nothing; **restart-recovery test** (arm,
  cycle the worker, fire happens) — this test also settles the job-store persistence
  verification (SQLAlchemy vs Redis backend); disabled handler refire no-ops.
- Migration: `handler_runs.role_changes` + `handler_runs.timers_scheduled`.
- Lint/judge/author prompt updates for the trigger_type-branch pattern and role-id
  literal rule, with authoring-pipeline eval cases (unconditional-ban script rejected;
  `schedule_timer` script without a timer branch rejected).

**Phase 2 — E4 message-context fields.** One-file bot change + context-shape tests
(role mentions excluded from `mentioned_user_ids`). Then **author the `sus` handler** —
the first real consumer, exercising phase 1 in production.

**Phase 3 — E1 member-event triggers + E5 stat fields.** The big one.
- Pure-function tests first: `member_update_deltas` (pending flip only → one
  member_rules_accepted; role delta only → one member_role_change with correct
  added/removed; both → two; no change → none; **missing old_member + role-holding member
  → none** (fail-safe skip); **missing old_member + non-pending role-less member → one
  member_rules_accepted** (at-least-once, the stuck-member guard); missing old_member +
  pending member → none), boost-role detection, boosting-member count, human-count
  computation.
- E6 pure functions + integration: `find_missed_rules_acceptances` (pending → excluded;
  non-pending with any non-everyone role → excluded; non-pending role-less → synthetic
  context with `is_reconciliation: True`); startup replay paces at
  `GUILD_MEMBER_EVENTS_PER_MIN` and drops nothing (backlog drains, never declines).
- `get_role_members` (E5): pagination + role filter, 200-result hard cap, metered like a
  web read, injected admin-tier only.
- Dispatch endpoint: member-event trigger matches standard handlers by guild_id; fire
  payload carries the handler's home channel implicitly; per-guild
  `GUILD_MEMBER_EVENTS_PER_MIN` declines under a simulated join burst (critical raid
  path); `active_channels` exposes guild-level pairs for standard member-event handlers.
- Migration: trigger-type check constraints on both handler tables.
- `describe_trigger` + prompts for the three new types.

**Phase 4 — author the remaining handlers** (each an authoring conversation + a
runtime smoke test against recorded contexts): `join-gate` (log-only mode),
`onboard-and-promote`, `onboarding-reconcile`, `member-milestones`, `boost-announcement`,
`premium-member-announcement`. Flip `join-gate` to banning after observation.

**Critical failure paths checklist (must exist by end of phase 3):** member left before
timer fires (role ops return False, fire outcome `ok`); raid burst (dispatch declines,
no queue explosion, milestone handler self-heals); handler deleted with timers outstanding
(orphan no-op); memory cap breach on sus map (CapExceeded, prior state intact —
`HandlerMemory` already copy-on-write); ban without reason (judge rejects at authoring);
role-change windowed cap breach mid-fire (CapExceeded recorded with cap name); cache-miss
role change (no fire, no crash); cache-miss rules acceptance on a non-pending role-less
member (FIRES — at-least-once; duplicate fire lands harmlessly on the idempotent
onboarding handler); member accepts rules during bot downtime (startup replay onboards
them — the missed-gateway-event path must have an explicit end-to-end test, not just the
lost-timer path).

## 6. Open questions & drop recommendations

### Decisions needed from Zech

1. **Q1 — Member-event triggers for the standard tier:** this plan lets per-channel
   member handlers subscribe to member_join/role-change (placement = output binding), with
   role/ban functions still admin-only. Alternative: gate all three trigger types
   admin-only initially and open them up later. Cheap to flip either way.
2. **Q2 — `settings["allowed_role_ids"]` hard rail (E2):** recommended host-enforced
   allowlist per handler on top of the literal-constant judge rule. Yes/no?
3. **Q3 — Cap numbers:** proposed `ADMIN_MAX_ROLE_CHANGES=10`, `ADMIN_MAX_TIMERS=5`,
   `GUILD_ROLE_CHANGES_PER_MIN=30`, `GUILD_MEMBER_EVENTS_PER_MIN=60`,
   `HANDLER_TIMERS_PER_HOUR=30`, timer delay `[60 s, 30 d]`. Sanity-check against expected
   join volume.
4. **Q4 — join-gate rollout:** log-only first, ban after a week of clean hits? And do the
   2021 fruit-name / "announcement" rules still earn their false-positive risk in 2026, or
   should the ported heuristic keep only pattern 1?
5. **Q5 — Rules-acceptance re-check asymmetry:** legacy re-checks only regex+age+avatar
   (not fruit/announcement). Preserve (sketched) or unify?
6. **Q6 — Milestone counter reset:** dropped in favor of delete/re-create or re-authoring.
   If a live `!reset`-style toggle is genuinely wanted, the extension is guild-scoped
   shared handler memory or multi-trigger handlers — a real design, deliberately not built
   for this alone.
7. **Q7 — `get_role_members(role_id)` gather function:** now in scope (E5) — required by
   the E6 `onboarding-reconcile` sweep. The remaining question narrows to: should
   `!list_sus` also use it for ground truth, or keep the memory-based listing? Note the
   drift only affects `!list_sus` cosmetics — the sus role itself is real, so moderation's
   `!history`/`!whois` "Are They Sus?" field (and any other role reader) is never wrong
   either way.
8. **Q8 — Moderator-role mapping for sus targeting:** guild moderator role replaces
   `view_guild_insights`. Confirm, and confirm the current `🚨sus🚨` and "Premium Members"
   role ids for authoring time.
9. **Q9 — Role matching by id** (premium announcement, sus role): plan switches off
   legacy name-matching. Confirm.
10. **Q10 — `author_role_ids` vs derived `author_is_moderator`:** plan exposes the raw
    list (no per-guild config wiring); a derived boolean is a later sugar if judge
    ergonomics demand it.
11. **Q11 — Sus bug-compatibility:** plan lets privileged users with no mentions self-sus
    (legacy: silent no-op) and keeps re-sus-doesn't-extend. Confirm both.
12. **Q12/Q13/Q14 — Presentation downgrades:** plain markdown instead of embeds for sus
    list / boost / premium announcements (no `send_embed` emitter in this group); birthday
    gif link won't unfurl because the emitter suppresses preview embeds. Acceptable, or
    does a `send_embed` extension get scheduled separately?
13. **Q15 — Port `!sus` at all?** Pure community flavor; marginal cost is one authoring
    conversation once phases 1–2 land. It's also the best end-to-end exercise of
    `schedule_timer`. One dependency to weigh either way: the source doc's cross-reference
    — moderation's `!history`/`!whois` render an "Are They Sus?" field from the 🚨sus🚨
    role. **Not** porting `!sus` leaves that field permanently empty in any ported
    moderation surface (or means removing the field there); porting it keeps that surface
    accurate for free, since the handler mutates the real role.
14. **Q16 — Stale doc:** `docs/EXISTING-FEATURES.md` documents an `automod.py` plugin that
    no longer exists in `bot/plugins/`. Confirm intentional removal (so `join-gate` isn't
    re-implementing a deliberately retired feature) and update the doc.

### Drop recommendations (rationale recap — final call is Zech's)

| Dropped capability | Superseded by |
| --- | --- |
| Per-member joined-timestamp store | `schedule_timer` payload carries the member id |
| Ready-time reconciliation — timer re-arm half only | DB-persisted timers; nothing to re-arm. (The downtime-recovery half + bot-ban checkpoint are NOT dropped — ported as the E6 `onboarding-reconcile` handler.) |
| Restart role backfill (`assign_missing_roles`) — announcement line only | E6 startup replay recovers the affected members through normal dispatch; only the "Restarted and added roles to N members" announcement is dropped (a startup log line covers audit). |
| Milestone lockdown suppression | No lockdown concept exists to gate on |
| `!reset member counter` | Delete/re-create or re-author the milestone handler |
| `!set welcome channel` + welcome/unwelcomed/reward flow | Disabled in prod, consciously superseded by an introductions channel; trivial member_join handler if ever revived |
| `!set booster channel` | Per-channel handler placement |
| `!test booster channel` / `!test premium member message` | Authoring pipeline review; optional admin-gated test branch is handler-today |
| Birthday restart re-arm | DB-backed schedule triggers |
| Missing-booster-channel failure handling | Handlers always have a bound channel + throttled error notices |
