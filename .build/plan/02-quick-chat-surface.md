# Stage 02 — The Quick chat surface: it becomes `/chat`, with thread dividers

**Stable id:** `quick-chat-surface`

## Outcome

Make Quick chat the default chat view. `GET /chat` stops showing an empty
"start a new chat" page and instead opens the person's own single, perpetual
Quick chat — the place for quick questions and one-off conversations. The page
that `/chat` used to show moves to `GET /chat/new`, which is where the rail's
"+ New chat" button now points, so ordinary multi-turn conversations are still
one click away and behave exactly as they do today.

The Quick chat is pinned at the top of the history rail rather than sinking into
the by-recency groups. Thread boundaries render as a labelled divider in the
message stream, in both places the stream is built: the server render on page
load and the client reconcile after a reconnect.

Nothing draws a boundary yet — the agent's tool is stage 03 and the idle
evaluator is stage 04 — so in practice a Quick chat has one thread and no divider
appears. The rendering has to land first so stage 03 has something to draw on,
and it is tested here against thread rows inserted directly.

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

## Settled with the reviewer

These were open questions during planning and have since been answered. They are
decisions now, not guesses:

- **Quick chat is the default chat view** and a single perpetual conversation per
  person — "intended for quick questions and one-off conversations". This is why
  it takes over `/chat` rather than living at a URL of its own.
- **One pinned row in the sidebar**, not one row per thread. Each new thread is
  separated inside that same conversation's history.
- **A boundary changes what the agent reads, not what the person sees.** They can
  still see every message; the agent only sees what was sent since the current
  thread began. Uploads and written documents stay listed for the whole Quick chat.

## Context you need before starting

- `smarter_dev/web/chat/controller.py` — `chat_index` (`GET /chat`),
  `chat_conversation` (`GET /chat/{conversation_id:uuid}`), and the context
  builders `_rail_context`, `_catalog_context`, `_chat_context`. Both page routes
  render the one template `chat/index.html`, which serves two products through a
  `mode` variable of `"chat"` or `"resources"`. That `mode` is **not**
  `chat_mode` from stage 01 — keep them straight.
- `smarter_dev/web/chat/api.py` — `ChatApiController`, mounted at
  `/v2/api/chat`. `create_conversation`, `get_conversation` (the reconcile
  snapshot), `delete_conversation`, `owned_conversation`, `require_entitled`.
- `themes/smarterdev/templates/chat/index.html` — the `history_row` macro, the
  "+ New chat" link, the rail, the empty state, and the message loop.
- `themes/smarterdev/static/js/chat.js` — one IIFE, ES5-style `var`, no
  framework and no build step. `syncThread(snapshot)` is the reconcile path; its
  sweep removes every element matching `.chat-message` not in the keep set, so
  divider elements need their own keep set. `createConversation()` runs only when
  the shell carries no conversation id — `/chat` now always carries one, so the
  lazy-create path only runs on `/chat/new`, which is correct as-is. After
  deleting the conversation on screen the JS navigates to `/chat`, which now
  lands on the Quick chat — a fine destination.
- `app.yaml` and `app.development.yaml` — routes are registered declaratively
  under the `controllers:` key in **both** files. A handler added to only one
  works in production and 404s in development, or the reverse.
- CSP is `script-src 'self' https://cdn.jsdelivr.net` — **no inline scripts**.
  State reaches the JS through `data-*` attributes or a
  `<script type="application/json">` payload block.
- `smarter_dev/web/chat/entitlements.py` — `has_chat`, `has_ultra_chat`. Chat is
  gated by role, not by a Litestar `Permission`.

## What to build

### 1. Split today's `/chat` into two routes

**`GET /chat/new`** — everything `chat_index` does today, unchanged: the empty
shell with the intelligence / model / reasoning selects and the starter prompts,
with `conversation` set to `None` so the template renders the empty state. Move
the existing handler body here rather than rewriting it.

This route is load-bearing beyond convenience: `intelligence_mode` is fixed when
a conversation is created and is immutable at the database level (a trigger,
`trg_web_chat_mode_immutable`), so the empty state's intelligence select is the
*only* place it can ever be chosen. Losing that page would quietly remove the
ability to start an Intelligence-mode conversation.

**`GET /chat`** — now the Quick chat:

1. Same gate as before: `require_user_id`, load the `User`, check
   `user.is_active` and `has_chat(permissions)`, else a 403 with the same wording.
2. Get-or-create the caller's Quick chat — the single `WebChatConversation` with
   `owner_user_id == user_id`, `chat_mode == 'quick'` and `archived_at IS NULL`.
   When creating, resolve model, reasoning and intelligence mode from
   `ChatSettings` through the same helper `create_conversation` uses, including
   the Ultra-intelligence downgrade for people without `has_ultra_chat`. Set
   `title="Quick chat"`, `title_is_custom=True` (nothing should rename it),
   `chat_mode="quick"`, `status="idle"`, `next_sequence=1`.
3. Render `chat/index.html` with the same context `chat_conversation` builds for a
   web chat, plus the `threads` list and `quick_chat=True`.

**Handle the create race.** Two tabs hitting `/chat` at once will both try to
insert. The partial unique index from stage 01 makes the loser's `INSERT` raise;
catch `sqlalchemy.exc.IntegrityError` specifically, roll back, re-select, and
continue with the row that won. Catching a broad `Exception` here would violate
the fail-fast rule in `CLAUDE.md`.

A `GET` that creates a row is a deliberate exception, worth a comment saying so:
it is a get-or-create for the person's own workspace, it is idempotent, and the
route is behind auth and marked `noindex,nofollow`.

Register both handlers in `app.yaml` **and** `app.development.yaml`. Neither
shadows `/chat/{conversation_id:uuid}`, because that parameter is UUID-typed —
but add a test that proves `/chat/new` reaches the new handler and that a real
conversation UUID still reaches `chat_conversation`.

### 2. Point "+ New chat" at `/chat/new`

In `themes/smarterdev/templates/chat/index.html`, the chat branch of the
"+ New chat" link becomes `/chat/new`. Leave the resources branch alone.

### 3. Keep the Quick chat out of the recency groups

`_rail_context` selects the owner's unarchived conversations and buckets them
into Today / This week / Older. Exclude `chat_mode == 'quick'` from that query
and return it separately as `quick_conversation`, so it can be pinned rather than
appearing twice. Every page that renders the rail merges `_rail_context`, so one
change covers all of them.

### 4. Pin it in the rail

Above the Today / This week / Older groups, render a row for
`quick_conversation` linking to `/chat`, marked `aria-current="page"` when it is
the conversation on screen.

It should **not** get the rename / archive / delete row menu the ordinary rows
have. A Quick chat is not filed away like a normal conversation, and those
actions would leave the person with no Quick chat and no obvious way back. If
reusing the `history_row` macro makes that awkward, render the pinned row as its
own small block instead of bending the macro.

Back that up on the server, because the API is reachable directly and the
conversation id is in the page: make `delete_conversation` and the archive path
in `update_conversation` refuse a conversation whose `chat_mode` is `'quick'`,
with a 409 and a clear message. A guard only in the UI is not a guard.

### 5. Threads in both snapshots

The stream is built twice and the two must agree, or a reconnect will silently
delete the dividers the server rendered.

- `_chat_context` — add a `threads` key: every `WebChatThread` for the
  conversation ordered by `sequence`, as
  `{"id", "sequence", "start_sequence", "title", "origin"}`.
- `get_conversation` — add the identical list to the JSON snapshot.

Write **one** function in `smarter_dev/web/chat/threads.py` that both call. Do
not build the list twice. For a standard conversation it returns an empty list,
so nothing about the existing render changes.

### 6. Render the divider

**Template** — in the message loop, before each `<article class="chat-message">`,
emit a divider when a thread starts at that message's sequence. Every stored
thread row is a real mid-conversation boundary with messages above it (no
`origin='initial'` row is ever written — see Notes), so every row draws its
divider.

Build a `{start_sequence: thread}` lookup in the controller rather than scanning
the thread list inside the Jinja loop. Markup along the lines of:

```html
<div class="chat-thread-break" data-thread-break data-thread-id="{{ thread.id }}"
     data-start-sequence="{{ thread.start_sequence }}" role="separator">
  <span class="chat-thread-break-label">{{ thread.title or 'New thread' }}</span>
</div>
```

**JS** — `syncThread`, two changes:

- While walking `snapshot.messages`, insert or adopt a `[data-thread-break]`
  before the article whose message sequence matches a thread's `start_sequence`.
  The snapshot's message entries need to carry `sequence` for this — check
  whether `message_dict` already includes it and add it if not.
- The sweep removes everything matching `.chat-message` not in the keep set. A
  `.chat-thread-break` is not a `.chat-message`, so today it would survive
  forever and accumulate duplicates on every reconcile. Give dividers their own
  keep set and their own sweep, keyed by thread id.

Match the file's style: `var`, no arrow functions, no template literals, no
optional chaining. It is deliberately ES5-flavoured.

**CSS** — `themes/smarterdev/static/css/pages/chat.css`. A horizontal rule with
the thread label centred on it, in the existing visual language — look at how
`.chat-history-group` and the `p-label` / `p-meta` classes are styled (small
caps, dim, letter-spaced). Keep it quiet: it marks a boundary, it is not a heading.

## Tests (write these first)

New `tests/web/quick_chat_surface_test.py`, plus additions to the thread tests
from stage 01. Follow `tests/web/test_chat_integration.py` for building a request
with a session and an entitled user, and create Skrift's `User` table the way
`tests/web/test_chat_dispatch.py` does.

- `GET /chat` for an entitled user creates exactly one conversation with
  `chat_mode='quick'`; a second request returns the same one and creates nothing.
- `GET /chat` for a user without chat entitlement is a 403.
- `GET /chat/new` renders the empty state with `conversation` as `None` and still
  offers the intelligence select — the regression guard for the route split.
- `GET /chat/{uuid}` still resolves for a normal conversation.
- The Quick chat does not appear in `conversation_groups` but is returned as
  `quick_conversation`.
- `DELETE` and archive against the Quick chat are refused; against a standard
  conversation they still work.
- `_chat_context` and `get_conversation` return the same `threads` payload for the
  same conversation — assert equality between the two so they cannot drift.
- With thread rows inserted directly, the rendered page contains one divider per
  stored row, each positioned before the right message.
- A standard conversation's snapshot has an empty `threads` list and its HTML
  contains no `data-thread-break`.

Run `uv run pytest`. Compare the failure set against a clean tree — several
failures are pre-existing (listed in stage 01), so confirm the set is unchanged
rather than expecting zero.

## Acceptance criteria

- `/chat` opens the person's Quick chat, idempotently, and both it and `/chat/new`
  are registered in `app.yaml` and `app.development.yaml`.
- "+ New chat" reaches `/chat/new`, and starting a conversation from there behaves
  exactly as it does today, intelligence select included.
- A person can send a message in the Quick chat and get a reply — it is a normal
  conversation in every respect this stage does not change, and the model and
  reasoning selects work, so they choose the chat agent.
- The Quick chat cannot be deleted or archived through the API.
- Dividers render identically on first load and after a reconcile, and reconciling
  twice does not duplicate them.
- No visible change to any existing standard conversation.
- `uv run pytest` shows no new failures; `semgrep` and `gitleaks` clean.

## Notes

- **The Quick chat's intelligence mode is whatever `ChatSettings.default_intelligence_mode`
  says at the moment it is created, and can never be changed** — the column is
  immutable by database trigger and the Quick chat is created implicitly, so it
  never passes through the empty state's select. That suits "quick questions and
  one-off conversations", and the administrator default governs it. Worth
  mentioning in the pull request so it is a known property rather than a surprise.
- Nothing in this stage creates a thread boundary. Do not write an
  `origin='initial'` row just to have one — treat "no thread rows" as "one
  implicit thread covering everything", which is already what `history_floor`
  does by returning `0`. A consequence to honour everywhere: **every stored
  thread row is a mid-conversation boundary and draws a divider** — there is no
  "first thread row that gets no divider", because the opening thread has no row.
- Resist adding a manual "start new thread" button. The reviewer described the
  agent deciding and the evaluator deciding; a manual control is a separate
  question worth asking before building.
