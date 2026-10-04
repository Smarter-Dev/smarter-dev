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
| Moderation's `ai_context_summary` (`moderation_actions`) | A free-text field the AI moderation tools may fill. Today only the purge tool writes it, with a count (`Purged 3 message(s)`); no code reads it. | 48 hours, cleared by the hourly sweep. |

The two agent histories are the "chat bot history" the policy carves out: they
are the bot's short-term working memory, they are not queryable by an operator,
and they are not in the database except as the proactive agent's crash-recovery
copy. The two proactive streams, the claimed batch and the pending list are
Redis hand-offs between the bot and the proactive worker; the claimed batch is
the one *bounded* key whose window runs from the claim rather than from the
write. The external proactive-agent worker sets the same expiry on the batches
it claims, and its dead-letter stream keeps ids and an error type, no text.

One place has no age bound at all, stated plainly. The proactive agent's
history has no clock, so a guild that never talks enough to trigger compaction
keeps every verbatim message it has read, in Redis and in its
`proactive_agent_histories` row, for as long as the channel stays enabled.

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
- `proactive_agent_histories` — the proactive agent's own working history,
  bounded as described above; it is not an operator-facing audit trail.
- The chat agent's own memory. Three tables, exempt for two different reasons:

  | Table | What it holds | Why it is exempt |
  | --- | --- | --- |
  | `chat_agent_guild_memory` | One ≤2000-character markdown document per guild: who the people here are to the bot, the running jokes, the opinions it has formed. Beside it, a ≤750-character behavior block (how the bot has learned to act there) and a ≤250-character personality block (who it is there). | Prose the bot wrote about itself, not message text it read. |
  | `chat_agent_memory_revisions` | The last five nights of that document and its two blocks, per guild. | Same — it is the history of the bot's own writing. |
  | `chat_agent_memory_notes` | Notes the bot keeps mid-conversation, in its own words. | Deleted outright by the nightly job that folds them into the document — they live under a day and never reach a 48-hour cutoff. |

  The rule the bot is held to when writing any of it is *remember the person,
  not the transcript*: no verbatim quotes, and nothing private, sensitive, or
  shared in confidence. So there is nobody's message content in here to scrub —
  only what the bot made of a day. A guild that would rather it forgot has a
  switch: `memory_enabled` on its row turns the memory off and blanks the
  document.
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

## Security logs

`security_logs` records one row per bytes API call and per authentication or
admin event: IP address, user agent, request path and query, and whatever
Discord ids those carry. Rows are kept for 90 days and then deleted outright by
the same hourly retention job (`smarter_dev/web/security_log_retention.py`); a
security log with its identifying columns blanked would have no audit value
left, so there is nothing to scrub and keep. The delete runs in batches of
1,000, each committed on its own, so the first run's backlog is never one long
transaction and a run that dies partway keeps the batches it finished. Nothing
reads further back than 90 days: rate limiting counts the last 15 minutes.
