# Discord data retention

What the bot stores from Discord, for how long, and why. This is the reference
behind the privileged-intent justification, so it should describe the system as
it actually behaves — if the code changes, change this file in the same commit.
`tests/web/test_retention.py` pins the numbers below against the constants they
come from.

## The rule

**Verbatim Discord message text is written to durable storage only as the
placeholder `[message content]`.**

Redaction happens at write time, in the web tier, at the moment the row is
built — not later, on a timer. Nothing needs to expire for the guarantee to
hold, and a database dump taken one second after a message was sent contains
the placeholder, not the message. Text that was empty stays empty: a
placeholder is never invented where nobody wrote anything.

Two things are deliberately excluded from the rule:

- **What a member deliberately submitted to us** — typed into one of our
  modals, or passed as a slash-command argument — is a normal user submission
  with its own lifecycle, not something we read off a channel because we hold
  the message-content intent.
- **The AI agents' own working history**, which cannot answer a conversation it
  cannot see. It lives in Redis, apart from one crash-recovery copy of the
  proactive agent's history in `proactive_agent_histories`.

The next section is the one list of every place verbatim text survives — the
two histories, the Redis hand-offs that feed the proactive agent and the
handler workers, and a handler timer's own payload — and what bounds each of
them.

The bot's own words are not kept either. A reply, a tool call's arguments, the
agent's running topic and notes, a `/help` answer and a forum agent's
reasoning and reply can all quote or closely paraphrase a member — a reply
that quotes the question, a web search whose query is lifted from a message —
so the records of what the AI did store them as the placeholder too.

We keep the surrounding *row*: timestamps, token counts, cost, model name, the
decision the agent reached, the moderation action taken. That is what pays for
the intent — it is how we monitor and prove out abuse of the AI integrations,
and how we track spend. None of it contains anyone's words.

Moderation auditing of what was actually said does not use these rows. It uses
the audit log the bot posts to the guild's activity channel, which is the
guild's own record in the guild's own space.

## Why we read messages at all

Every one of these features requires the model to see what people wrote:

| Feature | Why it reads messages |
| --- | --- |
| Chat agent | Answers questions in-channel; needs the conversation to answer. |
| Proactive agent | Watches a channel and decides whether it has something worth saying. |
| Help agent (`/help`) | Answers a question using the surrounding channel context. |
| AI moderation | Triages a reported/flagged incident from what was actually said. |
| Forum agent | Evaluates a new forum post and decides whether to answer it. |
| Channel handlers | User-authored automations that react to messages. |

The audit row an agent leaves behind is a separate question from the text it
needed in the moment. An operator has to be able to see *why* the AI did what
it did — to answer an abuse report, to debug a bad or harmful answer, to
attribute cost — and the ids, counts, tool names and decisions do that
without the row holding anybody's words, the bot's included. The text itself
was posted to the channel, where the guild's own audit log keeps it.

## Where verbatim message text still exists, and for how long

| Where | What it holds | Bound |
| --- | --- | --- |
| Chat agent working history (`smarter_dev/bot/services/chat_memory.py`, Redis) | The conversation the chat agent is currently in. | 2-hour key TTL, refreshed on write — that TTL is the bound. Compaction (`chat_compaction.py`) folds everything older than roughly the last 20,000 characters into a summary, keeping more when a single turn is larger than that, and the history grows again until the next fold. |
| Proactive agent history (`smarter_dev/bot/proactive/history_store.py`, Redis, with a recovery copy in `proactive_agent_histories`) | The running history the proactive agent reasons over. | Size, not age: no key TTL and no sweep. Compaction fires only once the history passes 100,000 estimated tokens, and then keeps at most the trailing 8 messages verbatim, summarising the rest. |
| Proactive wake stream, one per guild (Redis) | The notification envelope that woke a guild, message text included. | Trimmed to 48 hours by stream id on every publish, and again on the passive tick for guilds that stopped publishing. |
| Proactive shadow stream (Redis) | The same envelopes, copied where canary workers can read them. | The same 48-hour trim, plus a 10,000-entry cap. |
| A claimed proactive batch (Redis) | Envelopes handed to a wake that has not acknowledged them. | Expires 48 hours after the claim, not after the write, so a claimed envelope can outlive its own write cutoff by up to one more window. |
| Proactive pending list, one per guild (Redis) | Non-waking envelopes queued for the next wake, message text included. | Each envelope is dropped once it is 48 hours old, on the bot's 15-minute passive tick, and counted in `pending-dropped` so the agent is told; so an envelope lasts at most 48 hours and 15 minutes while the tick runs. An envelope this version cannot read (no `created_at`, or a field it does not know) is dropped and counted on the same tick, however new. The list's own expiry is a backstop for a bot that stopped ticking: every push moves it out to 30 minutes past the new envelope's 48 hours, and every tick resets it to 30 minutes past the newest envelope left, so it never deletes an envelope uncounted. If the tick stops while pushes go on, older envelopes stay until the tick runs again or the list expires 48 hours 30 minutes after its last push. Also capped at 20 envelopes, and drained by the next wake. |
| Handler fire hand-off (Redis, `handler-fire:context:*`) | The verbatim trigger context of an event that fired a handler, read back by the fire job so the script sees the real message. | 1-hour key TTL, set once and never refreshed. The job payload in Skrift's worker tables carries the redacted context and a random reference to this key, never the text. A fire that finds the key gone is recorded as `skipped` and does not run. |
| Handler timer payloads in the Skrift worker tables (`worker_queue`, `worker_state`) | What a handler script chose to carry to its own later fire. A script can copy the message it was reacting to into it. | Until the timer fires, however far ahead the script set it, then the 7 days Skrift keeps a finished job's state. Only a script that copies message text into a timer payload puts any there; the `handler_runs` audit row empties the payload whatever it holds. |
| Handler script memory (`channel_handlers.memory` and `admin_handlers.memory`, one JSON blob per handler; `guild_handler_memory`, one row per key shared by a guild's admin handlers) | Whatever a handler script chose to keep between fires. A script can copy text from the message it reacted to into it. | No age bound. A handler's own memory lasts as long as the handler. Guild memory is tied to no handler: uninstalling an extension or deleting a handler leaves it, and a key stays until a script deletes it. Each store is capped at 16 KB (`smarter_dev/web/handler_memory.py`, `handler_guild_memory.py`). Only a script that copies message text puts any there; the bot's own handlers keep counters, ids and timestamps. |
| Candidate blog topics (`candidate_blog_topics`) | Blog-post ideas the chat agent once filed from a conversation: a headline, an observation, a scope, a list of evidence strings and a category. These are the agent's words, but the evidence can paraphrase or quote what members said. | No age bound and not swept. The chat agent's output has had no blog-topic field since July 2026, so the current bot writes no new rows. The web endpoint still stores any `blog_topic_candidates` a turn sends, evidence included, so a bot image from before that change (in a rollback or canary) would add rows. The blogging pipeline's Review and Brainstorm stages and the blogging admin page read them, so clearing them is an operator decision, not a sweep. |
| Moderation's `ai_context_summary` (`moderation_actions`) | A free-text field the AI moderation tools may fill. Today only the purge tool writes it, with a count (`Purged 3 message(s)`); no code reads it. | 48 hours, cleared by the hourly sweep. |

The two agent histories are the "chat bot history" the policy carves out: they
are the bot's short-term working memory, they are not queryable by an operator,
and they are not in the database except as the proactive agent's crash-recovery
copy. The two proactive streams, the claimed batch and the pending list are
Redis hand-offs between the bot and the proactive worker; the claimed batch is
the one *bounded* key whose window runs from the claim rather than from the
write. The external proactive-agent worker sets the same expiry on the batches
it claims, and its dead-letter stream keeps ids and an error type, no text.

Three places have no age bound at all, stated plainly. The proactive agent's
history has no clock, so a guild that never talks enough to trigger compaction
keeps every verbatim message it has read, in Redis and in its
`proactive_agent_histories` row, for as long as the channel stays enabled.
Handler script memory keeps whatever a script stored, up to 16 KB per store:
a handler's own memory for as long as the handler exists, guild memory until a
script deletes the key. `candidate_blog_topics` rows stay until an operator
discards them.

Skrift's worker tables hold no handler fire's message text: the payload of a
fire is redacted at dispatch, and a fire that fails leaves Skrift only its
exception types and frames. What they keep of finished work is deleted by the
hourly retention job (see How it runs).

## What the write path stores

Every column below would otherwise hold message text. The web tier is the only
place these rows are built, and `smarter_dev/shared/message_content.py` is the
only thing that decides what may go in them.

| Table | Written as the placeholder | Written as sent |
| --- | --- | --- |
| `chat_agent_turns` | every field of a triggering message except its ids, reply pointers, reactions, flags and timestamp; every part of the model transcript, whoever wrote it — prompts, tool returns, the system prompt, the reply text, reasoning and tool-call arguments — stripped down to its kind, tool name, call id, tool kind, outcome and timestamp; in `agent_output`, the reply, its voice summary and voice instruction, the running topic and notes, and each ranking's reasoning; and a failed voice send's error, which keeps only its exception type | the decision in `agent_output` (which messages it scored and how high, which message it replied to and whether as a reply, the language, whether it kept watching), tool names, tokens, cost, model, timing |
| `chat_agent_engagements` | the copy of the running topic and notes the list view shows | activation ids, aggregate tokens/cost |
| `chat_agent_compaction_events` | the compacted original content, and the compaction `summary`, which retells it | all char counts and the summariser's tokens and cost |
| `chat_agent_errors` | every error's exception message and the provider body. Any of them can quote a member (a provider body echoing the prompt, a validation error quoting its input, a Discord API error), so the bot builds the traceback from the exception objects (each chained exception's type and stack frames) and never reads a message | the error type, the status code, and the traceback's types and frames (file, line, function) |
| `help_conversations` | every scraped context message, whatever the interaction type; `user_question` for every interaction type except a slash command (today: mention and streak reply); `bot_response` | `user_question` when the member typed it as a slash-command argument, plus tokens, latency |
| `forum_agent_responses` | the post title, the post body, the attachment list (emptied), the agent's `decision_reason` and its reply, `response_content` | tags, confidence, responded flag |
| `handler_runs` | every message-bearing key of `trigger_context`, including any future key following the `*_content` convention, plus a timer re-fire's `payload`, whose keys a handler script chose rather than the host, so the whole value is emptied; and a script's error message, which can quote the message the script tripped on: the error is built from the exception (the `runtime` label, its type and the script frames, or the cap's name), never from its message. A `compile` error keeps its message, since a script is compiled before it sees any context | trigger type, ids, flags, counters, role lists, outcome, the bot's own explanation on `skipped` and `rearmed` rows |

`help_conversations` redacts context for slash commands too: `/tldr` is a slash
command whose context is a verbatim channel scrape. Only `user_question` is
conditional, because a question typed as a command argument is a submission to
us rather than a message we read.

`handler_runs` stores a redacted context but hands the script the real one. The
handler still runs against what the member actually wrote; only the permanent
audit row is redacted.

The admin pages that showed the bot's words — a conversation's turns, the
engagement list's topic, a help conversation's answer, a forum agent's
analytics — show the placeholder instead, and a turn's voice reply can no
longer be replayed. Searching help conversations by answer text finds nothing.

## What the sweep still clears

`smarter_dev/web/retention.py` runs hourly. Its first pass applies the
write-time redaction of the bot's own words to rows written before it landed,
whatever their age: a turn's `agent_output` and transcript, an engagement's
topic and notes, a help `bot_response`, a forum agent's `decision_reason` and
`response_content`, and a voice-send error that still carries its message. A
row already written that way is left alone.

Its second pass blanks text on rows older than 48 hours, stamping
`content_purged_at`. That is the back-fill for rows written before each
write-time redaction landed, and the 48-hour bound on `ai_context_summary`.

| Table | Cleared after 48h | Kept |
| --- | --- | --- |
| `help_conversations` | the already-redacted answer, question and context | ids, interaction type, tokens, latency |
| `chat_agent_turns` | the already-redacted `agent_output`, triggering messages and transcript delta | tokens, cost, model, reasoning level, timing |
| `chat_agent_engagements` | the already-redacted topic and notes, 48 hours after the engagement's last turn, and again whenever a later turn wrote them | activation ids, aggregate tokens/cost |
| `chat_agent_compaction_events` | the compaction `summary` (back-fill only; written as the placeholder) | char counts, summariser cost |
| `chat_agent_errors` | the exception message and the whole traceback of every row not written redacted, whatever its status or body, and `provider_body` (back-fill only; written redacted) | error type, status code, and the types and frames of a row written redacted |
| `forum_agent_responses` | the already-redacted reasoning, reply, title and body | confidence, tokens, responded flag |
| `moderation_actions` | `ai_context_summary` | action, target, moderator, reason, duration, timestamp |
| `handler_runs` | the script's error on `error` and `cap_exceeded` rows (back-fill only; written redacted) | trigger type, ids, flags, outcome, all counters |

The `outcome` column still says a fire failed, so an old failure stays visible
as a failure. The explanations on `skipped` and `rearmed` rows are written by
the bot itself, never by a script, so they stay.

Moderation keeps everything except the AI's retelling of the exchange. An
action's `reason` — whether a moderator typed it or the triage agent wrote it —
is the justification for something *we* did, and it is already published
elsewhere: DM'd to the target and posted to the mod log. The action record never
expires, because it is about what we did, not about what anyone said.

## Prefix commands

Text prefix commands are prohibited, so the bot ships no handler that reacts
to a member typing a `!command`. A bot that has to read every message to notice
`!ping` cannot justify the message-content intent, and the same behaviour
belongs on a slash command, a reaction, or the chat agent.

Two bundled extensions changed to make that true. The `sus` extension was
nothing but the `!sus` and `!list_sus` commands, so it was deleted outright.
`disboard-bumping` lost its `!bumpers` / `!bumps` handler and kept its bump
tracker, which reacts to the Disboard bot's confirmation embed rather than to
anything a person typed.

The rule is enforced where scripts are produced, not just documented:
`smarter_dev/web/handler_lint.py` rejects a script that branches on a message's
leading command word, and it runs on every AI-authored script before that script
is offered for approval (`smarter_dev/bot/agents/handler_authoring.py`) and on
every bundled extension script as it is rendered
(`smarter_dev/extensions/rendering.py`). Every handler-authoring prompt carries
the same rule in prose. The write endpoints themselves do not re-run the lint,
so a script pushed straight into the API by an operator is held to the rule by
review rather than by code. Matching a keyword anywhere in a message stays
allowed — that is a keyword watch, not a command.

## What is out of scope, and why

- `quest_submissions`, `challenge_submissions` — typed into one of our modals.
  An explicit submission to us, kept as a game record.
- `research_sessions` (`/scan`) — the query is a deliberate command argument and
  the result is a user-facing artifact with its own lifecycle.
- `agent_conversations` / `agent_messages` — the website's own agent chat, not
  Discord.
- `work_dispatches` — one row per job the site hands to a worker (chat
  turns, sub-agents, account deletion, web search, Resources questions); a
  Resources row holds the question in full. No age bound and no sweep, and deleting the
  account does not remove it (no foreign key to the user); runbook step 8
  deletes a person's rows for a request.
- `proactive_agent_histories` — the proactive agent's own working history,
  with the bounds (and the missing ones) described above; it is not an
  operator-facing audit trail.
- The chat agent's own memory. Three tables, exempt for two different reasons:

  | Table | What it holds | Why it is exempt |
  | --- | --- | --- |
  | `chat_agent_guild_memory` | One ≤2000-character markdown document per guild: who the people here are to the bot, the running jokes, the opinions it has formed. Beside it, a ≤750-character behavior block (how the bot has learned to act there) and a ≤250-character personality block (who it is there). | Prose the bot wrote about itself, not message text it read. |
  | `chat_agent_memory_revisions` | The last five nights of that document and its two blocks, per guild. | Same — it is the history of the bot's own writing. |
  | `chat_agent_memory_notes` | Notes the bot keeps mid-conversation, in its own words. | Deleted outright by the nightly job that folds them into the document, so they normally live under a day. A night whose dream fails keeps its notes for the next night, and a guild whose memory is paused (below) keeps them indefinitely, because the dream that consumes them is skipped. |

  The rule the bot is held to when writing any of it is *remember the person,
  not the transcript*: no verbatim quotes, and nothing private, sensitive, or
  shared in confidence. So there is nobody's message content in here to scrub —
  only what the bot made of a day. It is still personal data: the dream asks
  for people to be named by username and Discord id, and the document is about
  them.

  This memory is permanent and the bot never resets it. Only the agent edits
  its own memory; no operator tool rewrites, blanks or deletes it, and none
  should be added. `memory_enabled` on a guild's row is a pause, not a reset:
  set false, the stored document, blocks, notes and revisions are all kept as
  they are, the bot is not shown them and the nightly dream skips the guild.
  Note writes do not check the flag, so a paused guild still accumulates
  notes. Nothing in the application sets the flag today.

  Removing one person from this memory is a purge the agent carries out
  itself, given that person's id: it rewrites the lines of the document, its
  blocks, notes and revisions that mention them and leaves everything else
  intact. The admin starts it from the Privacy Purges page
  (`/admin/bot/privacy-purges`, #79); nothing else may edit this memory for a
  deletion request (`docs/privacy-deletion-runbook.md`).
- Identity fields everywhere: user ids, usernames, display names, snowflakes.
  These come from the members intent, not the message-content intent, and an
  abuse record is worthless without knowing who it concerns.
- Logs. The goal is that log lines name message ids, author ids and character
  counts, never message text, so the log stream is not a second copy of the
  thing this document is about. The bot does not meet it yet: some bot and
  agent log sites still print text a member wrote (#47 tracks them). Every
  path that reads members' messages or builds prompts from them — the chat
  and proactive agents, `/help` and `/tldr`, the forum and moderation agents,
  the audit, content, attachment and spam filters, and the handler runtime —
  logs a failure as its exception types and stack frames only
  (`smarter_dev/shared/exception_logging.py`), never the exception's message,
  and the image generators log their prompt's length, not the prompt. The
  external proactive-agent worker does the same, and its errors from the app
  API and Discord carry a status and Discord error code, never the response
  body. The web
  side does not log email addresses, any part of a rejected bearer token,
  query-string values of bot API requests, or httpx's outbound request URLs
  (capped at WARNING in `main.py`).
- Model-written working notes beside the chat history: the running topic
  (24-hour key) and notes (2-hour key) per channel, and the guild's recent
  bot-event log (one-hour window, newest 200 events, holding usernames and
  moderation reasons). These are the agent's prose and the bot's own events,
  not message text, and they expire on their own clocks.
- In-memory only, never written down: the spam engine's message buffer, the
  message gate, and the chat agent's live context window. These die with the
  process.

## How it runs

`k8s/cron-retention-sweep.yaml` runs `scripts/retention_sweep.py` hourly, so the
true worst case is 48–49 hours rather than the 48–72 a daily job would give. The
sweep is idempotent (a stamped row is skipped) and commits per table, so a run
that dies partway through keeps the tables it finished and the next hourly run
picks up the rest. The first run after this change scrubs everything that
predates write-time redaction, so expect it to take substantially longer than
steady state.

Run it by hand against the current environment with:

```
uv run python scripts/retention_sweep.py
```

Operators can also hard-delete emptied help-conversation rows outright from
`/admin/help-conversations/cleanup`; the sweep only blanks the text.

The same job bounds Skrift's worker tables
(`smarter_dev/web/worker_retention.py`), because Skrift's own pruner is not
deployed and its Postgres backends never delete an expired or dead row by
themselves. Job payloads, agent run state (a Resources question, an agent's
prompt) and error text live there.

Live work is never deleted, however old. Live work is what Skrift can still
run or resume: a job with a queue row that was not dead-lettered (queued,
claimed, paused with a wake time, or a handler timer due weeks ahead); an
agent session whose hot run state has not expired and is not finished; and a
job that is not finished and either belongs to such a session or changed in
the last 7 days. Skrift resumes a session only from its hot run state, which
it keeps for 7 days after the session's last write, so a session idle longer
than that, or left running by a worker that died, can no longer resume and is
no longer live. Hot run state written before Skrift gave it that sliding
expiry has none at all; it is live only while it changed in the last 7 days.

A job that waits with neither a queue row nor a session — an inline job paused
on its own — is live for 7 days after it last changed, then deleted. Nothing
in this codebase submits one: every inline job here runs an agent session,
and a session keeps its jobs live.

| Table | Deleted |
| --- | --- |
| `worker_state` | once the row's own expiry has passed (Skrift sets 7 days on a finished job's state and 24 hours on a finished agent run's), and a job's or session's state that is not live and has not changed in 7 days |
| `worker_queue` | a dead-lettered job (it holds the job's payload) 7 days after it was dead-lettered; a pending job never |
| `worker_dead_letters` | 7 days after it was written, open or resolved |
| `worker_events`, `worker_archive_events`, `worker_archive_snapshots` | 7 days after they were written, unless they belong to live work |

A live session keeps its event stream, its newest snapshot and every stored
blob its state or events name; its older snapshots are history and go. Skrift
stores a job's or session's state wrapped as `{"__skrift_pydantic__": ...,
"value": {...}}`, so its status is at `value -> 'value' ->> 'status'`. The
blogging pipeline's admin timeline reads a run's event stream, so a run
finished more than 7 days ago shows no timeline.

Skrift also keeps a failed job's error, with its traceback, in the job's
state, its attempt history, its dead letter and its lifecycle events. A
handler fire holds a member's message while it runs, so both fire jobs replace
any exception at the job boundary with one that carries only the original's
types and frames (`smarter_dev/web/job_errors.py`).

The proactive Redis streams and pending lists are not swept by that job. The
bot trims the streams on every publish and on its 15-minute passive tick, for
the guilds that stopped publishing too, and trims each pending list by
envelope age on the same tick. The external proactive-agent worker trims its
dead-letter stream to 48 hours on every write and every 15 minutes.

## Agent web-search previews

User-facing chat-agent searches create an immutable capability link showing the
query and ordered Brave result snippets the agent saw. The preview is reserved
before the provider call (so the initial Discord tool-use message can link to a
pending page), populated when the search returns, and never performs a search
when loaded or refreshed.

These snapshots have a separate fixed 48-hour lifecycle. The public controller
rejects them as soon as `expires_at` is reached, and the same hourly retention
job then hard-deletes the expired rows. Only a SHA-256 hash of the random URL
token is stored. Preview pages are read-only, unlisted, and marked `noindex`.

## What the public notice states

The notice at `/privacy` (`smarter_dev/shared/privacy_notice.md`) lists each
kind of data a member would recognise, with one retention figure: the longest
any store of that kind can keep it, stated as a fact. Where a kind spans
several stores the figure is the longest of them, so most of the stores
behind it keep less. A figure must never be shorter than a store behind it;
change the notice in the same commit as anything here that raises a bound.
`tests/shared/privacy_notice_test.py` pins each figure.

| Notice says | Stores behind it | Bound, and where it is enforced |
| --- | --- | --- |
| The chat bot's memories: permanent | `chat_agent_guild_memory`, `chat_agent_memory_revisions` (last five nights), `chat_agent_memory_notes` | No bound (above); only the agent purge edits it for a request |
| The chat bot's conversations: until a deletion request | chat agent working history, running topic (24-hour key) and notes (2-hour key), the guild's bot-event log (one hour); proactive history in Redis and `proactive_agent_histories`, and the external worker's copy | Proactive history has no age bound; the chat history's 2-hour TTL is refreshed on every write, so an active conversation has no fixed end either. The agent purge (step 4 of the runbook) removes the person |
| Messages being handled: 5 days | proactive wake and shadow streams, claimed batches, pending lists; `handler-fire:context:*` (1 hour); `mediaread:*`, the AI's reading of a posted file (24 hours, `CACHE_TTL_SECONDS` in `smarter_dev/web/media_read.py`) | Longest is a claimed batch: an envelope can sit 48 hours in a stream, then a claim keeps it 48 hours more, about 4 days. Pending lists can outlast 48 hours only while the bot's passive tick is stopped |
| Server automations: until a deletion request | handler script memory and guild memory (16 KB each), handler timer payloads | No age bound (above); runbook steps 7 and 8 clear the person's entries |
| Blog post ideas: until a deletion request | `candidate_blog_topics`, and blogging pipeline sessions that copied them | No age bound, not swept; runbook step 6 |
| Records of what the AI did: until a deletion request | `chat_agent_turns`, `chat_agent_engagements`, `chat_agent_compaction_events`, `chat_agent_errors`, `forum_agent_responses`, `handler_runs`, `help_conversations` rows, usage cost rows | No age bound on the rows; text is written as the placeholder or cleared by the sweep. The runbook anonymises or deletes the person's rows; an anonymised row still holds message IDs, channel and tag IDs (including the DM channel ID), times, role details and a permission flag, reaction emoji, and moderation action details. The notice sums these up as "which messages and channels were involved and what was done" |
| `/help` questions and web searches: 3 days | `help_conversations.user_question` typed as a slash-command argument; `search_result_previews` | Both 48 hours, then the hourly sweep: at most 49 hours |
| Moderation: permanent | `moderation_actions`; the bot's posts to the moderation and audit log channels | No bound; not part of a deletion request |
| Games and community features: until a deletion request | bytes balances and transactions, squad memberships, quest and challenge submissions and progress, member activity, forum subscriptions, `/help` and `/tldr` records, legacy `/scan` rows | No bound; member leave removes that guild's bytes balance and squad membership |
| Rate limits and caches: 30 days | `chatlimit:*` (4-hour window, `user_message_limit.py`), `hcap:dmuser:*` (1 hour), `hdm:chan:*` (7 days, `handler_emitter.py`), `hclaim:*` (a script's claim, at most 30 days, `CLAIM_TTL_MAX_SECONDS` in `handler_caps.py`) | Longest is `hclaim:*` |
| Your account: until the account is deleted; signed in 30 days after the last visit | the site account, profile, linked logins and stored Discord tokens, push subscriptions; legacy GitHub/Google accounts | Session `max_age` 30 days, rolling (`app.yaml`) |
| Chat: until deleted; Resources questions until a deletion request; the AI's own copy of a Resources question, its research and its answer 8 days after it finishes | site chat conversations and attachments; Resources questions, including `work_dispatches` (each holds the full question; nothing sweeps it and account deletion does not reach it, since it has no foreign key to the user); the Resources and chat-title agents' Skrift sessions and jobs, and Skrift's queued notifications (24 hours) | Conversations, attachments and Resources conversations go with the account (a queued job); `work_dispatches` rows go only with a deletion request (runbook step 8). Account deletion leaves the agents' copies in Skrift's worker tables, which the hourly retention job deletes 7 days after their last write once the work is finished (rounded up to 8 days for the hourly run; a session that can still resume is live and has no limit); runbook step 8 removes them for a request |
| Searches: until a deletion request; searches made with a search link while signed out 30 minutes | dashboard searches; anonymous search keys (`TTL_SECONDS` in `smarter_dev/web/web_search/anonymous.py`), each holding the search text, queries, results, answer, the link owner's ID and the browser session that ran it | Not removed by account deletion; runbook step 5 |
| Email: until a deletion request | campaign and waitlist signups | No bound |
| Security: 30 days in Pydantic Logfire | security events (below) | Logfire organisation retention |
| Monitoring: 30 days in Pydantic Logfire; servers' own logs with no fixed time limit | errors and traces from the bot and the website; container stdout | Logfire organisation retention (the Personal plan default, never configured otherwise); container logs are bounded by size and pod lifetime, not time |

The notice no longer carries these details, recorded here instead: the
external proactive-agent worker reads channel messages from Discord directly,
as well as receiving them from the bot, and keeps its own copy of the
proactive history; legacy GitHub/Google accounts can also hold passkeys
(deleted with the account's second-factor enrollments, runbook step 5); a
dashboard search stores the query, the results and the answer; the audit log
channel posts carry a message's old and new text and its author.

Short-lived copies not named in the notice (Skrift's worker tables, 7 days
after work finishes) fall under the notice's "gone within 30 days of your
request".

## Retention is not deletion

Everything above is about where message *text* is kept and for how long.
None of it removes a person: a row whose text is redacted or blanked keeps
its Discord ids and usernames, the agent histories keep usernames and ids inside their prose, and
bytes, squads, quests, challenge submissions, activity dates and moderation
actions are game and moderation records keyed by Discord id with no clock at
all. Deleting a site account removes the account and its site chat, but
nothing keyed by Discord id (#45 tracks that gap). Member leave removes only
that guild's bytes balance and squad memberships.

Deleting one person's data is a manual request handled by the admin until it
is automated, with the chat bot's part done by the agent purge (#79). The
public notice is `/privacy` (`smarter_dev/shared/privacy_notice.md`); the
admin's steps, including what is kept and what cannot be removed yet, are in
`docs/privacy-deletion-runbook.md`.

## Security logs

Security events are structured logs, not database rows. Three kinds are
emitted (`smarter_dev/web/security_logger.py`), named in the `security.event`
attribute: `login_failed` (with `bearer_presented`, a `reason` code and the
`client_ip`, the source of the attempt), `rate_limit_exceeded` (with the
`key_id` of the API key, the `window`, `current_usage` and
`rate_limit`), and `admin_operation` (with `operation`, the calling key's
`key_id`, and `details`). Each also carries `success`, the
`http.route` template (`/api/guilds/{guild_id}/bytes/balance/{user_id}`,
never the
concrete path) and the `http.method`. No event
records a member's Discord id. The names avoid the words Logfire's default
scrubber redacts, and route templates sit under a key it never scrubs, so the
events arrive readable. They go to Pydantic Logfire
when the process has a `LOGFIRE_TOKEN` and are kept there for 30 days, the
default retention of the Logfire organisation's Personal plan (never
configured otherwise); without Logfire they go to the standard logger (container
stdout). Ordinary successful API requests are not logged at all.

Rate limiting keeps one Redis sorted set per API key: the times of its allowed
requests, keyed by the key's id, expiring 15 minutes after the last request.

The `security_logs` table that used to hold these, one row per bytes API call
with the Discord ids in request paths, has been dropped along with its rows.
