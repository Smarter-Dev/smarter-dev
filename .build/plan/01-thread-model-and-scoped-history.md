# Stage 01 — Thread boundaries in the data model, and history scoped to a thread

**Stable id:** `thread-model-and-scoped-history`

## Outcome

Add the storage and the history rule that the whole "Quick chat" feature rests on:
a conversation can be marked as a Quick chat, and a Quick chat's messages are
divided into **threads**. When the agent runs a turn in a Quick chat, it is only
handed the messages belonging to the current thread — everything before the most
recent thread boundary is invisible to the model, though it stays on screen and
in the database.

Nothing user-visible changes in this stage. No route creates a Quick chat yet, so
every existing conversation keeps `chat_mode = 'standard'` and takes exactly the
code path it takes today. Stage 02 adds the surface, stage 03 the agent's tool,
stage 04 the idle evaluator.

## Background: what "Quick chat" is

Quick chat is a single, long-lived chat per person — they never click "+ New
chat". When the subject changes, a **thread boundary** is drawn in place: the
messages stay in one continuous scroll, but the agent's context restarts from the
boundary. Boundaries get drawn two ways, both in later stages: the agent calls a
tool on a clear topic break (stage 03), or the person came back after a long gap
and a cheap evaluator model judged the new message to be a new subject
(stage 04). This stage builds the boundary itself and makes it mean something.

## Settled with the reviewer

These were open questions during planning and have since been answered. They are
decisions now, not guesses:

- **A Quick chat is one perpetual conversation per person** — a single thread in
  the sidebar, pinned to the top, and the default chat view. Hence a flag on the
  conversation plus threads inside it, rather than a new conversation type or a
  rail row per topic.
- **A thread boundary changes what the agent reads, not what the person sees.**
  In the reviewer's words: the user can still see them all, the agent only sees
  what was sent since the start of the current thread. Older messages, uploads
  and written documents all stay visible and downloadable.
- **Threads never nest and never reopen.** They are a straight sequence: at any
  moment exactly one is current, and it is the last one started.

## Context you need before starting

Read these. This codebase is large and the Chat runtime has several invariants
that are easy to break by accident.

- `smarter_dev/web/models.py` — one SQLAlchemy 2.0 async models file, schema
  `skrift`. `WebChatConversation` at ~line 4330, `WebChatTurn` ~4388,
  `WebChatMessage` ~4459, `WebChatCompaction` ~4607. `Base` (from
  `smarter_dev/shared/database.py`) already supplies `created_at` / `updated_at`
  — do not redeclare them.
- `smarter_dev/web/chat/jobs.py` — the worker-side turn runner.
  `_active_messages_before` at line 624 and `_structured_history` at 644 are the
  two functions this stage changes. `_maybe_compact` at 798 is the third.
- `alembic/main/env.py` — `SCHEMA = "skrift"`, version table
  `alembic_version_app`, and a `MAIN_TABLES: frozenset[str]` (lines ~35–120) that
  gates `include_object`. **A new table is silently skipped by autogenerate
  unless its name is added to `MAIN_TABLES`.** `tests/test_migration_ownership.py`
  `ast`-parses that literal and asserts it equals the model set minus the tables
  Skrift core owns, so forgetting it fails the suite.
- Migration head is `e5a8c2d9f4b1`
  (`alembic/main/versions/20260807_120000_e5a8c2d9f4b1_chat_guild_memory.py`).
  Chain onto it. The chain must stay linear — this repo has one head.
- House migration style: read that same file. A long prose docstring saying *why*,
  then plain unannotated `revision` / `down_revision` globals, `UUID =
  postgresql.UUID(as_uuid=True)`, and a local `stamps()` helper for the timestamp
  columns. The mako template still emits the older annotated form; follow the
  recent hand-written files, not the template.
- `tests/conftest.py` — `db_session` (line ~136) is a per-test SQLite file with
  foreign keys on and all tables recreated. There is no `tests/web/conftest.py`.
  Tests needing Skrift's `User` table create it themselves; copy
  `tests/web/test_chat_dispatch.py:60-68`.
- Global engineering rules in `CLAUDE.md` and `~/.claude/CLAUDE.md`: descriptive
  names, no inline imports, builtin generics (`list[str]` not `List[str]`),
  `X | None` never `Optional`, fail fast and only catch specific exceptions,
  prefer pure functions and do not mutate values the caller owns. **TDD — write
  the tests first.**

## Key facts about how history is built today

`_structured_history(conversation, turn)` (jobs.py:644) rebuilds the pydantic-ai
message list like this:

1. `_active_messages_before(conversation.id, turn.response_sequence)` selects
   `WebChatMessage` rows where `role == "assistant"`, `is_active` is true, and
   `sequence < turn.response_sequence`, ordered by `sequence`. Only assistant rows
   are read because each one's `model_message` JSON holds the whole request +
   response delta, user prompt included.
2. It looks up the newest complete `WebChatCompaction` for the conversation and,
   if its `version_fingerprint` still matches, seeds `history` from
   `compacted_messages` and skips every row at or below `through_sequence`.
3. It decodes each remaining row's `model_message` onto the end.

Sequence arithmetic, from `api.py` `submit_turn`: `response_sequence =
conversation.next_sequence * 2`, so **the user message sits at
`response_sequence - 1` and the assistant reply at `response_sequence`**. A
thread therefore starts at an odd sequence — the user message that opened it.

## What to build

### 1. `chat_mode` on `WebChatConversation`

Add to `smarter_dev/web/models.py`:

```python
chat_mode: Mapped[str] = mapped_column(
    String(16), nullable=False, default="standard", server_default=text("'standard'")
)
```

with a `CheckConstraint("chat_mode IN ('standard','quick')", name="web_chat_chat_mode")`
added to `__table_args__`.

Two things to be careful of:

- This is a **different axis** from `intelligence_mode`, which is already on this
  table. Say so in a comment. `intelligence_mode` is how hard the agent works;
  `chat_mode` is which surface the conversation lives on.
- `intelligence_mode` is immutable at the database level — a `BEFORE UPDATE`
  trigger `trg_web_chat_mode_immutable` raises `web chat intelligence mode is
  immutable` (created in `20260730_120000_e8a1c2d3f4b5_smarter_dev_web_chat.py`,
  lines ~480–492). **Read that trigger's body before writing the migration** and
  confirm it only guards `intelligence_mode`. If it is written loosely enough to
  fire on the new column, the migration must replace the function with one that
  names `intelligence_mode` explicitly.

Also add a partial unique index enforcing one live Quick chat per person, in the
same spirit as the existing `uq_web_chat_turn_one_active`:

```python
Index(
    "uq_web_chat_one_live_quick",
    "owner_user_id",
    unique=True,
    postgresql_where=text("chat_mode = 'quick' AND archived_at IS NULL"),
    sqlite_where=text("chat_mode = 'quick' AND archived_at IS NULL"),
),
```

Stage 02's get-or-create relies on this to stay correct under a double-click.

### 2. `WebChatThread`

New model in `smarter_dev/web/models.py`, table `web_chat_threads`:

| Column | Type | Notes |
|---|---|---|
| `id` | UUID pk, `default=uuid4` | |
| `conversation_id` | UUID FK → `web_chat_conversations.id` `ON DELETE CASCADE`, not null | |
| `sequence` | Integer not null | 1, 2, 3… ordinal within the conversation |
| `start_sequence` | Integer not null | the `WebChatMessage.sequence` of the user message that opens this thread |
| `title` | Text nullable | shown on the divider; derived from the opening message |
| `origin` | String(16) not null | `'initial'`, `'agent'`, or `'evaluator'` |
| `reason` | Text nullable | what the agent or the evaluator said when it drew the boundary |

Constraints: `UniqueConstraint("conversation_id", "sequence")`,
`UniqueConstraint("conversation_id", "start_sequence")`, a
`CheckConstraint("origin IN ('initial','agent','evaluator')")`, and an
`Index("ix_web_chat_threads_conversation_start", "conversation_id", "start_sequence")`.

The unique constraint on `start_sequence` is load-bearing: it is what makes
"open a thread at this turn" idempotent when a worker retries a turn it already
partly processed. Say so in a comment.

There is deliberately **no `current_thread_id` pointer** on the conversation. The
current thread is the one with the highest `start_sequence`, which cannot drift
out of sync with the rows it describes.

### 3. `smarter_dev/web/chat/threads.py`

A new small module, the single place that knows the thread rules — the same way
`conversations.py` is the single place that knows the title rules. Read
`conversations.py` first; match its shape and its comment style.

```python
async def current_thread(session, *, conversation_id: UUID) -> WebChatThread | None
```
The thread with the highest `start_sequence`, or `None` for a conversation that
has none (every standard conversation, and a Quick chat before its first turn).

```python
async def history_floor(session, *, conversation: WebChatConversation) -> int
```
The lowest `WebChatMessage.sequence` the agent may see. `0` for a standard
conversation or a Quick chat with no threads yet; otherwise the current thread's
`start_sequence`. This is the one function the runtime calls, so the "standard
conversations are unaffected" rule lives in exactly one place.

```python
async def open_thread(
    session,
    *,
    conversation_id: UUID,
    start_sequence: int,
    title: str | None,
    origin: str,
    reason: str | None,
) -> WebChatThread | None
```
Insert the next thread. Returns the row, or `None` when a thread already starts at
that sequence — that is the idempotent retry case, not an error, so do not raise.
Compute `sequence` as `max(existing) + 1`. Do not mutate the conversation object
the caller passed in; if the conversation's context counters need resetting, the
caller does it (see below) or this function does it through its own `session`
query. Pick one and be consistent.

Also give it a pure helper for the divider label so stage 02 and stage 03 agree:

```python
def derive_thread_title(content: str) -> str
```
Reuse `derive_title` from `smarter_dev/web/chat/conversations.py` rather than
writing a second truncation rule.

### 4. Scope the runtime's history to the current thread

In `smarter_dev/web/chat/jobs.py`:

- Give `_active_messages_before` a `from_sequence: int = 0` parameter and add
  `WebChatMessage.sequence >= from_sequence` to the `where`. The default keeps
  every existing caller behaving exactly as before.
- In `_structured_history`, call `history_floor(...)` once and pass the result to
  `_active_messages_before`.
- **Also floor the compaction lookup.** A compaction snapshot made before the
  boundary summarizes the previous subject; seeding history from it would drag
  the old thread straight back in through the side door. Add
  `WebChatCompaction.through_sequence >= floor` to the `select` at
  `jobs.py:653-662`. A compaction made *after* the boundary was itself built from
  thread-scoped history, so it is safe to use.

  This is the single easiest thing to get wrong in this stage. Write the test for
  it before writing the code.

- `_maybe_compact` (jobs.py:798) receives `rows` from `_structured_history` and so
  is already thread-scoped. Read it anyway and confirm nothing inside it
  re-queries messages without the floor.

### 5. Reset the context meter when a thread opens

`WebChatConversation.current_context_tokens` drives the "12 % context" meter in
the composer. It is rewritten from the real request size on every primary
operation (`runtime.py:418`), so it self-corrects on the next turn — but leaving
it stale until then makes the meter lie at exactly the moment the person is
watching the divider appear. When `open_thread` succeeds, set
`current_context_tokens = 0`, `context_state = []`, and increment
`context_revision`.

Bumping `context_revision` matters beyond the meter: `jobs.py:852` keys the
compaction operation on it, so incrementing it stops a new thread's compaction
from colliding with the old thread's reservation key.

### 6. Migration

```sh
uv run alembic -c alembic/main/alembic.ini revision --autogenerate -m "quick chat thread boundaries"
```

Then rewrite the generated file by hand into house style (prose docstring, plain
globals). Add `"web_chat_threads"` to `MAIN_TABLES` in `alembic/main/env.py`
**before** running autogenerate or the table will be skipped. Confirm the file
contains the `web_chat_threads` table, the `chat_mode` column with its server
default and check constraint, and both partial/unique indexes, and that
`down_revision = "e5a8c2d9f4b1"`.

Apply and confirm:

```sh
uv run python scripts/migrate.py --only main
```

(`scripts/migrate.py` is the supported runner. Do not use the `skrift db` CLI —
it silently no-ops when the project path contains a space, which this worktree
path does.)

## Tests (write these first)

New file `tests/web/chat_threads_test.py` (the `*_test.py` suffix matches the
newer Chat tests):

- `history_floor` returns `0` for a standard conversation, `0` for a Quick chat
  with no thread rows, and the current thread's `start_sequence` once one exists.
- `open_thread` assigns `sequence` 1, 2, 3 in order; a second call at the same
  `start_sequence` returns `None` and inserts nothing.
- `open_thread` zeroes `current_context_tokens`, empties `context_state`, and
  increments `context_revision`.
- The one-live-Quick-chat index rejects a second unarchived `chat_mode='quick'`
  conversation for the same owner, and permits one whose `archived_at` is set.
- `derive_thread_title` truncates the same way conversation titles do.

Extend the history tests (add to `tests/web/chat_threads_test.py` rather than
disturbing existing files):

- Build a Quick chat with six assistant messages, open a thread at the fifth
  message's user sequence, and assert `_structured_history` returns only the
  messages from the boundary onward.
- The same fixture as a standard conversation returns **all** messages —
  the regression guard that this stage changed nothing for existing chats.
- With a completed `WebChatCompaction` whose `through_sequence` is below the
  boundary, `_structured_history` ignores it entirely rather than seeding from it.
- With a compaction whose `through_sequence` is above the boundary, it is still
  used.

Run the suite: `uv run pytest`. Note that some failures are pre-existing on a
clean tree and are **not** yours: two in `usage_invoice_test`, three needing a
real `OPENAI_API_KEY` (`test_web_research_contract`, `test_writer_agent`,
`chat_memory_dream_test`), and collection errors in `test_integration.py` /
`test_scan.py`. Confirm the set of failures is unchanged rather than zero.

## Acceptance criteria

- `uv run python scripts/migrate.py --only main` applies cleanly and
  `tests/test_migration_ownership.py` passes.
- A conversation left at `chat_mode='standard'` produces byte-identical history to
  before this stage.
- In a Quick chat with a thread boundary, `_structured_history` returns only the
  current thread's messages, and ignores any compaction that predates the boundary.
- `uv run pytest` shows no new failures.
- `semgrep` and `gitleaks` run clean before committing.

## Notes

- No route, template, JS or prompt change belongs in this stage. If you find
  yourself editing `themes/` or `subagents.py`, you have crossed into stage 02
  or 03.
- Do not add a `thread_id` column to `WebChatMessage`. Membership is derived from
  `sequence` against the thread's `start_sequence`, which keeps regeneration and
  version-group behaviour untouched — a regenerated reply keeps its original
  sequence, so it stays in the thread it was always in.
