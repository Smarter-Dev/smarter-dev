# Threads & Member Events

**Feature group:** cross-cutting handler-system extension — five new **admin-tier-only**
event trigger types (`member_join`, `member_leave`, `member_rules_accepted`,
`member_role_change`, `thread_create`), thread-aware message dispatch, and a thread
scripting-function family. This document records an **approved design**; it amends
[member-lifecycle-and-role-automation.md](member-lifecycle-and-role-automation.md) §3 E1
(see §2) and is the build spec for the `feat/thread-member-triggers` work.

**Related plans:**
- `member-lifecycle-and-role-automation.md` — E1 context shapes (normative for the three
  member triggers reproduced here), E2 role mutation, E3 timers, E5/E6 (all unchanged).
- `staff-communication-channels.md` — E5 private-thread family and E6 thread dispatch
  sketches; E6 is realized here (§4), E5's private-thread/membership ops remain follow-ups
  to be reconciled with the `thread_ops` budget introduced here (§8).

## 1. Overview

Two capability families land together because they share every rail: **member gateway
events as triggers** (the lifecycle plan's E1 family, plus `member_leave`) and **threads**
— a `thread_create` trigger covering both regular threads and forum posts, dispatch of
thread messages to parent-channel handlers, and metered thread read/write functions.
Threads and member events are both guild-shaped rather than channel-shaped: a member event
has no channel at all, and a thread's identity keys off its **parent** channel. That shared
shape is why the whole family is **admin-tier only** (§2) — dispatch, scoping, and blast
radius all reason about the guild, which is the admin tier's home turf.

Everything here follows the existing chokepoints: triggers dispatch through
`dispatch_event` (`smarter_dev/web/api_native/handlers.py:332`), scripts reach Discord
only through `HandlerExecution.external_functions()`
(`smarter_dev/web/handler_runtime.py:152`), spend is metered by `HandlerBudget`
(`smarter_dev/web/handler_budget.py`) and the Redis `WindowedLimiter`
(`smarter_dev/web/handler_caps.py`), and every fire is audited in `HandlerRun`.

## 2. Amendment to E1 — member-event triggers are admin-only

`member-lifecycle-and-role-automation.md` E1 ("Tier availability & routing") specified
that the member-event trigger family would be available to **both** tiers, with a
standard-tier dispatch branch matching `ChannelHandler.guild_id` and `active_channels`
gaining `(guild_id, trigger)` entries for standard handlers. **That routing is
superseded: all five trigger types in this document are admin-tier only** (this also
resolves the sibling doc's Q1 in favor of its "gate admin-only initially" alternative).

Consequences for E1's standard-tier consumers:

- **`member-milestones`**, **`boost-announcement`**, and **`premium-member-announcement`**
  (lifecycle §4.1/§4.2) are re-tiered: each becomes an **admin handler targeting its
  output channel explicitly** — `send_message(content, channel_id=ANNOUNCE_CHANNEL)` with
  the channel id as a script constant stated at authoring time (the admin cross-channel
  send that already exists). "Placement = output binding" no longer applies to member
  events; the explicit channel constant is the binding, visible to the judge.
- The standard-tier guild-matching branch in `dispatch_event` and the standard
  `(guild_id, trigger)` entries in `active_channels` from E1 are **not built**.
- E1's context shapes, per-trigger cache-miss policies, delta computation, caps, and E6's
  recovery design are all **unchanged** — only the tier routing moves.
- **Standard create paths must reject the new triggers.** The standard-tier trigger
  vocabulary remains exactly `message` / `reaction` / `schedule` / `timer`: the
  `ChannelHandler` create/update endpoints and the standard authoring pipeline return a
  validation error for any of the five new types, and `ck_channel_handlers_trigger_type`
  is deliberately **not** extended (the DB enforces the tier split too, §6).

## 3. E1 — Trigger family: five admin-only event triggers

New listeners in `bot/plugins/handler_events.py` on `hikari.MemberCreateEvent`,
`hikari.MemberDeleteEvent`, `hikari.MemberUpdateEvent`, and
`hikari.GuildThreadCreateEvent`. All delta/selection logic is pure-function bot-side
(lifecycle E1's `member_update_deltas` pattern), TDD-able without a gateway.

### 3.1 Trigger types and context shapes

All ids as strings, times ISO-8601 UTC, matching existing contexts. The three shapes
carried over from lifecycle E1 are reproduced for convenience; **E1 remains normative**
for them, including `member_join`'s guild-count fields and `member_role_change`'s
boost/stat fields.

```python
# member_join  (hikari.MemberCreateEvent) — shape per lifecycle E1
{
    "trigger_type": "member_join",
    "member_id": "...", "username": "...", "display_name": "...",
    "is_bot": False,
    "account_created_at": "...",
    "has_custom_avatar": True,
    "guild_member_count": 12345,
    "guild_human_member_count": 11987,
}

# member_rules_accepted  (MemberUpdateEvent, pending true -> false) — per lifecycle E1
{
    "trigger_type": "member_rules_accepted",
    "member_id": "...", "username": "...", "display_name": "...", "nickname": None,
    "account_created_at": "...", "has_custom_avatar": True,
    "joined_at": "...",
}

# member_role_change  (MemberUpdateEvent, role set changed) — per lifecycle E1
{
    "trigger_type": "member_role_change",
    "member_id": "...", "member_display_name": "...",
    "added_role_ids": ["..."], "added_role_names": ["..."],
    "removed_role_ids": ["..."], "removed_role_names": ["..."],
    "is_boost_role_added": True,
    "premium_subscription_count": 14,
    "boosting_member_count": 9,
    "role_member_counts": {"role_id": 42},
}

# member_leave  (hikari.MemberDeleteEvent) — new in this document
{
    "trigger_type": "member_leave",
    "member_id": "...", "username": "...", "display_name": "...",
    "is_bot": False,
    "account_created_at": "...",       # from the snowflake, always available
    "joined_at": "...",                # from the cached old_member; None on cache miss
    "role_ids": ["..."], "role_names": ["..."],  # from cache; may be empty
    "cache_incomplete": False,         # True when old_member was not cached
}

# thread_create  (hikari.GuildThreadCreateEvent) — new in this document
{
    "trigger_type": "thread_create",
    "thread_id": "...", "thread_name": "...",
    "parent_channel_id": "...",
    "creator_id": "...", "creator_username": "...", "creator_display_name": "...",
    "is_forum_post": False,            # parent channel is a forum channel
    "applied_tag_ids": ["..."], "applied_tag_names": ["..."],
    "starter_message_content": "",     # forum posts; may be empty string
    "created_at": "...",
}
```

`thread_create` covers **both** regular threads and forum posts — Discord emits the same
`THREAD_CREATE` gateway event for both; `is_forum_post` is how a script branches. For
forum posts the starter message's id equals the thread's id (Discord contract), so the
bot reads `starter_message_content` from its message cache best-effort; when the starter
message is not yet cached the field is `""` — documented, never a skip. Discord also
sends `THREAD_CREATE` when the bot merely *gains access* to an existing thread; the
listener dispatches **only when the payload's `newly_created` flag is set**, so
visibility changes never fire the trigger.

### 3.2 Cache-miss / failure policy (per-trigger, fail-safe direction differs)

- `member_role_change` — cache miss (no `old_member`) ⇒ **skip** (no delta ⇒ no fire),
  exactly per lifecycle E1; this skip is what makes the boost re-fire guard structural.
- `member_rules_accepted` — **at-least-once heuristic** per lifecycle E1: on cache miss,
  fire iff the new member is not pending AND holds no roles beyond `@everyone`.
  Duplicate fires are possible and documented; handlers must be idempotent (judge rail).
- `member_leave` — **informational: fire always, never skip.** On cache miss the context
  carries `joined_at: None`, empty `role_ids`/`role_names`, and
  `cache_incomplete: True`; a leave notice with partial detail beats a silent drop, and
  there is nothing fail-dangerous a leave handler can do with missing history.
- `member_join` / `thread_create` — the event payload itself is sufficient; no cache
  dependency beyond the best-effort count/starter-message fields already marked as such.

### 3.3 Dispatch & routing

- **Guild-scoped events** (the four `member_*` triggers): the bot calls `dispatch_event`
  with `channel_id=""`. The standard-tier query is skipped entirely for these trigger
  types (standard rows cannot exist — create paths reject, constraint unchanged), and
  admin handlers match by **guild + trigger + enabled only**: the `channel_ids` scope
  check is bypassed for `member_*` triggers, since a member event has no channel for a
  scope to mean anything (the authoring agent leaves `channel_ids` empty for these). The
  fire's `channel_id` is `""`, so the handler has **no home channel**:
  `send_message(content)` without an explicit `channel_id` fails, and every send must
  name its target channel constant — an authoring-prompt rule the judge can verify.
- **`thread_create`**: dispatch keys off the **parent** channel — the bot calls
  `dispatch_event` with `channel_id=parent_channel_id`, and an admin handler's
  `channel_ids` scope matches against `parent_channel_id` via the existing scope check.
  The fire's home channel is the parent, so `send_message(content)` posts to the parent
  and `send_message(content, thread_id)` posts into the new thread (§5.1 relaxation).
- **`active_channels`**: the admin query's trigger filter extends to the five new types.
  `member_*` handlers always surface as `(guild_id, trigger)` entries in `guild_triggers`
  regardless of `channel_ids` (the bot's dispatch guard is per-guild for them);
  `thread_create` handlers follow the existing channel-scoped/guild-wide split, with the
  bot matching the guard against the new thread's parent channel.

### 3.4 Caps

- `fires_per_min_for_trigger` returns **10** for all five new triggers (today's default
  branch already returns 10 for non-reaction types — pinned by an explicit test so a
  future refactor cannot silently tighten or loosen it).
- **`GUILD_MEMBER_EVENTS_PER_MIN = 60`** on key `hcap:memberevt:{guild_id}`
  (`guild_member_events_key` in `handler_caps.py`) — checked in `dispatch_event`
  **before enqueueing** any `member_*` fire, so a raid degrades to declined dispatches
  rather than a fire-queue explosion. Same constant and semantics as lifecycle E1;
  `member_leave` fires draw from the same window (join and leave burst together in a
  raid + ban wave). `thread_create` is **not** under this window — it is bounded by the
  per-handler fire cap and the thread-op caps in §5.3.

**Consumed by:** every lifecycle §4 admin handler (`join-gate`, `onboard-and-promote` —
already admin-tier); the re-tiered milestone/boost/premium handlers (§2); moderation's
rejoin alert (`member_join`/`member_leave`); thread/forum automation (auto-tag triage,
forum-post greeters, thread-janitor handlers) via `thread_create`.

## 4. E2 — Thread-aware message dispatch

A `GuildMessageCreateEvent` inside a thread now **also dispatches to handlers registered
on the thread's parent channel**. Bot-side (`handler_events.py`): resolve the message's
channel from the gateway cache; when it is a thread, dispatch with
`channel_id = parent_id` and enrich the message context:

```python
# message context, when the message is inside a thread
{..., "thread_id": "...", "thread_name": "...", "is_thread": True}

# message context, non-thread messages
{..., "is_thread": False}
```

The fire's `channel_id` stays the **parent**, so home-channel semantics (default
`send_message()` target, error notices) are unchanged; a script replies *into* the
thread explicitly with `send_message(text, context["thread_id"])`, which the §5.1
relaxation permits for standard handlers because the thread belongs to the home channel.
Thread posts meter against the thread's own `channel_message_key` window (the limiter
already keys on the actual target), exactly as cross-channel sends do today. This
realizes staff-comms E6 with one context-shape change: `is_thread` replaces that
sketch's `thread_parent_channel_id` (redundant — the dispatch `channel_id` *is* the
parent).

**Consumed by:** staff-comms rows 14/15 (`!archive`/`!lock` typed inside mod threads);
any handler that should keep working when conversation moves into a thread of its
channel.

## 5. E3 — Thread scripting functions

Registered in `HandlerExecution.external_functions()`
(`smarter_dev/web/handler_runtime.py:152`), split exactly like moderation functions
today: general functions always injected; admin functions injected only when
`self.actor is not None`.

### 5.1 General functions (both tiers, home-channel scoped)

```python
list_threads() -> list[dict]
    # Active + recently archived threads/posts of the handler's HOME channel,
    # hard cap 50 (active first, then newest-archived), each:
    # {"thread_id": str, "name": str, "created_at": str, "archived": bool,
    #  "locked": bool, "owner_id": str, "message_count": int,
    #  "applied_tag_names": [str]}
create_thread(name, message_id=None) -> str    # thread id
    # message_id set: POST /channels/{home}/messages/{message_id}/threads
    # message_id None: POST /channels/{home}/threads (public thread)
create_post(title, content, tag_names=None) -> str    # thread id
    # Forum channels only: POST /channels/{home}/threads with a message payload;
    # tag_names resolved to ids against the channel's available_tags —
    # an unknown tag name raises ValueError (loud, fail fast).
```

`list_threads` spends the new `discord_reads` budget (§5.3); the REST shape is the
guild active-threads list filtered to the home channel plus
`GET /channels/{home}/threads/archived/public`, merged under the 50-entry hard cap.
`create_thread`/`create_post` spend the **existing message budget** (`spend_message`) —
creating a thread is an emit, and reusing the counter keeps standard handlers bounded
without new knobs — plus the guild thread-op window (§5.3).

**`send_message` relaxation:** the standard-tier cross-channel gate
(`handler_runtime.py:_send_message`) is relaxed to also allow posting into **threads of
the home channel**: when a standard handler targets a channel id that is not its home
channel, the runtime verifies host-side that the target is a thread whose `parent_id`
equals the home channel before allowing it; otherwise the existing
`cross_channel_send` `CapExceeded` is raised unchanged. The parent verification is a
host rail (one cached channel fetch per fire), **not** metered against `discord_reads`.
Admin handlers are unaffected (they already send anywhere in the guild).

### 5.2 Admin-only functions (injected only when `actor` is set, like `ban_user`)

```python
list_threads(channel_id) -> list[dict]   # any channel in the handler's scope
close_thread(thread_id) -> bool          # PATCH /channels/{id}  {"archived": true}
lock_thread(thread_id) -> bool           # PATCH /channels/{id}  {"locked": true, "archived": true}
reopen_thread(thread_id) -> bool         # PATCH /channels/{id}  {"archived": false}
delete_thread(thread_id) -> bool         # DELETE /channels/{id}
```

For admin handlers, `list_threads` gains the optional `channel_id` argument (same
injected name, admin variant shadows the general one): the target must be within the
handler's `channel_ids` scope (empty scope = any channel in the guild; the runtime
verifies the channel belongs to the guild). The four mutating functions are
`AdminActor` REST methods (`smarter_dev/web/admin_actions.py`), spending `thread_ops`
(§5.3) and the guild thread-op window.

**Gone-target contract (explicit, tested — NOT a blanket except):** a 404 **Unknown
Channel/Thread/Member** on `close_thread`/`lock_thread`/`reopen_thread`/`delete_thread`
returns `False` instead of raising — a janitor sweeping stale threads must be a silent
no-op on a thread someone already deleted. `list_threads(channel_id)` on a 404 Unknown
Channel returns `[]` (informational read of a gone channel). Any **other** REST failure
raises `AdminActionError` (fail fast), exactly the lifecycle-E2 `add_role`/`remove_role`
semantics. `create_thread`/`create_post` **raise** on any failure — a create returns a
thread id and has nothing sensible to return without one (mirrors staff-comms
`create_private_thread`).

### 5.3 Budgets & windowed caps

Two new `HandlerBudget` counters (`smarter_dev/web/handler_budget.py`), following the
existing spend-method pattern:

- **`discord_reads`** / `spend_discord_read()` — spent by `list_threads` (both
  variants). `DEFAULT_MAX_DISCORD_READS = 2`, `ADMIN_MAX_DISCORD_READS = 5`.
- **`thread_ops`** / `spend_thread_op()` — spent by
  `close_thread`/`lock_thread`/`reopen_thread`/`delete_thread`.
  `DEFAULT_MAX_THREAD_OPS = 0` (standard handlers have no mutating thread ops, like
  `mod_actions`), `ADMIN_MAX_THREAD_OPS = 10`.

`usage()` and `HandlerRun` gain `discord_reads` and `thread_ops` (migration, §6);
`admin_budget()` passes the admin maxima.

Windowed cap (`smarter_dev/web/handler_caps.py`): **`GUILD_THREAD_OPS_PER_MIN = 30`**
on key `hcap:threadop:{guild_id}` (`guild_thread_ops_key`), enforced **in the runtime
wrapper before the REST call** — breaching raises `CapExceeded` mid-flight, like the
channel-message window. It covers all six mutating thread operations (`create_thread`,
`create_post`, close/lock/reopen/delete): creation is the spammy primitive and must not
escape the guild window just because it spends the message budget.

**Consumed by:** forum triage/janitor handlers (`thread_create` + close/lock/delete);
staff-comms mod-chat archive/lock flows (via the follow-up reconciliation, §8);
`create_post` FAQ/announcement flows; `list_threads` duplicate-post detection.

## 6. Schema & migration

**One alembic revision** (repo conventions per `alembic/main/versions/` — dated
filename, module docstring stating intent, plain `op.*` calls, symmetric `downgrade`):

- Drop and recreate **`ck_admin_handlers_trigger_type`** with the extended tuple:
  `('message', 'reaction', 'schedule', 'timer', 'member_join', 'member_leave',
  'member_rules_accepted', 'member_role_change', 'thread_create')`.
  **`ck_channel_handlers_trigger_type` is UNCHANGED** — the standard tier's vocabulary
  does not grow (this amends lifecycle E1's migration note, which extended both).
- Add `handler_runs.discord_reads` and `handler_runs.thread_ops`
  (`Integer, nullable=False, server_default="0"`), matching the existing counter
  columns.

`models.py`: `HANDLER_TRIGGER_TYPES` (the standard vocabulary) stays as-is; a new
`ADMIN_ONLY_TRIGGER_TYPES = ("member_join", "member_leave", "member_rules_accepted",
"member_role_change", "thread_create")` names the split — the admin constraint/creation
paths accept the union, the standard create paths reject membership in it, and the
`active_channels` admin query filters on the extended event-trigger tuple (the standard
query keeps `HANDLER_EVENT_TRIGGERS` unchanged).

## 7. Authoring & judge

- **Admin author prompt** learns all five trigger types with their context shapes
  (§3.1), the no-home-channel rule for `member_*` handlers (every `send_message` names a
  channel constant), and the admin thread functions with the gone-target contract.
- **Standard author prompt** learns only the general functions (§5.1): `list_threads()`,
  `create_thread`, `create_post`, and the thread-of-home-channel `send_message`
  relaxation. It does **not** learn the new triggers — they are not in its vocabulary.
- **`describe_trigger`** (`smarter_dev/bot/agents/handler_authoring.py:349`) gains
  honest frequency lines:
  - `member_join` — "fires on EVERY member join — bursts hard during raids."
  - `member_leave` — "fires on EVERY member leave — bursts during raids and ban waves."
  - `member_rules_accepted` — "fires once per member on rules acceptance — may
    duplicate after cache misses; the script must be idempotent."
  - `member_role_change` — "fires only when a member's roles actually change — low
    frequency."
  - `thread_create` — "fires on EVERY new thread/post in the channel."
- **Judge rails:**
  - `delete_thread` targets must come from **trigger context or a `list_threads`
    result** — never a constant or arithmetic on ids; a hardcoded delete target is an
    unreviewable destructive action.
  - A `member_join` handler that messages unconditionally is **spam at raid frequency**
    and must be rejected unless the target channel is explicitly a join-log.
  - `member_rules_accepted` handlers must be idempotent (at-least-once delivery, §3.2).
  - Lifecycle E1's existing rails (role-id literals, etc.) apply unchanged to the
    handlers that ride these triggers.

## 8. Out of scope / follow-ups

- **`member_ban` / `member_unban` triggers** — moderation-plan territory; not in this
  family.
- **`pin_post` / `unpin_post`** and **`set_post_tags`** — further forum ops; add when a
  consumer demands them (they slot into `thread_ops` cleanly).
- **Lifecycle E2 (role mutation) and E3 (durable timers)** — already fully specced in
  `member-lifecycle-and-role-automation.md`; nothing here changes them.
- **Staff-comms E5 reconciliation** — `create_private_thread`, thread membership ops
  (`add/remove_thread_member`, `get_thread_members`), and that sketch's
  `thread_ops`/`GUILD_THREAD_CREATES_PER_MIN` numbers must be reconciled onto this
  document's `thread_ops` counter and `hcap:threadop` window before mod-chat is built
  (per the index's build-once rule); this document's budgets and dispatch context are
  the baseline.

## 9. Implementation order & TDD notes

Red-green per the repo norm; schema via one hand-reviewed alembic revision; semgrep +
gitleaks before every commit.

1. **Budgets & caps** — `spend_discord_read`/`spend_thread_op` raise at cap (defaults 0
   thread ops for standard tier); `usage()` includes both; `fires_per_min_for_trigger`
   pinned at 10 for the five new types; window keys/constants.
2. **Migration** — extended admin constraint (standard constraint provably unchanged),
   two `HandlerRun` columns.
3. **Runtime functions** — general + admin injection split (admin `list_threads`
   shadows); gone-target 404 ⇒ `False` / `[]`, other failures raise `AdminActionError`;
   unknown forum tag raises `ValueError`; `send_message` thread relaxation (thread of
   home channel allowed, unrelated thread still `cross_channel_send`); guild thread-op
   window breach mid-fire records the cap name.
4. **Bot dispatch** — pure-function tests for member deltas (per lifecycle E1's list)
   plus: `member_leave` with cache miss fires with `cache_incomplete: True`;
   `thread_create` only on `newly_created`; forum post carries tags + starter content
   (empty string when uncached); thread message dispatches on the parent with
   `is_thread: True`; non-thread message context carries `is_thread: False`.
5. **Dispatch endpoint** — `member_*` with `channel_id=""` skips the standard query,
   bypasses admin scope, and declines past `GUILD_MEMBER_EVENTS_PER_MIN` under a
   simulated raid; `thread_create` scope-matches on `parent_channel_id`; standard
   create/update paths reject the five new triggers; `active_channels` surfaces the new
   guild/channel entries.
6. **Authoring** — prompt + `describe_trigger` updates with eval cases: unconditional
   member_join messaging rejected; hardcoded `delete_thread` target rejected.
