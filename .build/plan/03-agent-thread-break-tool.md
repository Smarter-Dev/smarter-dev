# Stage 03 — The agent's `start_new_thread` tool

**Stable id:** `agent-thread-break-tool`

## Outcome

Give the chat agent a tool that draws a thread boundary when the person has
clearly changed the subject. This is the first thing that actually creates a
boundary, so the dividers built in stage 02 start appearing, and the history
flooring built in stage 01 starts taking effect.

The tool is only available inside a Quick chat, and only once the current thread
has something in it to break away from.

## Prior state (stages 01–02, already committed)

- `WebChatConversation.chat_mode` is `'standard'` or `'quick'`.
- `web_chat_threads` + `smarter_dev/web/chat/threads.py` with `current_thread`,
  `history_floor`, `open_thread`, `derive_thread_title`.
- `_structured_history` in `smarter_dev/web/chat/jobs.py` is floored to the
  current thread, compaction lookup included.
- `/chat/quick` exists, gets-or-creates one Quick chat per person, and the rail
  pins it.
- Thread dividers render from a `threads` list, in the template and in
  `chat.js` `syncThread`.

## Assumptions

On the review thread, may come back corrected:

- **The boundary lands in front of the message being answered, and the agent
  finishes the turn on the context it already has.** The agent can only recognise
  a topic break *after* it has been handed the previous thread, so those tokens
  are already spent; re-running the turn on a clean context would double the cost
  of exactly the turn we are trying to make cheap. Every turn after this one
  starts clean, which is where the saving is.
- **"Only on a clear topic break" is enforced structurally, not just asked for.**
  The tool is absent when there is nothing to break from, it demands a written
  reason, and it refuses a second call in the same turn.

## Context you need before starting

- `smarter_dev/web/chat/jobs.py` — `_build_root_agent` at line **1121** builds the
  root agent and defines every tool as a closure over the turn. Tools are
  registered imperatively at **2031–2039**:

  ```python
      agent.tool(search_web, name="web_search")
      agent.tool(read_web, name="web_read")
      agent.tool(run_code_tool, name="run_code")
      agent.tool(write_document)
      agent.tool(edit_document)
      agent.tool(read_document)
      agent.tool(list_documents)
      if needs_title:
          agent.tool(set_chat_title)
  ```

  The tool schema comes from the Python signature and docstring via pydantic-ai
  introspection. First parameter is always `ctx: RunContext`.

- **`set_chat_title` at jobs.py:1977–2029 is the model to copy.** It is the
  closest existing tool to this one: conditionally registered based on
  conversation state, capped by its own counter rather than the shared tool
  budget, writes through a guarded statement in `threads.py`'s sibling module
  `conversations.py`, publishes a notification, and returns a sentence the model
  can act on when the write did not apply. Read it end to end before writing
  anything.

  The house tool body shape, from that file: `_tool_state(...)` hard-cutoff check
  → `_publish_activity(status=…)` → do the work → `_record_tool_result(...)` →
  `_done_thinking(...)` → `_format_tool_result(result, after)`.

- `smarter_dev/web/chat/subagents.py` — `BASE_PROMPT`, `TOOL_GUIDANCE`,
  `DOCUMENT_GUIDANCE`, `SUBAGENT_GUIDANCE`, `TITLE_GUIDANCE`, composed by
  `effective_system_prompt(*, child: bool, needs_title: bool = False)`. Child
  agents drop the document, subagent and title sections.
- `smarter_dev/web/chat/toolsets.py` — `ExecutionCounters` and the
  `accept_tool` / `accept_search` / `accept_subagent` budget.
- `themes/smarterdev/static/js/chat.js` — the notification handler at line 2300
  dispatches on event type; the list it handles is at 2308–2340. `syncThread` at
  1640 is the reconcile path.
- Sequence arithmetic: the user message of a turn sits at
  `turn.response_sequence - 1`.

## What to build

### 1. `QUICK_THREAD_GUIDANCE` in `subagents.py`

Add a prompt section and a `quick_thread: bool = False` parameter to
`effective_system_prompt`, appended only for the root agent, the same way
`TITLE_GUIDANCE` is.

Write it to describe the bar, not merely the mechanism. Something in the register
of the existing sections — direct, second person, no bullet lists:

> This is a Quick chat: one continuous conversation split into threads. When the
> person's message is plainly about a different subject from what came before —
> not a follow-up, not a tangent, not a change of angle on the same problem, but a
> genuinely new topic — call `start_new_thread(reason)` before you answer. Doing so
> draws a line in the conversation and starts your context fresh from their
> message. Everything above the line stays on their screen and their uploads and
> documents remain available to you. Do not use it to tidy up a long thread, and do
> not use it when you are unsure — a wrongly drawn line costs the person context
> they wanted you to keep. Answer their message as normal afterwards, and do not
> mention the tool.

Getting this wording right matters more than the code around it. The whole
"clear topic break" requirement lives here.

### 2. The tool in `_build_root_agent`

Compute availability alongside the existing `needs_title` at jobs.py:1159:

```python
quick_thread = conversation.chat_mode == "quick" and bool(history)
```

`history` is empty when the current thread has no prior exchanges, which is
exactly the "nothing to break away from" case — so a brand-new Quick chat and the
first message after a boundary both correctly lack the tool. Pass `quick_thread`
into `effective_system_prompt` so the guidance and the tool appear and disappear
together.

Define the tool as a closure next to `set_chat_title`:

```python
    thread_break_used = False

    async def start_new_thread(ctx: RunContext, reason: str) -> str:
        """Start a new thread because the person has clearly changed the subject.

        Pass a short reason naming the old subject and the new one.
        """
```

Body:

1. If `thread_break_used`, return a sentence telling the model the line is already
   drawn and to answer the question — do not raise. A raised error becomes a retry
   prompt and can cost the person their reply.
2. Reject an empty or whitespace-only `reason` with `"error: reason is
   required"`, recorded through `_record_tool_result` like the other tools do.
   Cap it at a sane length (say 500 characters) and reject control characters,
   mirroring `SubagentDispatch.validated()` in `subagents.py:51-64`.
3. `_tool_state(...)` and return `USAGE_LIMIT_RESULT` on `hard_cutoff`, as every
   other tool does.
4. `_publish_activity(..., tool="start_new_thread", status="Starting a new thread")`.
5. Call `open_thread(session, conversation_id=conversation.id,
   start_sequence=turn.response_sequence - 1,
   title=derive_thread_title(user_message_content), origin="agent",
   reason=reason)`. `open_thread` returns `None` when a thread already starts
   there — treat that as success and set `thread_break_used`, since it means a
   retry of this same turn already drew the line.

   The tool closure needs the user's message text for the title.
   `_build_root_agent` receives `prompt`, which is the user message with the
   upload manifest possibly prepended. Pass the raw `user_message.content`
   through to `_build_root_agent` as its own parameter rather than stripping the
   manifest back off — `run_chat_turn` already has it in scope at jobs.py:2391.
6. Set `thread_break_used = True`.
7. `await _notify_safe(owner_id, "chat_thread_started", conversation.id, turn.id,
   thread_id=..., start_sequence=..., title=...)` so the divider appears live, and
   record the same through `_event(turn_id, "chat_thread_started", …)` so a missed
   ephemeral notification is recoverable — every other notification in this file
   is mirrored that way.
8. Return through `_record_tool_result` / `_done_thinking` /
   `_format_tool_result` like the neighbours, with a result along the lines of
   `"A new thread has started. Answer their message; nothing above the line is
   yours to refer to."`

Register it:

```python
    if quick_thread:
        agent.tool(start_new_thread)
```

**Do not put it behind `accept("tool", …)`.** `set_chat_title` deliberately sits
outside the shared budget for the same reason: a turn that has burned its tool
allowance on searches must still be able to draw the line, or the budget silently
disables the feature on exactly the busiest turns. The `thread_break_used` flag is
its cap.

### 3. Live divider in the browser

In `themes/smarterdev/static/js/chat.js`, handle `chat_thread_started` in the
notification dispatcher at 2308–2340: insert a `[data-thread-break]` before the
article for the in-flight turn's user message, using the same element builder
stage 02 added to `syncThread` so the live insert and the reconcile produce
identical markup. The subsequent reconcile must adopt it rather than duplicate it
— the keep-set logic stage 02 added, keyed by thread id, already does this
provided the live insert sets `data-thread-id`.

### 4. Sub-agents do not get this tool

`effective_system_prompt(child=True)` already drops the root-only sections;
confirm `quick_thread` follows the same rule and that
`tool_names_for_child` in `subagents.py:31` has nothing to say about it (it only
strips `run_subagent`). A sub-agent rewriting the conversation's thread structure
would be a real bug; make sure the child agent construction at jobs.py:2989 and
its tool registrations at 3177–3179 cannot reach it.

## Tests (write these first)

New `tests/web/quick_chat_thread_tool_test.py`.

There is no VCR or cassette layer in this repo and `pydantic_ai` `FunctionModel`
is only used in `tests/bot/agents/test_message_gate.py` — read that file for the
pattern if you want a real agent under test. For most of these, testing the tool
closure's behaviour directly is both cheaper and more precise than driving a
model; extract the body into a module-level function in
`smarter_dev/web/chat/threads.py` taking explicit arguments if that makes it
testable without standing up a whole turn. Prefer that over an elaborate mock.

- The tool is registered when `chat_mode='quick'` and history is non-empty.
- It is **not** registered for a standard conversation, nor for a Quick chat whose
  current thread has no prior exchanges, nor for a sub-agent.
- `effective_system_prompt(child=False, quick_thread=True)` contains the guidance;
  `child=True` does not, and `quick_thread=False` does not.
- Calling it opens a thread at `turn.response_sequence - 1` with `origin='agent'`
  and the given reason.
- A second call in the same turn changes nothing and returns the
  already-drawn sentence.
- An empty or whitespace `reason` is refused and no thread is opened.
- Calling it when a thread already starts at that sequence (the worker-retry case)
  reports success and does not raise.
- After a boundary, `_structured_history` for the *next* turn returns only the
  messages from the boundary onward — the end-to-end proof that the tool does what
  it is for. Build this from real rows rather than mocks.

Run `uv run pytest`; compare the failure set to a clean tree (several failures are
pre-existing — see stage 01).

## Acceptance criteria

- In a Quick chat, an agent call to `start_new_thread` inserts a `web_chat_threads`
  row, publishes `chat_thread_started`, and the divider appears without a reload
  and survives a reconcile without duplicating.
- The turn that drew the line still answers the person's message.
- The next turn's history starts at the boundary.
- The tool and its prompt section are absent from standard conversations and from
  sub-agents.
- `uv run pytest` shows no new failures; `semgrep` and `gitleaks` clean.

## Notes

- Worth doing by hand once against the real stack before calling this done: the
  prompt wording is the whole feature here, and a model that draws lines eagerly
  is worse than one that never draws them. If it fires on follow-up questions,
  tighten the guidance rather than adding code.
- `scripts/chat_eval.py` exists as an eval harness for the Discord chat agent, but
  a known gap (recorded in the project's memory notes) is that it cannot reproduce
  tool-firing faithfully because it bypasses the model-catalog routing the real
  runtime uses. Do not trust a pass/fail from it for this tool without
  reconciling that first.
