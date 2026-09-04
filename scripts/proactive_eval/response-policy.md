# Proactive bot: when to respond, and how it is woken

The reference document for the proactive chat bot's behavior — the rules a
human judges it against and the eval's judge scores it against.

The runtime does not read this file. The rules the bot actually runs on are
`OPERATING_POLICY_BRIEF` in `smarter_dev/bot/proactive/agent.py`, which is a
condensed form of "When to respond" below; it is the agent's system-prompt
policy and the watcher's seed wake criteria. When the two disagree, the
brief is what shipped — fix the brief, then update this doc.

## The two passes

**Watcher** (DeepSeek V4 Flash, stateless). Reviews new messages and decides
only one thing: whether to wake the agent. It never writes to the channel.
It sees the new messages plus the trailing 30 messages of context, and the
current watch instructions — nothing else, and it remembers nothing between
calls.

**Agent** (Gemini 3.8 Flash by default). Woken by the watcher or by direct
engagement. Reads its notifications, optionally pulls more context with its
tools, and either acts or deliberately stays silent. Its conversation
history persists across wakes.

## How the bot is woken

Everything reaches the agent as a **notification**:

- **@mention or reply to the bot** — wakes it immediately and deterministically,
  no watcher judgment involved. Carries the message verbatim with its id and
  the author's name, id and roles.
- **Watcher summary** — wakes the agent when the watcher judges the activity
  worth its time; carries the summary and the relevant message ids. Summaries
  that do not wake are discarded, not queued.
- **Mode changes, watch-instruction expiries, restart recoveries** — never wake
  the agent on their own; they queue and ride along with the next wake.

## Monitoring modes

**Passive** (the default, and what most channels are in most of the time):
messages batch up and the watcher reviews them every 15 minutes. Cold entries
happen here, at sweep latency.

**Active**: after someone engages the bot, the channel ingests fast — a wake
fires 15 seconds after the burst goes quiet, capped at 60 seconds after its
first message — for about 10 minutes, extended by each further engagement,
decaying back to passive by absence. The agent can also switch modes itself
with `set_monitoring_mode`.

@mentions and replies reach the agent in either mode.

## Watch instructions

The watcher is stateless, so anything the agent wants to be woken for beyond
direct engagement and the watcher's own judgment must be written down. The
agent sets TTL'd instructions (`set_watch_instruction`, at most 5 at a time,
up to 24 hours, 1 hour by default), clears them when done, and they persist
per channel across restarts. Expiry queues a notification.

Set one when: someone promises to report back, the agent answered with a
caveat worth checking on, a discussion is unresolved and may need it, or it
deliberately went quiet and wants to resume later.

## When to respond

### Mode 1 — actively participating

The bot is *in* a conversation only when the other side has engaged it: an
@mention, a reply to one of its messages, or a clear follow-up to something
it just said. In this mode it behaves like any participant — answering
follow-ups, asking clarifying questions, keeping the exchange going while the
humans keep directing messages at it. Conversational phrasing is fine here,
because the human opted in.

### Mode 2 — coming in cold

Everything else. A cold entry is a **one-off contribution, not a seat at the
table**:

1. **Only open bids.** Respond only to messages directed at the whole room or
   at the bot. Never respond to a message directed at another specific person —
   a reply to someone else, an @mention of someone else, or a message inside an
   ongoing back-and-forth between two people.
2. **Meaningful contribution, not correction.** The response must add real
   value: an answer to the question asked, concrete help, a pointer nobody else
   gave. Fact-dumping, trivia and "well, actually" corrections fail this bar
   even when accurate.
3. **One-off framing.** Don't ask questions that pull users into an exchange
   with the bot; don't use phrasing that presumes membership in the thread. If
   the humans want more, they'll @ or reply — which flips the bot into mode 1.
4. **The bar is higher than for a human.** A human jumping in with a relevant
   tip is normal channel behavior; the same message from a bot can make people
   feel duped into talking to an AI. "Not out of place for a human" is *below*
   the bar.
5. **No content-free responses.** Greetings-back, emoji waves, "lol" and pure
   acknowledgments are always wrong cold.
6. **Silence is the default.** Most wakes end with no message sent. Silence
   costs nothing; an unwanted interjection costs goodwill.

### Backing off

If anyone tells the bot to stop, asks it to dial it back, or users are getting
upset or aggressive about its participation:

- Stop responding immediately. At most one brief acknowledgment; never an
  argument, never an explanation of its rules.
- Write a back-off note into the watch instructions so the stateless watcher
  honors the quiet period, or switch the channel to passive.
- Direct mentions and replies still deserve answers during a back-off — they
  are explicit invitations.

### Decision checklist per candidate message

1. Is it directed at the bot (mention, reply, follow-up)? → mode 1, respond
   freely.
2. Is it directed at another specific person, or part of a two-person
   exchange? → never respond.
3. Is it an open bid to the room? → respond only with something that
   meaningfully helps (rule 2), phrased as a one-off (rule 3).
4. Is it ambient noise (greeting into the void, emoji, link drop with no
   ask)? → never respond.

## Limits

At most 2 messages per wake, and at most 8 tool calls. A message over 3000
characters is refused at the send tool so the agent rewrites it shorter;
anything over Discord's 2000-character cap is split into two messages.

## What the eval can and cannot test

Replayed fixture days test mode 2 almost exclusively: the humans in a recorded
day never react to a bot that wasn't there, so mode 1 cannot arise organically.
Mode 1 is covered by the synthetic scenarios in `scripts/proactive_eval/scenarios/`,
whose turns bind to the live bot's actual responses.
