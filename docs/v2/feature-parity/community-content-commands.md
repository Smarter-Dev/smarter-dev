# Feature Parity Plan: Community Content Commands

**Legacy sources:**
- `beginner.py-bot/docs/prod-functionality/03-rules.md` (Rules Cog: `!rule`, `!update-rules`, `!formatting`)
- `beginner.py-bot/docs/prod-functionality/04-resources-command.md` (Resources Cog: `!resources`/`!r`, `!project`)

**Implementation target:** the agentic handler system (`smarter_dev/web/handler_runtime.py` and friends), per the "lean hard on handlers" direction.

## 1. Overview

This group covers the legacy bot's two read-mostly curated-content command suites:

- **Rules** — a hardcoded set of 8 community rules with `!rule <label>` fuzzy lookup, a
  suggestions index, the staff-only `!update-rules <reason>` republish of the canonical
  "# Rules & Conduct" post, and the `!formatting` code-block how-to.
- **Resources** — config-driven curated learning links with `!resources [topic]`
  (aliases `!r`, `!resource`), an alias table (`py`→`python`, `js`→`javascript`, …),
  and the static `!project` link command. **Dropped entirely** (Zech, 2026-07-18): the
  2020-era catalog is stale, the curation burden outweighs the value, and the chat agent
  plus challenges/quests cover "where do I learn X" better. See the disposition table.

They were grouped because they share the same shape: no event listeners, no schedules, no
DB, no HTTP — pure message-in/message-out over a small curated dataset. That is almost
exactly what a message-trigger handler is, so everything that survives is **handler-today**.
The only genuine gaps are three small, shared extensions: an `edit_message` emit for the
canonical rules post, an `author_role_ids` context field for the staff gate, and default
mention suppression on the emitter (a blanket safety hardening also wanted by the
staff-comms group).

One important correction to earlier analysis: **guild-wide message handlers already
exist**. `AdminHandler` with `channel_ids=[]` fires on every channel in the guild
(`smarter_dev/web/models.py`, `handler_events.py` dispatches via `guild_triggers`), and
admin handlers can already `send_message(content, channel_id=...)` cross-channel and
`delete_message`. No scoping extension is needed — the anywhere-invocable commands are
authored as guild-wide admin handlers.

### Invocation style decision

We keep the bang-command style (`!rule`, `!formatting`) rather than re-imagining
these as slash commands. Handlers match raw message content, so a prefix guard at the top
of the script is the natural handler idiom, costs nothing, and preserves user muscle
memory from the legacy server. What we do NOT port verbatim: channel-by-name lookups,
embeds, and the config/redeploy edit workflow (all replaced below). Prefix guards must be
word-boundary exact (`content == "!rule"` or `content.startswith("!rule ")`) so `!ruler`
never fires.

## 2. Feature disposition table

| # | Capability | Source | Disposition | Justification |
|---|------------|--------|-------------|---------------|
| 1 | Canonical rules content store (8 rules + lookup labels) | py-rules | handler-today | Dataset lives as a literal in the handler script (~3–5 KB, under the 8 KB script cap); edits go through the author→lint→judge re-author flow instead of a deploy. |
| 2 | `!rule <label>` fuzzy lookup | py-rules | handler-today | Pure Monty string matching (exact label, then unique-title-substring fallback) + one `send_message`; guild-wide admin handler. |
| 3 | `!rule` not-found / bare-`!rule` index | py-rules | handler-today | Same handler, same dataset; renders sorted primary labels as markdown. |
| 4 | `!update-rules <reason>` republish of the canonical rules post | py-rules | handler-extension | Needs the new `edit_message` emit (admin tier); stored message ids in handler memory replace the fragile oldest-message-in-named-channel edit. |
| 5 | `manage_channels` permission gate on `!update-rules` | py-rules | handler-extension | Needs `author_role_ids` added to the message-trigger context; ported as a staff-role-id check rather than a permission-bit check. |
| 6 | `!update-rules` confirmation with 60 s auto-delete | py-rules | drop | Confirmation ports as a plain `send_message`; the auto-delete needs a delayed execution path scripts don't have. Cosmetic — not worth a new rail. |
| 7 | `!formatting [language]` / `!format` / `!code` how-to | py-rules | handler-today | Static reply template with alphanumeric-sanitized language arg; nested backticks rendered via `\`` escapes. |
| 8 | Embed presentation with server-icon thumbnail | py-rules | drop | Markdown headings/bold reproduce the information; the emitter is deliberately content-only and this feature alone doesn't justify a `send_embed` surface. |
| 9 | Mention suppression on rules republish | py-rules | handler-extension | Port as default `allowed_mentions: {"parse": []}` on ALL emitter sends/edits — blanket hardening, not a per-feature flag. |
| 10 | Dead code: `build_rule_message_embed` (hardcoded admin id) | py-rules | drop | Never called in legacy; inert. Do not port. |
| 11 | Channel-by-name dependency on `👮rules` | py-rules | handler-today (replaced) | Rules channel id is a literal in the script (resolved at authoring time via the existing `list_channels` tool); message ids persist in handler memory. Name lookup is not ported. |
| 12–20 | Entire `!resources` command suite (topic index, alias-resolved lookup, not-found reply, content workflow, `!project`, invocation rules, quoted tips, `lang_aliases` set) | py-resources-command | **drop** | Dropped entirely (Zech, 2026-07-18). The catalog is 2020-era and stale, re-curation is a content burden with no owner, and smarter-dev's chat agent + challenges/quests plugins cover the "where do I learn X" need better than a static link list. Nothing here required any handler-system extension, so nothing else is affected; trivially authorable as a handler later if ever wanted. |

## 3. Handler-system extensions

Three extensions, all small, none needing an alembic migration (context is JSON in
`HandlerRun.trigger_context`; no schema changes). Ordered by blast radius, smallest first.

### 3.1 Default mention suppression on the emitter (shared hardening)

**What:** `DiscordEmitter.create_message` (`smarter_dev/web/handler_emitter.py`) sends no
`allowed_mentions`, so any handler that emits user-influenced text (a rule description a
staff member pasted, a resource entry, quoted message content) can ping `@everyone` or
mass-ping roles. Legacy `!update-rules` explicitly disabled all mentions; we make that the
platform default.

**Design:** add to the `create_message` payload (and the new `edit_message` payload):

```python
_ALLOWED_MENTIONS_NONE = {"parse": []}
payload = {
    "content": content[:_MESSAGE_MAX],
    "flags": _SUPPRESS_EMBEDS,
    "allowed_mentions": _ALLOWED_MENTIONS_NONE,
}
```

`{"parse": []}` suppresses everyone/here, role, and user pings while leaving the mention
text readable. No opt-out parameter for now — if a future handler legitimately needs to
ping (e.g. a mod-alert), that is a deliberate later change with its own judge guidance.

**Budget/caps:** none — behavior of existing metered calls.
**Lint/judge:** none required; optionally note in both judge prompts that emitted content
cannot ping, which simplifies `actions_appropriate` review of quoting scripts.
**Consumers:** capabilities 4 and 9 directly; every other emitting handler benefits.

### 3.2 `author_role_ids` message-context field

**What:** the message-trigger context (`smarter_dev/bot/plugins/handler_events.py`
`on_message`) carries `author_id`/`author_name`/account-age/activity facts but nothing
about roles, so a script cannot gate a command on "is staff". This ports the legacy
`manage_channels` permission gate as a role-id check — computing effective Discord
permissions bot-side is heavier and not needed for one gate.

**Design:** in `on_message`, alongside `author_joined_at`:

```python
author_role_ids: list[str] = []
if event.member is not None:
    author_role_ids = [str(role_id) for role_id in event.member.role_ids]
```

and add `"author_role_ids": author_role_ids` to the dispatched context. `event.member`
is already in hand — no extra REST call, no budget/cap impact. Empty list when the member
object is unavailable, which fails closed for any gate written as
`if not STAFF_ROLE_IDS & set(context.get("author_role_ids", []))`.

**Lint/judge:** document the field in `handler_author.md` and `admin_handler_author.md`
(context key list) and add a line to the judge prompts: a role-gated action must fail
closed (missing/empty role list ⇒ no privileged action). This is what enables
permissioned commands in scripts, so the judge should look for gates that compare against
literal role ids, not role names from message text.
**Authoring ergonomics (optional, not blocking):** a `list_roles` authoring tool mirroring
`list_channels`, so the author agent can resolve "the Staff role" to an id instead of the
admin pasting one. Ship the context field first; add the tool if authoring friction shows.
**Consumers:** capability 5 here; every future staff-gated command (staff-comms group
shares this).

### 3.3 `edit_message(message_id, content, channel_id=None)` — admin-only metered emit

**What:** the emitter can create messages but not edit them; maintaining a canonical
bot-owned post (the rules message) requires editing in place. This is the only genuinely
new capability in the group.

**Design — emitter** (`handler_emitter.py`), symmetric with `create_message`:

```python
async def edit_message(self, channel_id: str, message_id: str, content: str) -> str:
    """Edit a bot-authored message in place; returns the message id.

    Discord REST only permits editing the bot's own messages, so
    bot-authored-only is enforced by the API (403 otherwise) — no
    ownership bookkeeping needed on our side.
    """
    payload = {
        "content": content[:_MESSAGE_MAX],
        "flags": _SUPPRESS_EMBEDS,
        "allowed_mentions": _ALLOWED_MENTIONS_NONE,
    }
    response = await self._request(
        "PATCH", f"/channels/{channel_id}/messages/{message_id}", json=payload
    )
    return str(response.json().get("id", ""))
```

**Design — runtime** (`handler_runtime.py`): registered in
`HandlerExecution.external_functions()` **only when `self.actor is not None`** (admin
tier), alongside `delete_message` — a standard channel handler editing arbitrary bot
messages is a defacement primitive we don't need for any planned feature.

```python
async def _edit_message(
    self, message_id: str, content: str, channel_id: str | None = None
) -> str:
    self.budget.spend_message()
    target = str(channel_id) if channel_id else self.channel_id
    return await self.emitter.edit_message(target, str(message_id), str(content))
```

**Budget/caps:** draws from the per-fire message pool (`spend_message`, admin tier = 5)
but does NOT hit the per-channel `channel_messages_per_min` window — same precedent as
`add_reaction`: an edit changes an existing message, it does not add channel volume.
Editing a message the bot doesn't own surfaces as a `DiscordEmitError` (REST 403), which
errors the fire loudly through the existing notice path — fail fast, no silent fallback.
**Lint/judge:** `admin_handler_author.md` gains the function under MODERATION-adjacent
functions with the note "only messages the bot itself posted can be edited; store the ids
of posts you intend to maintain in memory". `admin_handler_judge.md`: edits count as
emits for `within_limits`; a script that edits ids taken from trigger context (rather
than from its own memory/`send_message` returns) should fail `actions_appropriate`.
**Migration:** none (no schema change; `HandlerRun.messages_sent` already counts it).
**Consumers:** capability 4. Also useful later for any "maintained post" feature
(leaderboard boards, event posts).

### Explicitly not built

- **Guild-wide message-trigger scoping** — already exists (`AdminHandler.channel_ids=[]`).
- **`send_embed(title, fields, thumbnail)`** — recommend against: markdown headings,
  bold section headers, and masked links carry all the information; the emitter's
  deliberate smallness is worth more than thumbnail parity. Revisit only if multiple
  future groups independently want it. (Capability 8 renders fine as markdown.)
- **`delete_after_seconds` on `send_message` / script-schedulable follow-ups** — only
  consumer is the cosmetic 60 s confirmation auto-delete (capability 6). Dropped.

## 4. Per-feature plans

Both command handlers are **guild-wide admin handlers** (`channel_ids=[]`), created
through the existing admin authoring flow (dual-judge pipeline). They coexist under the
`MAX_ADMIN_HANDLERS_PER_GUILD = 20` cap (this group uses 2). Each script's first lines
are a cheap prefix guard, since a guild-wide message handler runs on every human message.

### 4.1 Rules — one handler: `server-rules` (message trigger, guild-wide)

One handler owns the rules dataset and serves both `!rule` (everyone) and
`!update-rules` (staff-gated). A single handler is deliberate: the dataset must not be
duplicated across two 8 KB scripts, and handler memory is private per handler, so the
lookup and the republish must share a script to share the data. The commands are two
branches of one behavior ("serve and maintain the rules"), which keeps the author's
"never fold unrelated behavior" rule satisfied.

**Data home:** the 8-rule dataset is a script literal (fits well under 8 KB). Content
edits are conversational re-authors ("change the DM rule to say …"), which re-runs
lint+judge — strictly better than the legacy edit-code-and-deploy. The rules channel id
and staff role ids are also script literals, resolved at authoring time via
`list_channels` (and pasted/`list_roles` for the role). Handler memory holds only
`rules_message_ids` — the list of bot-authored message ids that make up the canonical
post (a list, not a single id: the emitter truncates at 2 000 chars, so the full post is
chunked at rule boundaries; 8 rules typically means 1–2 chunks).

**Script sketch** (what the author agent should land on; Monty-valid — no classes, no
imports beyond the allowed four, `async def run()` + trailing `await run()`):

```python
STAFF_ROLE_IDS = {"<staff-role-id>"}
RULES_CHANNEL_ID = "<rules-channel-id>"
RULES = [
    {"title": "Direct Messages", "labels": ["dm", "dming", "pm"],
     "description": "..."},
    # ... 7 more, each: title, labels (first label = primary), description
]

def find_rule(label):
    exact = [r for r in RULES if label in r["labels"]]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [r for r in RULES if label in r["title"].casefold()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    return None

def primary_labels():
    return sorted(r["labels"][0] for r in RULES)

def rule_title(rule):
    return rule["title"]

def full_rules_chunks():
    sections = ["# Rules & Conduct"]
    for rule in sorted(RULES, key=rule_title):
        sections.append("## " + rule["title"] + "\n" + rule["description"])
    chunks, current = [], ""
    for section in sections:
        candidate = current + "\n\n" + section if current else section
        if len(candidate) > 1900:
            chunks.append(current)
            current = section
        else:
            current = candidate
    chunks.append(current)
    return chunks

async def handle_lookup(argument):
    if not argument:
        lines = ["`!rule " + name + "`" for name in primary_labels()]
        await send_message("## Server Rules\n" + "\n".join(lines))
        return
    rule = find_rule(argument)
    if rule is None:
        names = ", ".join("`" + n + "`" for n in primary_labels())
        await send_message(
            "Didn't find a rule for '" + argument + "'. Try one of: " + names
        )
        return
    await send_message("## " + rule["title"] + "\n" + rule["description"])

async def handle_republish(reason):
    if not STAFF_ROLE_IDS & set(context.get("author_role_ids", [])):
        return  # fail closed, silently — matches legacy permission failure
    if not reason:
        await send_message("Usage: `!update-rules <reason>`")
        return
    chunks = full_rules_chunks()
    stored_ids = await memory_get("rules_message_ids", [])
    if len(stored_ids) == len(chunks):
        for message_id, chunk in zip(stored_ids, chunks):
            await edit_message(message_id, chunk, RULES_CHANNEL_ID)
    else:
        new_ids = []
        for chunk in chunks:
            new_ids.append(await send_message(chunk, RULES_CHANNEL_ID))
        await memory_set("rules_message_ids", new_ids)
    await send_message("Rules message has been updated: " + reason)

async def run():
    content = context["message_content"].strip()
    lowered = content.lower()
    if lowered == "!rule" or lowered.startswith("!rule "):
        await handle_lookup(content[len("!rule"):].strip().casefold())
    elif lowered == "!update-rules" or lowered.startswith("!update-rules "):
        await handle_republish(content[len("!update-rules"):].strip())

await run()
```

Notes on deliberate legacy deviations:
- **First-run bootstrap replaces the oldest-message edit.** When `rules_message_ids` is
  empty (or the chunk count changed), the handler POSTS fresh messages to the rules
  channel and remembers their ids; thereafter it edits in place. The `👮rules`
  channel-by-name lookup and the "edit the oldest message" heuristic are gone. If the
  guild already has a legacy rules post, staff delete it once and run `!update-rules
  initial` — a one-time manual step, documented in the authoring conversation.
- The legacy `force` recursion in `get_rules` is an implementation quirk; the observable
  not-found/index behavior above is what's ported.
- Budget check: worst republish fire = 2 chunk posts + 1 confirmation = 3 emits, inside
  the admin cap of 5. A rules post growing past 4 chunks would breach; the judge's
  `within_limits` check and a test pin this.

### 4.2 Code formatting — one handler: `code-formatting-help` (message trigger, guild-wide)

Static-reply handler; the only logic is alias matching and language sanitization.

```python
async def run():
    content = context["message_content"].strip()
    lowered = content.lower()
    matched = None
    for prefix in ("!formatting", "!format", "!code"):
        if lowered == prefix or lowered.startswith(prefix + " "):
            matched = prefix
            break
    if matched is None:
        return
    raw = content[len(matched):].strip()
    language = "".join(ch for ch in raw if ch.isalnum()) or "py"
    await send_message(
        "## Code Formatting\n"
        "Wrap your code in a fenced block so Discord keeps the formatting:\n"
        "\\`\\`\\`" + language + "\n"
        "your code here\n"
        "\\`\\`\\`"
    )

await run()
```

The nested-backtick problem (showing a fence inside a message) is solved with `\``
escapes, which Discord renders as literal backticks — no zero-width-space tricks. This
could equally be a standard channel handler in the help channels; guild-wide admin is
chosen for legacy parity (see open question 3).

### 4.3 Resources — not ported

The entire `!resources` suite (capabilities 12–20) is dropped — see the disposition
table. No handler is authored, no extension was needed, and nothing else in this plan
depended on it. If a curated-links command is ever wanted again, it is a plain
handler-today authoring exercise (catalog literal + prefix guard), with the one caveat
that the catalog must be curated to fit the 8 KB script cap.

## 5. Implementation order & TDD notes

TDD throughout: write the failing test first, all happy paths plus the critical failure
paths named below. The handler scripts themselves are testable offline via
`run_handler_script` with a fake emitter/limiter (existing pattern in the handler tests) —
author the "golden" scripts in a fixture, assert on emitted payloads, then feed the same
scripts through the real authoring pipeline when installing.

**Phase 0 — emitter mention suppression** (no dependencies, ships alone)
- Test: `create_message` payload includes `allowed_mentions == {"parse": []}` alongside
  the existing `SUPPRESS_EMBEDS` flag; content containing `@everyone` goes out unchanged
  as text.
- One-line change + tests; also update any emitter payload snapshots.

**Phase 1 — `author_role_ids` context field**
- Tests (bot side, `handler_events`): message context includes stringified role ids when
  `event.member` is present; includes `[]` when `event.member is None` (fail-closed
  path); reaction/schedule/timer contexts unchanged.
- Update `handler_author.md` / `admin_handler_author.md` context documentation and judge
  guidance (fail-closed gating) in the same change.

**Phase 2 — `edit_message` extension**
- Emitter tests: PATCH to `/channels/{cid}/messages/{mid}`; content truncated at 2 000;
  payload carries suppression flags + `allowed_mentions`; REST 403/404 raises
  `DiscordEmitError` (fail fast — critical failure path for "not our message" /
  "message deleted").
- Runtime tests: function present only when `actor` is set (a standard handler script
  calling `edit_message` fails as an unknown function); each call spends the message
  budget (6th emit in an admin fire raises `CapExceeded("messages", …)` and is recorded
  via the breach path); does not hit the channel message window; targets the trigger
  channel when `channel_id` is omitted.
- Prompt updates (admin author + admin judge) in the same change, with a judge-eval
  fixture: a script editing a context-supplied message id should be rejected on
  `actions_appropriate`.

**Phase 3 — author the handlers** (content work + golden-script tests; depends on 0–2
only for `server-rules`; `code-formatting-help` could ship after Phase 0 alone)
1. `code-formatting-help` — smallest. Tests: each alias fires; `!coder` does not;
   default language `py`; `!format c++` sanitizes to `c`; reply renders literal fences.
2. `server-rules` — needs Phases 1+2. Tests: exact-label hit; unique-title-substring
   fuzzy hit; ambiguous substring → not-found; bare `!rule` index sorted; non-staff
   `!update-rules` does nothing (critical failure path: empty `author_role_ids`);
   staff + missing reason → usage message; first-run bootstrap posts chunks and persists
   `rules_message_ids`; steady-state republish edits in place and re-sends nothing;
   chunk-count change falls back to re-posting; worst-case emit count ≤ 5; every chunk
   ≤ 1 900 chars.
- Install order in the live guild: formatting → rules, each via the admin
  authoring flow; for rules, run the one-time `!update-rules initial` bootstrap and
  verify the memory write in the admin handler view.

Run `semgrep` and `gitleaks` before each commit, per project convention. No alembic
migrations are needed anywhere in this group.

## 6. Open questions & drop recommendations

### Needs Zech's decision

1. **Staff gate shape** — role-id allowlist (this plan) vs true `manage_channels`
   permission resolution. Role ids are simple and fail closed but drift if the guild
   restructures roles; permission bits would need bot-side effective-permission
   computation added to dispatch. Plan assumes role ids are acceptable.
2. **Command scope** — guild-wide (legacy parity, this plan) vs restricted to help
   channels. Guild-wide means every human message fires two cheap script executions
   and each handler shares the 120 fires/min admin ceiling — above that message rate,
   command invocations silently decline until the window rolls. If that bothers, author
   them as standard channel handlers in designated channels instead (also drops the
   `server-rules` staff republish's cross-channel need only if the handler lives in the
   rules channel itself).
3. **Rules text refresh** — the dataset is 2020-era beginner.py content with beginner.py
   branding. Migration mechanics don't care, but the seeded literals need final text
   before `server-rules` is authored.

### Decided drops

| Capability | Rationale |
|---|---|
| **Entire `!resources` command suite (caps 12–20)** | **Dropped by Zech, 2026-07-18.** Stale 2020-era catalog, curation burden with no owner; the chat agent and challenges/quests cover the need. Includes `!project` and the alias table. |

### Drop recommendations (final call is Zech's)

| Capability | Rationale |
|---|---|
| 60 s auto-delete on the `!update-rules` confirmation (cap. 6) | Requires a delayed-execution path (script-schedulable timer or `delete_after_seconds`) whose only consumer is channel-hygiene cosmetics. The confirmation itself ports as a plain message. |
| Embed presentation + server-icon thumbnail (cap. 8) and the `send_embed` extension | Markdown carries the content; the emitter's smallness is a design asset. Revisit only if several future groups independently want embeds. |
| Dead code `build_rule_message_embed` (cap. 10) | Never called in legacy; references a hardcoded admin id. Inert. |
| Channel-by-name `👮rules` lookup (cap. 11 mechanism) | Replaced by an authoring-time channel id + memory-persisted message ids; the fragile name coupling is not ported. |
