# Stage 02 — The Quick chat surface: route, rail entry, and thread dividers

**Stable id:** `quick-chat-surface`

## Outcome

Give Quick chat a front door. After this stage a person can open `/chat/quick`,
land in their own single perpetual Quick chat, and use it exactly like any other
chat — same composer, same model and reasoning selects, same documents dock. It
appears pinned at the top of the history rail rather than sinking into the
by-recency groups.

Thread boundaries render as a labelled divider in the message stream, in both
places the stream is built: the server render on page load and the client
reconcile after a reconnect. Nothing draws a boundary yet — the agent's tool is
stage 03 and the idle evaluator is stage 04 — so in practice a Quick chat has one
thread and no divider is shown. The rendering has to land first so that when
stage 03 starts drawing boundaries there is something to draw them on, and it is
tested here against thread rows inserted directly.

## Prior state (stage 01, already committed)

- `WebChatConversation.chat_mode` — `'standard'` or `'quick'`, defaulting to
  `'standard'`, with a partial unique index `uq_web_chat_one_live_quick` allowing
  one unarchived Quick chat per owner.
- `web_chat_threads` table and the `WebChatThread` model: `conversation_id`,
  `sequence`, `start_sequence`, `title`, `origin`, `reason`.
- `smarter_dev/web/chat/threads.py` with `current_thread`, `history_floor`,
  `open_thread`, `derive_thread_title`.
- The worker's `_structured_history` is floored to the current thread, compaction
  lookup included.

## Assumptions

On the review thread, may come back corrected:

- **One Quick chat per person, reached at a fixed URL** rather than a per-chat
  toggle. Hence get-or-create rather than a create button.
- **Threads are not rail entries.** The rail shows one "Quick chat" row; threads
  are dividers inside the stream.
- **Uploads and written documents span the whole Quick chat**, not the thread. A
  boundary resets what the model reads; the documents dock still lists everything.

## Context you need before starting

- `smarter_dev/web/chat/controller.py` — `chat_index` at line 302 (`GET /chat`),
  `chat_conversation` at 331 (`GET /chat/{conversation_id:uuid}`), and the three
  context builders `_rail_context` (72), `_catalog_context` (112), `_chat_context`
  (153). Both page routes render the one template `chat/index.html`, which serves
  two products through a `mode` variable of `"chat"` or `"resources"`. That
  `mode` is **not** `chat_mode` from stage 01 — do not conflate them; if anything,
  rename nothing and be careful.
- `smarter_dev/web/chat/api.py` — `ChatApiController` at 294, mounted at
  `/v2/api/chat`. `create_conversation` at 346, `get_conversation` at 396 (the
  reconcile snapshot), `owned_conversation` at 156, `require_entitled` at 122.
- `themes/smarterdev/templates/chat/index.html` — 318 lines. The `history_row`
  macro at line 16, the rail at 64, and the message loop at **162–185**.
- `themes/smarterdev/static/js/chat.js` — 2,461 lines, one IIFE, ES5-style `var`,
  no framework and no build step. `syncThread(snapshot)` at **line 1640** is the
  reconcile path; `messageArticle` (1435), `adoptArticle` (1423), `renderMessage`
  (1599) are its helpers. Note lines 1673–1675: **it removes every element
  matching `.chat-message` that was not in the keep set.**
- `themes/smarterdev/static/css/pages/chat.css`.
- `app.yaml` lines 92–95 and `app.development.yaml` lines 71–74 — routes are
  registered declaratively in **both** files. A new route handler that is only
  added to one of them works in production and 404s in local development, or the
  reverse.
- CSP is `script-src 'self' https://cdn.jsdelivr.net` (`app.yaml:180`) — **no
  inline scripts**. State reaches the JS through `data-*` attributes or a
  `<script type="application/json">` payload block; see `index.html:177`.
- `smarter_dev/web/chat/entitlements.py` — `has_chat`, `has_ultra_chat`. Chat is
  gated by role, not by a Litestar `Permission`.

## What to build

### 1. `GET /chat/quick`

A new handler in `smarter_dev/web/chat/controller.py`, registered in both
`app.yaml` and `app.development.yaml` next to the existing chat routes.

It does not collide with `/chat/{conversation_id:uuid}` because that parameter is
typed as a UUID, but register it before that route anyway and add a test that
proves `/chat/quick` reaches the new handler.

Behaviour:

1. Same gate as `chat_index`: `require_user_id`, load the `User`, check
   `user.is_active` and `has_chat(permissions)`, else a 403 with the same wording.
2. Get-or-create the caller's Quick chat: the single `WebChatConversation` with
   `owner_user_id == user_id`, `chat_mode == 'quick'` and `archived_at IS NULL`.
   When creating, mirror `create_conversation` in `api.py:346-389` for how model,
   reasoning and intelligence mode are resolved from `ChatSettings` — including
   the Ultra-intelligence downgrade for people without `has_ultra_chat` — and set
   `title="Quick chat"`, `title_is_custom=True` (nothing should rename it),
   `chat_mode="quick"`, `status="idle"`, `next_sequence=1`.

   Put the resolution logic somewhere both this handler and
   `create_conversation` can call rather than copying twenty lines. A small
   helper in `api.py` or a new function in `smarter_dev/web/chat/settings.py`
   returning the resolved `(intelligence_mode, model_key, reasoning_level)` is
   fine; whichever you pick, `create_conversation` must go through it too so the
   two cannot drift.

3. Render `chat/index.html` with the same context `chat_conversation` builds for
   a web chat, plus the new `threads` list (below) and `quick_chat=True`.

**Handle the create race.** Two tabs opening `/chat/quick` at once will both try
to insert. The partial unique index from stage 01 makes the loser's `INSERT`
raise `IntegrityError`; catch that specific exception, roll back, re-select, and
carry on with the row that won. Catching a broad `Exception` here would violate
the fail-fast rule in `CLAUDE.md` — catch `sqlalchemy.exc.IntegrityError` only.

### 2. Keep the Quick chat out of the recency groups

`_rail_context` (controller.py:72) selects the owner's unarchived conversations
and buckets them into Today / This week / Older. Exclude
`chat_mode == 'quick'` from that query and return it separately as
`quick_conversation`, so it can be pinned rather than appearing twice.

Every page that renders the rail gets this — `chat_index`, `chat_conversation`
and the new handler all merge `_rail_context`, so one change covers them.

### 3. Threads in both snapshots

The stream is built twice and the two must agree, or a reconnect will silently
delete the dividers the server rendered.

- `_chat_context` in `controller.py:153` — add a `threads` key: every
  `WebChatThread` for the conversation ordered by `sequence`, as
  `{"id", "sequence", "start_sequence", "title", "origin"}`.
- `get_conversation` in `api.py:396` — add the identical `threads` list to the
  JSON snapshot, built from the same helper. Write **one** function (in
  `smarter_dev/web/chat/threads.py`) that both call. Do not build the list twice.

For a standard conversation the list is empty, so nothing about the existing
render changes.

### 4. Render the divider

**Template** — `themes/smarterdev/templates/chat/index.html`, in the message loop
at 162–185. Before each `<article class="chat-message">`, emit a divider when a
thread starts at that message's sequence **and** that thread's `sequence > 1`
(the first thread opens the conversation and has nothing above it to divide).

Build a `{sequence: thread}` lookup in the controller rather than scanning the
thread list inside the Jinja loop. Markup along the lines of:

```html
<div class="chat-thread-break" data-thread-break data-thread-id="{{ thread.id }}"
     data-start-sequence="{{ thread.start_sequence }}" role="separator">
  <span class="chat-thread-break-label">{{ thread.title or 'New thread' }}</span>
</div>
```

**JS** — `themes/smarterdev/static/js/chat.js`, `syncThread` at 1640. Two changes:

- While walking `snapshot.messages`, insert or adopt a `[data-thread-break]`
  element before the article whose message sequence matches a thread's
  `start_sequence`, skipping thread `sequence == 1`. The snapshot's message
  entries need to carry `sequence` for this — check whether `message_dict`
  (`api.py:252`) already includes it and add it if not.
- The sweep at 1673–1675 removes everything matching `.chat-message` that is not
  in the keep set. A `.chat-thread-break` is not a `.chat-message`, so today it
  would survive forever and accumulate duplicates on every reconcile. Give
  dividers their own keep set and their own sweep, keyed by thread id.

Match the file's existing style: `var`, no arrow functions, no template literals,
no optional chaining. It is deliberately ES5-flavoured.

**CSS** — `themes/smarterdev/static/css/pages/chat.css`. A horizontal rule with
the thread label centred on it, in the existing visual language (look at how
`.chat-history-group` and the `p-label` / `p-meta` classes are styled — small
caps, dim, letter-spaced). Keep it quiet: it is a boundary marker, not a heading.

### 5. Rail entry

In the rail (`index.html:64-89`), pin a "Quick chat" row above the
Today / This week / Older groups when `quick_conversation` is present, linking to
`/chat/quick`, marked `aria-current="page"` when it is the conversation on
screen. It should not get the rename / archive / delete row menu the ordinary
rows have — a Quick chat is not filed away like a normal conversation, and the
`history_row` macro's menu actions would leave the person with no Quick chat and
no obvious way back. If a shared macro makes that awkward, render the pinned row
as its own small block rather than bending the macro.

Also add a link into the empty state on `/chat` so the surface is discoverable.

## Tests (write these first)

Add to `tests/web/` — a new `quick_chat_surface_test.py`, plus additions to the
thread tests from stage 01.

- `GET /chat/quick` for an entitled user creates exactly one conversation with
  `chat_mode='quick'`; a second request returns the same conversation and creates
  nothing.
- `GET /chat/quick` for a user without chat entitlement is a 403.
- The Quick chat does not appear in `conversation_groups` but is returned as
  `quick_conversation`.
- `_chat_context` and `get_conversation` return the same `threads` payload for
  the same conversation — assert equality between the two, so they cannot drift.
- With three thread rows inserted directly, the rendered page contains two
  dividers (thread 1 gets none) positioned before the right messages.
- A standard conversation's snapshot has an empty `threads` list and its rendered
  HTML contains no `data-thread-break`.
- `/chat/{uuid}` still resolves for a normal conversation — the literal `quick`
  segment did not shadow the UUID route.

For the controller tests, follow the existing pattern in
`tests/web/test_chat_integration.py` for building a request with a session and an
entitled user, and remember to create Skrift's `User` table as
`tests/web/test_chat_dispatch.py:60-68` does.

Run `uv run pytest`. Compare the failure set against a clean tree — several
failures are pre-existing (see stage 01's note).

## Acceptance criteria

- `/chat/quick` is registered in both `app.yaml` and `app.development.yaml`,
  loads for an entitled user, and is idempotent.
- A person can send a message in the Quick chat and get a reply — it is a normal
  conversation in every respect that this stage does not change.
- The model and reasoning selects work in the Quick chat, so the person chooses
  the chat agent.
- Dividers render identically on first load and after a reconcile, and reconciling
  twice does not duplicate them.
- No visible change to any existing standard conversation.
- `uv run pytest` shows no new failures; `semgrep` and `gitleaks` clean.

## Notes

- Nothing in this stage creates a thread boundary. If a Quick chat needs a first
  thread row, create it lazily — either when the first turn is submitted or, more
  simply, treat "no thread rows" as "one implicit thread covering everything",
  which is what `history_floor` already does by returning `0`. Prefer the
  implicit reading; do not write an `origin='initial'` row just to have one, or
  the divider logic has to special-case it in two more places.
- Resist adding a "start new thread" button for the person. The goal describes the
  agent deciding and the evaluator deciding. A manual control is a separate
  question worth asking before building.
