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
  cannot see. That is the only place verbatim message text survives, it lives
  in Redis rather than the database, and it is bounded — see the next section.

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
attribute cost — and the ids, counts, decisions and the agent's own output do
all of that without the row holding anybody's words.

## Where verbatim message text still exists, and for how long

| Where | What it holds | Bound |
| --- | --- | --- |
| Chat agent working history (`smarter_dev/bot/services/chat_memory.py`, Redis) | The conversation the chat agent is currently in. | 2-hour key TTL, refreshed on write. Compaction (`chat_compaction.py`) summarises everything older and keeps at most a 20,000-character verbatim tail. |
| Proactive agent history (`smarter_dev/bot/proactive/history_store.py`, Redis, with a recovery copy in `proactive_agent_histories`) | The running history the proactive agent reasons over. | Compaction keeps at most the trailing 8 messages verbatim and replaces the rest with a summary. |
| Proactive wake stream, one per guild (Redis) | The notification envelope that woke a guild, message text included. | Trimmed to 48 hours by stream id on every publish, and again on the passive tick for guilds that stopped publishing. |
| Proactive shadow stream (Redis) | The same envelopes, copied where canary workers can read them. | The same 48-hour trim, plus a 10,000-entry cap. |
| A claimed proactive batch (Redis) | Envelopes handed to a wake that has not acknowledged them. | Expires after 48 hours. |

The two agent histories are the "chat bot history" the policy carves out: they
are the bot's short-term working memory, they are not queryable by an operator,
and they are not in the database except as the proactive agent's crash-recovery
copy. Every other row in the table above is a Redis hand-off between the bot and
the proactive worker, bounded by the same 48-hour window the sweep uses.

One gap, stated plainly: the proactive *pending* list (a non-waking envelope
queued for the next wake) is capped at 20 envelopes by count, not by age. It is
drained by the next wake, so a guild that never wakes again can hold up to 20
verbatim envelopes until it does.

## What the write path stores

Every column below would otherwise hold message text. The web tier is the only
place these rows are built, and `smarter_dev/shared/message_content.py` is the
only thing that decides what may go in them.

| Table | Written as the placeholder | Written as sent |
| --- | --- | --- |
| `chat_agent_turns` | each triggering message's body, its attachments, and the user-prompt and tool-return parts of the model transcript | `agent_output`, tool names and call arguments, tokens, cost, model, timing |
| `chat_agent_compaction_events` | the compacted original content | the compaction `summary` and all char counts |
| `help_conversations` | every scraped context message, whatever the interaction type; `user_question` when the member reached the bot by mention or streak reply | `user_question` when the member typed it as a slash-command argument, plus `bot_response`, tokens, latency |
| `forum_agent_responses` | the post title, the post body, and the attachment list (emptied) | tags, confidence, `decision_reason`, `response_content`, responded flag |
| `handler_runs` | every message-bearing key of `trigger_context`, including any future key following the `*_content` convention | trigger type, ids, flags, counters, role lists, outcome |

`help_conversations` redacts context for slash commands too: `/tldr` is a slash
command whose context is a verbatim channel scrape. Only `user_question` is
conditional, because a question typed as a command argument is a submission to
us rather than a message we read.

`handler_runs` stores a redacted context but hands the script the real one. The
handler still runs against what the member actually wrote; only the permanent
audit row is redacted.

## What the sweep still clears

`smarter_dev/web/retention.py` runs hourly and blanks text on rows older than
48 hours, stamping `content_purged_at`. Its remaining job is text the AI wrote
about what it read — a summary quotes nobody but describes everything — plus
back-filling rows written before write-time redaction landed.

| Table | Cleared after 48h | Kept |
| --- | --- | --- |
| `help_conversations` | `bot_response`, plus the already-redacted question and context | ids, interaction type, tokens, latency |
| `chat_agent_turns` | `agent_output` (the reply plus the agent's running topic and notes), plus the already-redacted triggering messages and transcript delta | tokens, cost, model, reasoning level, timing |
| `chat_agent_engagements` | the denormalised running topic and notes | activation ids, aggregate tokens/cost |
| `chat_agent_compaction_events` | the compaction `summary` | char counts, summariser cost |
| `chat_agent_errors` | `provider_body` — a provider error can echo the prompt back, and no write-time rule can tell when it does | error type, traceback, status code |
| `forum_agent_responses` | `decision_reason`, `response_content`, plus the already-redacted title and body | confidence, tokens, responded flag |
| `moderation_actions` | `ai_context_summary` | action, target, moderator, reason, duration, timestamp |
| `handler_runs` | the script's error message on `error` and `cap_exceeded` rows | trigger type, ids, flags, outcome, all counters |

A script that trips over the message it is reacting to puts that text into its
exception message, and nothing at write time can tell which errors quote a
member — so `handler_runs.error` is treated like every other derived text the
sweep owns. The `outcome` column still says the fire failed, so an old failure
stays visible as a failure. The explanations on `skipped` and `rearmed` rows are
written by the bot itself, never by a script, so they stay.

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

The rule is enforced, not just documented: `smarter_dev/web/handler_lint.py`
rejects any stored script that branches on a message's leading command word,
and every handler-authoring prompt carries the same rule in prose. Matching a
keyword anywhere in a message is still allowed — that is a keyword watch, not a
command.

## What is out of scope, and why

- `quest_submissions`, `challenge_submissions` — typed into one of our modals.
  An explicit submission to us, kept as a game record.
- `research_sessions` (`/scan`) — the query is a deliberate command argument and
  the result is a user-facing artifact with its own lifecycle.
- `agent_conversations` / `agent_messages` — the website's own agent chat, not
  Discord.
- `proactive_agent_histories` — the proactive agent's own working history,
  covered above. It is a recovery copy of the Redis history, bounded by the same
  compaction, and it is not an operator-facing audit trail.
- The chat agent's own memory. Three tables, exempt for two different reasons:

  | Table | What it holds | Why it is exempt |
  | --- | --- | --- |
  | `chat_agent_guild_memory` | One ≤2000-character markdown document per guild: who the people here are to the bot, the running jokes, the opinions it has formed. | Prose the bot wrote about itself, not message text it read. |
  | `chat_agent_memory_revisions` | The last five nights of that document, per guild. | Same — it is the history of the bot's own writing. |
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
- Logs. Log lines name message ids, author ids and character counts, never
  message text, so the log stream is not a second copy of the thing this
  document is about.
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

The proactive Redis streams are not swept by that job. They are trimmed by the
bot itself: on every publish, and on the passive ticker for the guilds that
stopped publishing.

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
