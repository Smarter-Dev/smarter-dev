# Discord data retention

What the bot stores from Discord, for how long, and why. This is the reference
behind the privileged-intent justification, so it should describe the system as
it actually behaves — if the code changes, change this file in the same commit.

## The rule

**Message content taken passively from Discord is deleted after 48 hours.**

"Passively" is the important word. Anything a user deliberately submits to us —
typed into one of our modals, passed as a slash-command argument — is a normal
user submission with its own lifecycle. Anything the bot read off a channel
because it holds the message-content intent is transient by default, and gets
scrubbed on a fixed 48-hour window whether or not anyone asked us to.

We keep the surrounding *row*: timestamps, token counts, cost, model name, the
decision the agent reached, the moderation action taken. That is what pays for
the intent — it is how we monitor and prove out abuse of the AI integrations,
and how we track spend. None of it contains anyone's words.

## Why we hold message content at all

Every one of these features requires the model to see what people wrote:

| Feature | Why it reads messages |
| --- | --- |
| Chat agent | Answers questions in-channel; needs the conversation to answer. |
| Help agent (`/help`) | Answers a question using the surrounding channel context. |
| AI moderation | Triages a reported/flagged incident from what was actually said. |
| Forum agent | Evaluates a new forum post and decides whether to answer it. |
| Channel handlers | User-authored automations that react to messages. |

And the reason any of it lands in the database rather than staying in memory:
an operator has to be able to see *why* the AI did what it did — to answer an
abuse report, to debug a bad or harmful answer, and to attribute cost. Forty-eight
hours is the window where that is still actionable.

## What gets scrubbed

`smarter_dev/web/retention.py` is the single authority. Each table carries a
`content_purged_at` marker, stamped when its text is nulled out.

| Table | Cleared after 48h | Kept |
| --- | --- | --- |
| `help_conversations` | question, answer, channel context | ids, interaction type, tokens, latency |
| `chat_agent_turns` | triggering messages, agent output, model transcript | tokens, cost, model, reasoning level, timing |
| `chat_agent_engagements` | running topic and notes | activation ids, aggregate tokens/cost |
| `chat_agent_compaction_events` | original content, summary | char counts, summariser cost |
| `chat_agent_errors` | provider error body (can echo the prompt) | error type, traceback, status code |
| `forum_agent_responses` | post title/content/attachments, decision reason, reply | confidence, tokens, responded flag |
| `moderation_actions` | AI context summary | action, target, moderator, reason, duration, timestamp |
| `handler_runs` | message text inside `trigger_context` | trigger type, ids, flags, all counters |

`handler_runs` keeps the non-content parts of its trigger context — which
trigger fired, in which channel, for whom — and drops every key that carries
message text, including any future key following the `*_content` convention.

Moderation keeps everything except the AI's retelling of the exchange. An
action's `reason` — whether a moderator typed it or the triage agent wrote it —
is the justification for something *we* did, and it is already published
elsewhere: DM'd to the target and posted to the mod log. The action record never
expires, because it is about what we did, not about what anyone said.

## What is out of scope, and why

- `quest_submissions`, `challenge_submissions` — typed into one of our modals.
  An explicit submission to us, kept as a game record.
- `research_sessions` (`/scan`) — the query is a deliberate command argument and
  the result is a user-facing artifact with its own lifecycle.
- `agent_conversations` / `agent_messages` — the website's own agent chat, not
  Discord.
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
- In-memory only, never written down: the spam engine's message buffer, the
  message gate, and the chat agent's live context window. These die with the
  process.

## How it runs

`k8s/cron-retention-sweep.yaml` runs `scripts/retention_sweep.py` hourly, so the
true worst case is 48–49 hours rather than the 48–72 a daily job would give. The
sweep is idempotent (a stamped row is skipped) and commits per table, so a run
that dies partway through keeps the tables it finished and the next hourly run
picks up the rest. The very first run after deploy scrubs everything that
predates the sweep, so expect it to take substantially longer than steady state.

Run it by hand against the current environment with:

```
uv run python scripts/retention_sweep.py
```

Operators can also hard-delete emptied help-conversation rows outright from
`/admin/help-conversations/cleanup`; the sweep only blanks the text.

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
