# Handling a data deletion request by hand

Until deletion is automated (#75), a member asks for their data to be deleted
by sending a direct message on Discord to anyone with the @admin role, and the
admin who receives it follows this runbook. The notice promises the deletion
within 30 days of the request. The public promise is the notice at `/privacy`
(`smarter_dev/shared/privacy_notice.md`); this runbook is how it is kept. If
the two disagree, fix whichever is wrong in the same change.

Every step that changes production is run by the admin, by hand. An agent may
help prepare or check a step, but does not run mutations.

## What a request removes and keeps

| | Stores |
| --- | --- |
| **Purge (agent)** | Everything the chat bot holds about the person: the guild memory, behavior and personality blocks, pending notes and retained revisions, both agents' working histories and their compaction summaries, the proactive recovery copy and watch instructions, and the external worker's history. The agent does the edit; see step 4. |
| **Delete** | Bytes balances, squad memberships, quest and challenge submissions and quest progress, member activity dates, forum subscriptions, campaign signups, `/help` and `/tldr` records they started, legacy `/scan` profiles, rate-limit and DM caches, bot API security log rows whose request named them (until #81's migration drops that table), and their site account with its chat, attachments, searches, resources questions, profile, linked logins (and their stored Discord tokens), push subscriptions, roles, API keys, second-factor enrollments, OAuth consent grants, republish links and membership rows. Also these, which can outlast the limits under "Ages out": AI error messages that name them, the running topic and notes of engagements that name them, blog topic candidates from their conversations or naming them, entries in automation memory that carry them, automation jobs about them still waiting in Skrift's job stores (and any not yet pruned), AI agent sessions that mention them, and their site jobs. |
| **Anonymise** | Rows other people share. Bytes transfers the person sent or received keep their amount and date for the other member, with the person's id and username replaced and the reason cleared. Chat engagements they started lose the starter's id and username. Usage cost rows lose their Discord id and details. Legacy `/scan` usage rows lose their user id. Chat agent turns and handler runs have the person's id and names replaced where they stand as values; forum agent responses have the author's display name replaced. Site page revisions they wrote lose their author when the account is deleted. |
| **Keep** | Moderation history: `moderation_actions` and the bot's posts in the guild's moderation and audit log channels. Anonymised billing: usage cost rows with no person linked (the membership rows are deleted with the account; Polar keeps its payment records under its own terms). A bare receipt that the request was completed. The person's Discord id alone in `chat_bot_blocked_users`, written by the purge in step 4, so the chat bot sees their messages only as `[BLOCKED BY USER]` and does not respond to them. |
| **Ages out** | Short-lived records listed below. Nothing in them lasts past 30 days, so a request does not touch them, apart from the live work steps 6 to 8 clear. |

The Delete and Anonymise rows also cover records that can outlast those
limits, which the steps below edit: AI error messages and engagement topics (step 6), blog
topic candidates (step 6), automation memory (step 7) and job stores
(step 8).

The Keep row has a second part, disclosed in the notice's "What we keep":
creator fields on automations, campaigns and scheduled messages set up by a
requester who is an admin (`created_by` on `channel_handlers`,
`admin_handlers`, `forum_agents`, `campaigns`, `scheduled_messages`,
`squad_sale_events` and `repeating_messages`;
`extension_installs.installed_by`); Pydantic Logfire and server logs; copies
held by AI model providers and the other processors the notice names; the
bot's Discord posts and DMs outside the moderation and audit log channels;
and copies of channel conversations exported to test the bot's AI
(`scripts/proactive_eval/fetch_history.py`, and the historical copies from
#42). Site pages and assets an admin authored are reassigned, not kept (step
5).

### Ages out

These can hold the person's id, name or words for a short time. Each has a
limit, and the longest is 30 days, which the notice states. Do not touch them
for a request. The exception is live work, which has no limit while it lasts:
an engagement still running, an automation job or timer still waiting, an AI
agent session still running or paused. Steps 6 to 8 clear the person's part of
it.

The limits hold once smarter-dev PR 135 and proactive-agent PR 7 (#80) are
deployed and the one-off clean-up in their descriptions has been run:

- **SQL:** the verified script in smarter-dev PR 135's description
  (`gh pr view 135 -R Smarter-Dev/smarter-dev --json body -q .body`), used as
  written; do not retype it. It runs inside `psql` with `\i` and leaves the
  transaction open. It stops with nothing changed if the stored shape is not
  what it expects or no handler-fire job state exists. `COMMIT;` only if every
  "deleted" count equals its count above it and "finished fire rows left" is
  0; otherwise `ROLLBACK;`. On production a zero count means stop and ask a
  developer, not "nothing to clean".
- **Redis:** the pending-list `EXPIRE` loop in PR 135's description, and the
  claimed-batch `EXPIRE` loop and `DEL proactive:v1:dead-letter` in
  proactive-agent PR 7's.

Check that the clean-up was done before taking the first request.

- **Message text in hand-offs:** `handler-fire:context:*` in Redis, the
  verbatim message that set off an automation (1 hour; a fire that finds it
  gone is skipped); the proactive pending lists (each message is dropped once
  it is 48 hours old, on the bot's 15-minute tick, so at most 48 hours 15
  minutes; the list's own expiry is a backstop); claimed proactive batches,
  by the bot or the external worker (48 hours after the claim); the
  proactive wake and shadow streams (trimmed to 48 hours). The external
  worker's dead-letter stream holds ids and an error type only (trimmed to
  48 hours on every write and every 15 minutes).
  `proactive:v1:control-processed*` markers last 7 days and hold only a
  command id. `proactive:v1:control` entries and
  `proactive:v1:{guild:*}:pending-dropped` counters have no limit: the bot
  deletes a control entry once it has processed it, and a wake deletes the
  counter, so one that stays is escalated, not waited out.
- **Cleared by the hourly retention sweep 48 hours after they are written:**
  the bot's replies and working records, which can quote a member (not its
  memory, which step 4 covers):
  `chat_agent_turns.agent_output` and the model's reply text and tool-call
  arguments in turn transcripts (a search query lifted from a message, for
  example), `chat_agent_engagements.last_topic` and `last_notes` (48 hours
  after the engagement's last turn), `forum_agent_responses` replies and
  reasons, the text of `help_conversations` (including other members' names
  in a conversation someone else started), and
  `moderation_actions.ai_context_summary`. Member text is written as
  `[message content]` everywhere else: every `chat_agent_errors` message and
  provider body (its traceback keeps exception types and stack frames only),
  and `handler_runs.error` (the exception type and the script's frames, or
  the cap name; a compile error keeps its message, since the script is
  compiled before it sees any message). Rows written before #80 that still
  hold member text (compaction summaries, error messages and tracebacks,
  `handler_runs.error`, model reasoning in turn transcripts) are cleared by
  the same sweep.
- **Skrift worker tables, pruned by the hourly retention job:** finished,
  dead-lettered and unresumable work 7 days after it was written: job state,
  queue rows, dead letters, events and snapshots. Skrift's own error text for
  handler fire jobs holds exception types and frames only. Job payloads hold
  ids and names, not message text, except a timer whose automation script
  copied text into it: that stays until the timer fires, then up to 7 days.
  Live work is never pruned: a queued or pending job, an AI agent session
  Skrift can still resume, and an unfinished job that belongs to such a
  session or changed in the last 7 days. Step 8 removes the person's part of
  it.
- **Caches:** `search_result_previews` (48 hours),
  `chat_agent:guild:{guild}:events` (about an hour), `mediaread:*` (24 hours),
  `hclaim:*` (up to 30 days; deleting one can make a handler act twice), the
  anonymous web search keys (30 minutes) and in-process caches in the bot and
  workers.

Do **not** reset, blank or hand-edit the chat bot's memory to satisfy a
request. Only the agent edits its own memory, and the bot never resets it. Do
not delete or rewrite a guild's proactive history or its Redis keys either:
it is the proactive agent's memory for the whole guild, and the external
worker falls back to `proactive:guild-history:{guild}` and to the
`proactive_agent_histories` row, so removing one copy does not remove the
history. The agent purge in step 4 is the only way the chat bot's memory and
history change for a request.

## 1. Take the request

1. The request must come from the Discord account whose data is to be
   deleted, in a direct message. Discord has authenticated the sender, so the
   account sending the DM is the account you delete. Do not act on a request
   sent on someone else's behalf, by email, or from another account.
2. Copy the sender's user id (Developer Mode → right-click the user → Copy
   User ID). Call it `DID` below. Never look a person up by username, display
   name or email.
3. Reply with the link to the notice and what the request keeps, matching
   the notice: moderation history, anonymised billing, a bare receipt, and
   their Discord id alone on the chat bot's blocked list, so the chat bot sees
   their messages only as `[BLOCKED BY USER]` and does not respond to them.
   Tell them the blocked list covers the chat bot only: other AI features
   (`/help`, forum replies, server automations, moderation) still process
   their new messages. Ask them to confirm they want to go ahead, because
   deletion cannot be undone. Wait for a yes. The 30 days run from the
   request.
4. Note the names the person goes by on Discord: their username, display
   name and server nickname, as shown on their profile in the server. Steps 4
   and 6 to 8 use them. Keep them only until the request is
   closed.
5. Start a private note for this request with a random receipt id
   (`uuidgen`), the date received and the date confirmed. The note never holds
   the person's id, username or messages. Everything else you write down while
   working (counts, errors) goes in it as numbers only.

## 2. Find the site account

Run every query in this runbook in `psql` against the production database,
with these variables set:

```sql
SET search_path TO skrift;
-- DID, the sender's Discord user id. Keep comments off \set lines: psql
-- reads anything after the value as more of the value.
\set did '123456789012345678'
```

```sql
BEGIN READ ONLY;
SELECT user_id FROM oauth_accounts
 WHERE provider = 'discord' AND provider_account_id = :'did';
ROLLBACK;
```

One row: that is their site account; set `\set uid '<that uuid>'`. No row:
they have no site account linked to this Discord account; skip every step
marked *site*. Two or more rows cannot happen (the pair is unique); stop and
ask a developer if it does.

A site account made with GitHub or Google sign-in before the site went
Discord-only has no Discord link, and its owner cannot sign in any more. Do
not try to match it to a Discord account. If its owner asks, they must prove
they own it (for example, by email from the account's address), and the
account is deleted through the developer path in step 5.

## 3. Dry run

Run the counts and keep the numbers in the receipt note. Nothing changes.

```sql
BEGIN READ ONLY;
SELECT 'bytes_balances' AS store, count(*) FROM bytes_balances WHERE user_id = :'did'
UNION ALL SELECT 'bytes_transactions (delete, no other member)', count(*) FROM bytes_transactions WHERE (giver_id = :'did' AND receiver_id IN ('SYSTEM', 'DELETED')) OR (receiver_id = :'did' AND giver_id IN ('SYSTEM', 'DELETED'))
UNION ALL SELECT 'bytes_transactions (anonymise as giver)', count(*) FROM bytes_transactions WHERE giver_id = :'did' AND receiver_id NOT IN ('SYSTEM', 'DELETED')
UNION ALL SELECT 'bytes_transactions (anonymise as receiver)', count(*) FROM bytes_transactions WHERE receiver_id = :'did' AND giver_id NOT IN ('SYSTEM', 'DELETED')
UNION ALL SELECT 'squad_memberships', count(*) FROM squad_memberships WHERE user_id = :'did'
UNION ALL SELECT 'quest_submissions', count(*) FROM quest_submissions WHERE user_id = :'did'
UNION ALL SELECT 'quest_progress', count(*) FROM quest_progress WHERE user_id = :'did'
UNION ALL SELECT 'challenge_submissions', count(*) FROM challenge_submissions WHERE user_id = :'did'
UNION ALL SELECT 'member_activity', count(*) FROM member_activity WHERE user_id = :'did'
UNION ALL SELECT 'forum_user_subscriptions', count(*) FROM forum_user_subscriptions WHERE user_id = :'did'
UNION ALL SELECT 'campaign_signups', count(*) FROM campaign_signups WHERE discord_id = :'did'
UNION ALL SELECT 'help_conversations', count(*) FROM help_conversations WHERE user_id = :'did'
UNION ALL SELECT 'research_sessions', count(*) FROM research_sessions WHERE user_id = :'did'
UNION ALL SELECT 'scan_user_profiles', count(*) FROM scan_user_profiles WHERE user_id = :'did'
UNION ALL SELECT 'scan_service_usage', count(*) FROM scan_service_usage WHERE user_id = :'did'
UNION ALL SELECT 'chat_agent_engagements (anonymise)', count(*) FROM chat_agent_engagements WHERE activation_user_id = :'did'
UNION ALL SELECT 'usage_cost_rows (anonymise)', count(*) FROM usage_cost_rows WHERE discord_user_id = :'did'
UNION ALL SELECT 'chat_agent_turns (anonymise)', count(*) FROM chat_agent_turns WHERE triggering_messages::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR model_messages_delta::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR agent_output::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)')
UNION ALL SELECT 'handler_runs (anonymise)', count(*) FROM handler_runs WHERE trigger_context::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)')
UNION ALL SELECT 'chat_agent_errors (clear text)', count(*) FROM chat_agent_errors WHERE error_message ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR traceback ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR coalesce(provider_body, '') ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)')
UNION ALL SELECT 'chat_agent_engagements topic (clear)', count(*) FROM chat_agent_engagements WHERE coalesce(last_topic, '') ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR coalesce(last_notes, '') ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)')
UNION ALL SELECT 'candidate_blog_topics', count(*) FROM candidate_blog_topics WHERE engagement_id IN (SELECT id FROM chat_agent_engagements WHERE activation_user_id = :'did') OR (headline || ' ' || observation || ' ' || scope || ' ' || evidence::text) ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)')
-- kept:
UNION ALL SELECT 'moderation_actions (kept)', count(*) FROM moderation_actions WHERE target_user_id = :'did' OR moderator_user_id = :'did'
-- chat bot stores, purged by the agent in step 4; counted to compare after:
UNION ALL SELECT 'chat memory mentions (agent purge)', count(*) FROM chat_agent_guild_memory WHERE content LIKE '%' || :'did' || '%' OR behavior LIKE '%' || :'did' || '%' OR personality LIKE '%' || :'did' || '%'
UNION ALL SELECT 'memory revisions mentioning (agent purge)', count(*) FROM chat_agent_memory_revisions WHERE content LIKE '%' || :'did' || '%' OR behavior LIKE '%' || :'did' || '%' OR personality LIKE '%' || :'did' || '%'
UNION ALL SELECT 'memory notes mentioning (agent purge)', count(*) FROM chat_agent_memory_notes WHERE content LIKE '%' || :'did' || '%'
UNION ALL SELECT 'proactive histories mentioning (agent purge)', count(*) FROM proactive_agent_histories WHERE history::text LIKE '%' || :'did' || '%';
ROLLBACK;
```

Until migration `9c41e07d5b28` (#81) drops `security_logs`, old bot API
security log rows can still name the person in their path; nothing new is
written there. Count them only while the table exists:

```sql
SELECT to_regclass('security_logs') IS NOT NULL AS has_security_logs \gset
\if :has_security_logs
SELECT 'security_logs' AS store, count(*) FROM security_logs WHERE details ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR event_metadata::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)');
\endif
```

Automation memory and the job stores have their own counts in steps 7 and
8.

A zero for the memory and history rows only means the id is not written
there. The bot may still remember the person by name. Treat those counts as
"at least"; the purge's own check in step 4 is the one that counts.

*Site*, with `uid` set:

```sql
BEGIN READ ONLY;
SELECT 'web_search_runs' AS store, count(*) FROM web_search_runs WHERE owner_user_id = :'uid'
UNION ALL SELECT 'web_search_links', count(*) FROM web_search_links WHERE owner_user_id = :'uid'
UNION ALL SELECT 'push_subscriptions', count(*) FROM push_subscriptions WHERE user_id = :'uid'
UNION ALL SELECT 'web_chat_conversations', count(*) FROM web_chat_conversations WHERE owner_user_id = :'uid'
UNION ALL SELECT 'sudo_memberships', count(*) FROM sudo_memberships WHERE user_id = :'uid'
UNION ALL SELECT 'open subscriptions', count(*) FROM sudo_memberships WHERE user_id = :'uid' AND subscription_id IS NOT NULL AND revoked_reason IS NULL
UNION ALL SELECT 'usage_cost_rows (site)', count(*) FROM usage_cost_rows WHERE user_id = :'uid'
UNION ALL SELECT 'work_dispatches mentioning', count(*) FROM work_dispatches WHERE payload::text LIKE '%' || :'uid' || '%';
ROLLBACK;
```

If the person has an open subscription, tell them it will be cancelled
before going on.

## 4. Purge the chat bot

The purge (#79) works only once both runtimes enforce the blocked list.
Before the first request, check that #79 was rolled out in this order: the
web release first (it serves the list), then the bot and the external
worker, in either order. Each refuses to send any Discord message to a model
until it has loaded the list once. A purge run before both enforce it can be
undone within hours, because the next wake reads the person's old messages
back into history.

The page refuses to start until both runtimes report enforcing and a live
purge consumer, but it sees only processes new enough to report. A pod still on an image from before #79
reports nothing, does not enforce the list, and could write the person's
messages back. So before the **first** purge, and again after any rollback of
these deployments, confirm that every pod runs the #79 release and no
rollout is still in progress:

```sh
for d in smarter-dev-bot smarter-dev-proactive-agent smarter-dev-agent-worker smarter-dev-chat-child-worker; do
  kubectl -n smarter-dev rollout status deployment/$d --timeout=10s
done
kubectl -n smarter-dev get pods \
  -l 'app in (smarter-dev-bot,smarter-dev-proactive-agent,smarter-dev-agent-worker,smarter-dev-chat-child-worker)' \
  -o custom-columns='APP:.metadata.labels.app,POD:.metadata.name,DELETING:.metadata.deletionTimestamp,IMAGE:.spec.containers[*].image'
```

Every rollout must report "successfully rolled out". Every pod listed must
show `<none>` under DELETING and, under IMAGE, the tag of a release that
includes #79 (the tag the deploy run for the #79 merge printed, and the
proactive-agent release tag for its PR, or any later one): `zzmmrmn/smarter-dev-bot:<tag>` for the bot,
`zzmmrmn/smarter-dev-proactive-agent:<tag>` for the external worker, and
`zzmmrmn/smarter-dev-website:<tag>` for the agent worker, which runs the
purge itself, and the chat child worker, which runs the chat agent's model
jobs from the same image. If any pod shows an older tag or is
still being deleted, wait and run the check again; do not start a purge.

1. Open Admin → Bot Admin → Privacy Purges (`/admin/bot/privacy-purges`).
   Only administrators can open it.
2. Enter `DID`, press "Look up names", and add every name noted in step 1
   that the lookup did not find.
3. Press "Start purge for `DID`". This writes `DID` to
   `chat_bot_blocked_users`, where it stays: from then on the chat bot sees
   the person's messages only as `[BLOCKED BY USER]` and does not respond to
   them. The chat bot's own model then rewrites the guild memory, behavior
   and personality blocks, that day's notes and the retained past versions
   without them, keeping everything else word for word. Both agents' working
   histories, topics and notes, and the external worker's history with its
   recovery and legacy copies, are folded into new summaries without them
   and the raw messages dropped. Watch instructions are reviewed the same
   way. The runtimes discard the guild's queued wake, pending, batch and
   dead-letter entries; the shadow and control keys and the event log are
   not rewritten, and the check reports whatever is left.
4. Wait while the status moves through `queued`, `waiting_for_runtimes`,
   `purging`, `awaiting_acks`, `checking` and `finishing`. It ends in
   `complete` or `needs_review`. A runtime listed as "behind the block list"
   holds the run in `waiting_for_runtimes` until it catches up; if it never
   does, the run ends `needs_review` with the reason. If the status reads
   `failed` ("stopped by an error: run the purge again"), press "Run the
   purge again".
5. Read the check report. It lists hits by store, location, id-hit count
   and name-hit count, never the text, in four sections:
   - **"Still found where the purge rewrites"** (memory, notes, revisions,
     watch instructions, histories): an id hit is a leftover, so press "Run
     the purge again". A name hit may be another member with the same name:
     judge it from the guild's Chat Memory page and run the purge again if
     it is them. If you judge it to be someone else, do not close on it:
     write the count in the receipt note and ask a developer to decide. If
     the page says a guild's memory step was refused for losing unrelated
     lines, the person may go by a name you did not enter: start a new purge
     with that name added.
   - **"Raw operational copies"** (the proactive wake, pending,
     pending-dropped, batch, dead-letter, shadow, control and
     control-processed keys, and the guild event log): copies the purge does
     not rewrite. Do not edit them; step 10 waits for them to age out.
   - **"Chat audit tables, for the #71 deletion runbook"**
     (`chat_agent_turns`, `chat_agent_engagements`,
     `chat_agent_compaction_events`, `chat_agent_errors`): expected at this
     point. Step 6 and the 48-hour sweep clear them, and step 10 checks
     again.
   - **"Possible remains reported by the runtimes"**: press "Run the purge
     again"; it purges the guilds listed.

   Above the sections, the page may also show:
   - **"[unchecked] Too short to search for (ASCII, under 2 characters)"**:
     the names listed cannot be searched. Press "Start purge for `DID`"
     again with a longer form of each such name added; this reruns the same
     request with the extra names. The short name stays on the request, so
     it can never reach `complete`: step 10 says when it counts as done.
   - **"[failed] The worker's history is tombstoned (half-written)"**: press
     "Run the purge again". The request cannot be closed until the worker
     has rewritten those guilds.

Whatever the status, go on with steps 5 to 9. The request stays open until
step 10 sees `complete`. Any hit in any section, an unchecked name, a
tombstoned history or a runtime still reporting name hits keeps it at
`needs_review`, and a late runtime report can move a `complete` request back
to `needs_review`.

Never fix a leftover by editing memory, history or Redis by hand. The purge
finds the person by id and by the names you give it; something that only
paraphrases them, or names them another way, is found only if the agent
recognises it, so give it every name you know. The check does not search
the other tables steps 6 to 8 clear, Discord, logs and traces, what
providers keep, or a process's memory before it reloads; the page lists
these.

## 5. Delete the site account *(site)*

The site's own account deletion does this part, and it is the one path that
also removes private chat attachments from storage and cancels Polar
subscriptions. It does not touch searches, so remove those first.

1. Delete the search history, which has no link to the account and would be
   left behind:

   ```sql
   BEGIN;
   DELETE FROM web_search_links WHERE owner_user_id = :'uid';
   DELETE FROM web_search_runs WHERE owner_user_id = :'uid';
   DELETE FROM push_subscriptions WHERE user_id = :'uid';
   -- stop here: compare each count with the dry run, then run COMMIT; or ROLLBACK;
   ```

2. Ask the person to sign in and delete the account themselves from Account →
   Security → Delete account. This also proves they still control it.

   When they cannot sign in, a developer queues the same job from a shell
   with the app's environment (a one-off pod from the web image). Do not
   delete the `users` row by hand: that skips the attachment and subscription
   cleanup. The job does nothing without its request row, so create both,
   exactly as the account page does:

   ```python
   import asyncio
   from uuid import UUID
   from sqlalchemy import select
   from skrift.db.models.user import User
   from smarter_dev.shared.database import get_db_session_context
   from smarter_dev.web.chat.dispatch import create_dispatch, dispatch_one
   from smarter_dev.web.models import AccountDeletionRequest

   async def main(uid: UUID) -> None:
       async with get_db_session_context() as session:
           user = await session.scalar(
               select(User).where(User.id == uid).with_for_update()
           )
           deletion = await session.scalar(
               select(AccountDeletionRequest).where(
                   AccountDeletionRequest.user_id == uid
               )
           )
           if deletion is None:
               deletion = AccountDeletionRequest(user_id=uid, status="pending")
               session.add(deletion)
               await session.flush()
           user.is_active = False
           dispatch = await create_dispatch(
               session,
               job_type="chat.account.delete",
               aggregate_id=deletion.id,
               payload={"request_id": str(deletion.id)},
           )
           await session.commit()
       await dispatch_one(dispatch.id)

   asyncio.run(main(UUID("<uid>")))
   ```
3. Check the job finished:

   ```sql
   BEGIN READ ONLY;
   SELECT id, status, finished_at FROM account_deletion_requests WHERE user_id = :'uid';
   SELECT count(*) FROM users WHERE id = :'uid';           -- expect 0
   SELECT count(*) FROM usage_cost_rows WHERE user_id = :'uid';  -- expect 0
   ROLLBACK;
   ```

   `status` must be `complete`. `error` means billing revocation is being
   retried; wait for it rather than finishing the request around it.

If the person authored site pages or uploaded assets (admins only), those
rows reference the account with no delete rule and the job fails on them; ask
a developer to reassign them first.

Deleting the account cascades to its profile, linked logins, roles, API keys,
web chat (conversations, threads, turns, messages, documents, compactions,
subagents, attachments rows), resources questions and spending windows. Its
membership rows go with it; the anonymised usage cost rows stay.

## 6. Delete what is keyed by Discord id

One transaction. The blocks below end without `COMMIT;` on purpose: paste a
block, compare each `DELETE n` / `UPDATE n` psql prints with the dry run, and
only then type `COMMIT;`. Anything unexpected: `ROLLBACK;`.

```sql
BEGIN;
-- Bytes. Rows with no other member on the other side (daily rewards,
-- welcome bonuses, squad fees, or a member already deleted) are only this
-- person's history: delete them. Every other transfer stays in the other
-- member's history with its amount and date; 'DELETED' stands in for the id
-- the way 'SYSTEM' does for system rewards.
DELETE FROM bytes_transactions
 WHERE (giver_id = :'did' AND receiver_id IN ('SYSTEM', 'DELETED'))
    OR (receiver_id = :'did' AND giver_id IN ('SYSTEM', 'DELETED'));
UPDATE bytes_transactions
   SET giver_id = 'DELETED', giver_username = '[deleted user]', reason = NULL
 WHERE giver_id = :'did';
UPDATE bytes_transactions
   SET receiver_id = 'DELETED', receiver_username = '[deleted user]', reason = NULL
 WHERE receiver_id = :'did';
DELETE FROM bytes_balances WHERE user_id = :'did';
DELETE FROM squad_memberships WHERE user_id = :'did';
DELETE FROM quest_submissions WHERE user_id = :'did';
DELETE FROM quest_progress WHERE user_id = :'did';
DELETE FROM challenge_submissions WHERE user_id = :'did';
DELETE FROM member_activity WHERE user_id = :'did';
DELETE FROM forum_user_subscriptions WHERE user_id = :'did';
DELETE FROM campaign_signups WHERE discord_id = :'did';
DELETE FROM help_conversations WHERE user_id = :'did';
DELETE FROM research_sessions WHERE user_id = :'did';
DELETE FROM scan_user_profiles WHERE user_id = :'did';
UPDATE scan_service_usage SET user_id = NULL WHERE user_id = :'did';
-- Records that can name them past the 48-hour sweep. Blog topic candidates go
-- first: they are found through the engagement the person started, which
-- the statements after them anonymise.
DELETE FROM candidate_blog_topics
 WHERE engagement_id IN (SELECT id FROM chat_agent_engagements WHERE activation_user_id = :'did') OR (headline || ' ' || observation || ' ' || scope || ' ' || evidence::text) ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)');
UPDATE chat_agent_errors
   SET error_message = '[removed]', traceback = '[removed]', provider_body = NULL
 WHERE error_message ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR traceback ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR coalesce(provider_body, '') ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)');
UPDATE chat_agent_engagements SET last_topic = NULL, last_notes = NULL
 WHERE coalesce(last_topic, '') ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR coalesce(last_notes, '') ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)');
-- Shared audit rows: keep the row, drop who started it.
UPDATE chat_agent_engagements
   SET activation_user_id = '0', activation_username = '[deleted user]',
       activation_message_id = '0'
 WHERE activation_user_id = :'did';
UPDATE usage_cost_rows SET discord_user_id = NULL, details = '{}'
 WHERE discord_user_id = :'did';
-- Shared audit JSON: replace the id wherever it stands alone as a number.
UPDATE chat_agent_turns SET
   triggering_messages = regexp_replace(triggering_messages::text, '(?<![0-9])' || :'did' || '(?![0-9])', '0', 'g')::json,
   model_messages_delta = regexp_replace(model_messages_delta::text, '(?<![0-9])' || :'did' || '(?![0-9])', '0', 'g')::json,
   agent_output = regexp_replace(agent_output::text, '(?<![0-9])' || :'did' || '(?![0-9])', '0', 'g')::json
 WHERE triggering_messages::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)')
    OR model_messages_delta::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)')
    OR agent_output::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)');
UPDATE handler_runs SET
   trigger_context = regexp_replace(trigger_context::text, '(?<![0-9])' || :'did' || '(?![0-9])', '0', 'g')::json
 WHERE trigger_context::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)');
-- Bot API calls that named the person in their path, written before #81
-- stopped per-request logging. Skipped once #81's migration has dropped the
-- table.
SELECT to_regclass('security_logs') IS NOT NULL AS has_security_logs \gset
\if :has_security_logs
DELETE FROM security_logs
 WHERE details ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR event_metadata::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)');
\endif
-- stop here: compare each count with the dry run, then run COMMIT; or ROLLBACK;
```

Then, for **each** name noted in step 1 (username, display name, server
nickname), set it and replace it where shared audit rows record it. The first
query turns the name into the form the app's JSON holds (non-ASCII characters
as `\u` escapes) and prints how many rows each update will touch. Stop and ask
a developer when:

- the forum count is higher than the number of forum posts the person made,
  because another member shares that display name;
- the name is a common word (`send`, `content`, `kind`), which can also be a
  JSON key or an ordinary value;
- the name contains an emoji or another character outside the Basic
  Multilingual Plane, which the escape below does not produce;
- the errors, engagement topics or blog topics count is higher than you
  expect. Those are matched by the name anywhere in their text, ignoring
  case, so a short name can match ordinary words.

```sql
\set name 'their_username'
SELECT string_agg(CASE WHEN ascii(c) < 128 THEN c
                       ELSE '\u' || lpad(to_hex(ascii(c)), 4, '0') END, '' ORDER BY n) AS jq
  FROM regexp_split_to_table(to_json(:'name'::text)::text, '') WITH ORDINALITY AS t(c, n) \gset
SELECT substr(:'jq', 2, length(:'jq') - 2) AS jin \gset
BEGIN READ ONLY;
SELECT 'turns: name as a value' AS what, count(*) FROM chat_agent_turns WHERE strpos(triggering_messages::text, :'jq') > 0
UNION ALL SELECT 'turns: name in prompt text', count(*) FROM chat_agent_turns
 WHERE strpos(coalesce(model_messages_delta::text, ''), 'username=\"' || :'jin' || '\"') > 0
    OR strpos(coalesce(model_messages_delta::text, ''), 'nickname=\"' || :'jin' || '\"') > 0
UNION ALL SELECT 'handler runs', count(*) FROM handler_runs WHERE strpos(trigger_context::text, :'jq') > 0
UNION ALL SELECT 'forum responses', count(*) FROM forum_agent_responses WHERE author_display_name = :'name'
UNION ALL SELECT 'errors: name in text', count(*) FROM chat_agent_errors WHERE (strpos(lower(error_message), lower(:'name')) > 0 OR strpos(error_message, :'jin') > 0) OR (strpos(lower(traceback), lower(:'name')) > 0 OR strpos(traceback, :'jin') > 0) OR (strpos(lower(coalesce(provider_body, '')), lower(:'name')) > 0 OR strpos(coalesce(provider_body, ''), :'jin') > 0)
UNION ALL SELECT 'engagement topics: name', count(*) FROM chat_agent_engagements WHERE (strpos(lower(coalesce(last_topic, '')), lower(:'name')) > 0 OR strpos(coalesce(last_topic, ''), :'jin') > 0) OR (strpos(lower(coalesce(last_notes, '')), lower(:'name')) > 0 OR strpos(coalesce(last_notes, ''), :'jin') > 0)
UNION ALL SELECT 'blog topics: name', count(*) FROM candidate_blog_topics WHERE (strpos(lower(headline || ' ' || observation || ' ' || scope), lower(:'name')) > 0 OR strpos(headline || ' ' || observation || ' ' || scope, :'jin') > 0);
ROLLBACK;
BEGIN;
UPDATE chat_agent_turns
   SET triggering_messages = replace(triggering_messages::text, :'jq', '"[deleted user]"')::json
 WHERE strpos(triggering_messages::text, :'jq') > 0;
UPDATE chat_agent_turns
   SET model_messages_delta = replace(replace(model_messages_delta::text,
         'username=\"' || :'jin' || '\"', 'username=\"[deleted user]\"'),
         'nickname=\"' || :'jin' || '\"', 'nickname=\"[deleted user]\"')::json
 WHERE strpos(coalesce(model_messages_delta::text, ''), 'username=\"' || :'jin' || '\"') > 0
    OR strpos(coalesce(model_messages_delta::text, ''), 'nickname=\"' || :'jin' || '\"') > 0;
UPDATE handler_runs
   SET trigger_context = replace(trigger_context::text, :'jq', '"[deleted user]"')::json
 WHERE strpos(trigger_context::text, :'jq') > 0;
UPDATE forum_agent_responses SET author_display_name = '[deleted user]'
 WHERE author_display_name = :'name';
UPDATE chat_agent_errors
   SET error_message = '[removed]', traceback = '[removed]', provider_body = NULL
 WHERE (strpos(lower(error_message), lower(:'name')) > 0 OR strpos(error_message, :'jin') > 0) OR (strpos(lower(traceback), lower(:'name')) > 0 OR strpos(traceback, :'jin') > 0) OR (strpos(lower(coalesce(provider_body, '')), lower(:'name')) > 0 OR strpos(coalesce(provider_body, ''), :'jin') > 0);
UPDATE chat_agent_engagements SET last_topic = NULL, last_notes = NULL
 WHERE (strpos(lower(coalesce(last_topic, '')), lower(:'name')) > 0 OR strpos(coalesce(last_topic, ''), :'jin') > 0) OR (strpos(lower(coalesce(last_notes, '')), lower(:'name')) > 0 OR strpos(coalesce(last_notes, ''), :'jin') > 0);
DELETE FROM candidate_blog_topics
 WHERE (strpos(lower(headline || ' ' || observation || ' ' || scope), lower(:'name')) > 0 OR strpos(headline || ' ' || observation || ' ' || scope, :'jin') > 0);
-- stop here: compare each UPDATE and DELETE count with its line above, then run COMMIT; or ROLLBACK;
```

The chat turn updates are a safety net: turns are written with member text
already redacted and their triggering messages carry ids, not names, so they
usually change 0 rows. The handler run and forum updates are the ones that
normally match. A name the model repeated in its own words is not matched.

The AI-written text in these rows (replies, forum replies) is cleared by the
48-hour sweep; see "Ages out" at the top.

Expected side effects, all accepted:

- Anonymised transfers keep the other member's balance, totals, history and
  send cooldown exactly as they were. They show as "[deleted user]".
- Squad scoreboards are summed from submissions, so the person's squad loses
  the points they earned.
- Their Discord squad and sudo roles are not touched by this; remove them in
  Discord if they are still in the server and asked for it.
- If they stay in the server, the bot starts new bytes and activity rows the
  next time they post. Tell them so when you reply.

Do not touch `moderation_actions`, including rows where the person was the
moderator.

## 7. Remove them from automation memory

Server automations (handlers) keep their own memory: a JSON object per
handler (`memory` on `channel_handlers` and `admin_handlers`) and shared keys
per guild (`guild_handler_memory`). This is automation state, not the chat
bot's memory, and the admin may edit it. The bundled automations keep member
ids there: the DM relay maps members to their forum posts and lists members
it has warned, and the Disboard tracker lists bumps and the current bump
king. Member-written automations can keep anything.

The edit removes whole entries, never parts of one: a top-level key whose
value is the person's id or name, a map entry keyed by them or whose value
mentions them, and a list element that mentions them. A map stays a map and a
list stays a list of the same shape, so the automation reads what it always
did. A removed key reads as never set. Expected effects:

- The DM relay opens a new forum post the next time the person sends the
  bot a DM; a staff reply in their old post is refused with "No member is
  mapped".
- If the person was the Disboard bump king, the tracker no longer knows it
  and does not take the role away: remove it from them in Discord.
- The DM relay no longer knows it warned them, so it shows them its
  "Heads up" notice again the next time they DM the bot.
- A map entry or list element that mentions the person goes whole, including
  anything about other members kept in that same entry.

Pausing has effects too, so keep it short (usually a minute or two). It pauses
every automation whose memory holds the person and, when a guild key does,
every admin automation in that guild, moderation automations included. While
paused, events for them are not handled (a member's DM through the relay is
dropped, not queued), a one-shot timer that comes due is lost (such as the
Disboard two-hour reminder), and a recurring schedule skips until the
handler sweep re-arms it, one period plus 15 minutes later.

Run this step and step 8 in **one** `psql` session: they share the
temporary table and functions set up here, which vanish when the session
ends. The setup starts by dropping its own leftovers, so it is safe to run
again, and you must run it again if you reconnect. In a fresh session those
drops print `NOTICE: schema "pg_temp" does not exist, skipping`; that is
expected.

Setup. Add one `INSERT` per name noted in step 1:

```sql
DROP FUNCTION IF EXISTS pg_temp.scrub_memory(json), pg_temp.scrub(json),
  pg_temp.hit(text), pg_temp.mentions(text), pg_temp.mentions_json(jsonb);
DROP TABLE IF EXISTS pg_temp.terms;
CREATE TEMP TABLE terms (did text, jq text, name text);
INSERT INTO terms VALUES (:'did', NULL, NULL);
\set name 'their_username'
INSERT INTO terms VALUES (NULL, to_json(:'name'::text)::text, :'name');
-- repeat the two lines above for each name

-- Whether a piece of JSON text carries the person: their id as a whole
-- number, or one of their names as a whole JSON string.
CREATE FUNCTION pg_temp.hit(t text) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM pg_temp.terms
                  WHERE coalesce(t ~ ('(^|[^0-9])' || did || '([^0-9]|$)'), false)
                     OR coalesce(strpos(t, jq) > 0, false))
$$;

-- Whether free text mentions the person: their id as a whole number, or one
-- of their names as a whole word, ignoring case. Used for run errors and AI
-- agent sessions in step 8, where names stand inside sentences.
CREATE FUNCTION pg_temp.mentions(t text) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM pg_temp.terms
                  WHERE coalesce(t ~ ('(^|[^0-9])' || did || '([^0-9]|$)'), false)
                     OR coalesce(lower(t) ~ ('(^|[^[:alnum:]_])'
                          || regexp_replace(lower(name), '([^[:alnum:][:space:]])', '\\\1', 'g')
                          || '($|[^[:alnum:]_])'), false))
$$;

-- The same over every key, string and number in a JSON document, read with
-- its escapes undone, so a name after a newline or quote is still found.
CREATE FUNCTION pg_temp.mentions_json(j jsonb) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM jsonb_path_query(j, 'strict $.**') AS v
                  WHERE jsonb_typeof(v) IN ('string', 'number')
                    AND pg_temp.mentions(v #>> '{}'))
      OR EXISTS (SELECT 1 FROM jsonb_path_query(j, 'strict $.**') AS v,
                               jsonb_object_keys(CASE WHEN jsonb_typeof(v) = 'object' THEN v ELSE '{}'::jsonb END) AS k
                  WHERE pg_temp.mentions(k))
$$;

-- A map without the entries that carry the person, or a list without the
-- elements that do. Anything else comes back unchanged. Order is kept: the
-- DM relay prunes its map oldest-first.
CREATE FUNCTION pg_temp.scrub(v json) RETURNS json LANGUAGE plpgsql STABLE AS $$
BEGIN
  IF json_typeof(v) = 'object' THEN
    RETURN (SELECT coalesce(json_object_agg(e.k, e.v ORDER BY e.o), '{}'::json)
              FROM json_each(v) WITH ORDINALITY AS e(k, v, o)
             WHERE NOT pg_temp.hit(to_jsonb(e.k)::text)
               AND NOT pg_temp.hit(e.v::jsonb::text));
  ELSIF json_typeof(v) = 'array' THEN
    RETURN (SELECT coalesce(json_agg(e.v ORDER BY e.o), '[]'::json)
              FROM json_array_elements(v) WITH ORDINALITY AS e(v, o)
             WHERE NOT pg_temp.hit(e.v::jsonb::text));
  END IF;
  RETURN v;
END $$;

-- A handler's memory object: drop keys that are the person or whose plain
-- value carries them, and scrub each map or list value.
CREATE FUNCTION pg_temp.scrub_memory(m json) RETURNS json LANGUAGE sql STABLE AS $$
  SELECT coalesce(json_object_agg(e.k, pg_temp.scrub(e.v) ORDER BY e.o), '{}'::json)
    FROM json_each(m) WITH ORDINALITY AS e(k, v, o)
   WHERE NOT pg_temp.hit(to_jsonb(e.k)::text)
     AND NOT (json_typeof(e.v) NOT IN ('object', 'array') AND pg_temp.hit(e.v::jsonb::text))
$$;
```

Dry run. It lists where the person appears, by handler and key:

```sql
BEGIN READ ONLY;
SELECT 'channel handler' AS memory, id::text AS handler_or_guild, NULL AS key FROM channel_handlers WHERE pg_temp.hit(memory::jsonb::text)
UNION ALL SELECT 'admin handler', id::text, NULL FROM admin_handlers WHERE pg_temp.hit(memory::jsonb::text)
UNION ALL SELECT 'guild', guild_id, key FROM guild_handler_memory WHERE pg_temp.hit(to_jsonb(key)::text) OR pg_temp.hit(value::jsonb::text);
ROLLBACK;
```

No rows: go to step 8. Otherwise pause the automations involved, so a run in
progress cannot write its old copy of the memory back over the edit:

```sql
BEGIN;
DROP TABLE IF EXISTS pg_temp.paused;
CREATE TEMP TABLE paused AS
  SELECT 'channel' AS tier, id FROM channel_handlers
   WHERE enabled AND pg_temp.hit(memory::jsonb::text)
  UNION ALL
  SELECT 'admin', id FROM admin_handlers
   WHERE enabled AND (pg_temp.hit(memory::jsonb::text)
     OR guild_id IN (SELECT guild_id FROM guild_handler_memory
                      WHERE pg_temp.hit(to_jsonb(key)::text) OR pg_temp.hit(value::jsonb::text)));
UPDATE channel_handlers SET enabled = false WHERE id IN (SELECT id FROM paused WHERE tier = 'channel');
UPDATE admin_handlers SET enabled = false WHERE id IN (SELECT id FROM paused WHERE tier = 'admin');
COMMIT;
```

Wait until no run of a paused automation is in progress. Repeat this until it
prints 0; it is usually 0 within a minute:

```sql
SELECT count(*) FROM worker_queue
 WHERE claim_token IS NOT NULL
   AND job::jsonb ->> 'type' IN ('handlers.fire', 'admin_handlers.fire')
   AND coalesce(job::jsonb #>> '{payload,handler_id}', job::jsonb #>> '{payload,admin_handler_id}')
       IN (SELECT id::text FROM paused);
```

Then edit and resume in one transaction:

```sql
BEGIN;
UPDATE channel_handlers SET memory = pg_temp.scrub_memory(memory)
 WHERE pg_temp.hit(memory::jsonb::text);
UPDATE admin_handlers SET memory = pg_temp.scrub_memory(memory)
 WHERE pg_temp.hit(memory::jsonb::text);
DELETE FROM guild_handler_memory
 WHERE pg_temp.hit(to_jsonb(key)::text)
    OR (json_typeof(value) NOT IN ('object', 'array') AND pg_temp.hit(value::jsonb::text));
UPDATE guild_handler_memory SET value = pg_temp.scrub(value), updated_at = now()
 WHERE pg_temp.hit(value::jsonb::text);
-- each of these must print 0:
SELECT count(*) FROM channel_handlers WHERE pg_temp.hit(memory::jsonb::text);
SELECT count(*) FROM admin_handlers WHERE pg_temp.hit(memory::jsonb::text);
SELECT count(*) FROM guild_handler_memory WHERE pg_temp.hit(to_jsonb(key)::text) OR pg_temp.hit(value::jsonb::text);
UPDATE channel_handlers SET enabled = true WHERE id IN (SELECT id FROM paused WHERE tier = 'channel');
UPDATE admin_handlers SET enabled = true WHERE id IN (SELECT id FROM paused WHERE tier = 'admin');
-- stop here: compare the counts with the dry run rows and check the three
-- zeros, then run COMMIT; or ROLLBACK; (after a ROLLBACK the automations are
-- still paused: resume them with the last two UPDATEs on their own)
```

Names are matched only as whole values (`"their_username"`). A name inside a
longer text an automation wrote is not matched; the dry run in step 10 counts
those for you to judge.

## 8. Remove them from job stores

Skrift's job tables keep each automation run's trigger: who wrote the message
or joined, their names and the moderation target. The hourly retention job
deletes finished, dead-lettered and unresumable work 7 days after it was
written, but live work has no limit: a job still waiting, such as a timer,
and an AI agent session Skrift can still resume. This step removes the
person's part of it, and does not wait 7 days for the rest.
Use the same `psql` session as step 7.

Deleting a waiting job cancels it. That includes an automation's timer or
recurring fire about the person that is already due; one due later is left
for a developer, because deleting it stops a recurring schedule for good.

Dry run:

```sql
BEGIN READ ONLY;
SELECT 'queue: waiting or dead-lettered (delete)' AS store, count(*) FROM worker_queue
 WHERE job::jsonb ->> 'type' IN ('handlers.fire', 'admin_handlers.fire')
   AND claim_token IS NULL AND (dead_lettered OR visible_at <= now())
   AND pg_temp.hit((job::jsonb -> 'payload')::text)
UNION ALL SELECT 'queue: running now (wait, then rerun)', count(*) FROM worker_queue
 WHERE job::jsonb ->> 'type' IN ('handlers.fire', 'admin_handlers.fire')
   AND claim_token IS NOT NULL AND pg_temp.hit((job::jsonb -> 'payload')::text)
UNION ALL SELECT 'queue: timers due later (ask a developer)', count(*) FROM worker_queue
 WHERE job::jsonb ->> 'type' IN ('handlers.fire', 'admin_handlers.fire')
   AND claim_token IS NULL AND NOT dead_lettered AND visible_at > now()
   AND pg_temp.hit((job::jsonb -> 'payload')::text)
UNION ALL SELECT 'dead letters (delete)', count(*) FROM worker_dead_letters WHERE pg_temp.hit(entry::jsonb::text)
UNION ALL SELECT 'job state (delete)', count(*) FROM worker_state
 WHERE key LIKE 'workers:jobs:%' AND pg_temp.hit(value::jsonb::text)
UNION ALL SELECT 'run errors (clear)', count(*) FROM worker_events
 WHERE stream = 'workers:lifecycle' AND pg_temp.mentions(event::jsonb ->> 'error')
UNION ALL SELECT 'archive events, all (expect 0)', count(*) FROM worker_archive_events
UNION ALL SELECT 'webhook deliveries, all (expect 0)', count(*) FROM webhook_deliveries;
ROLLBACK;
```

Running jobs finish within minutes; rerun the dry run until that row is 0. A
timer due later is an automation's scheduled follow-up; deleting it cancels
it, and if it belongs to a recurring schedule the schedule stops, so a
developer decides. The archive and webhook tables are not used by this site:
if either count is not 0, stop and ask a developer.

```sql
BEGIN;
CREATE TEMP TABLE gone_jobs ON COMMIT DROP AS
  SELECT job_id FROM worker_queue
   WHERE job::jsonb ->> 'type' IN ('handlers.fire', 'admin_handlers.fire')
     AND claim_token IS NULL AND (dead_lettered OR visible_at <= now())
     AND pg_temp.hit((job::jsonb -> 'payload')::text);
DELETE FROM worker_queue WHERE job_id IN (SELECT job_id FROM gone_jobs) AND claim_token IS NULL;
DELETE FROM worker_dead_letters WHERE pg_temp.hit(entry::jsonb::text);
DELETE FROM worker_state
 WHERE key LIKE 'workers:jobs:%'
   AND (substr(key, 14) IN (SELECT job_id FROM gone_jobs) OR pg_temp.hit(value::jsonb::text))
   AND substr(key, 14) NOT IN (SELECT job_id FROM worker_queue);
UPDATE worker_events SET event = jsonb_set(event::jsonb, '{error}', '"[removed]"')::json
 WHERE stream = 'workers:lifecycle' AND pg_temp.mentions(event::jsonb ->> 'error');
-- stop here: compare each count with the dry run (job state may be lower: the
-- state of a job still in the queue, such as a timer due later, stays with its
-- job), then run COMMIT; or ROLLBACK;
```

*Site*, with `uid` set and after step 5 has finished: the site's own jobs
name the account by `uid`.

```sql
BEGIN READ ONLY;
SELECT 'work_dispatches' AS store, count(*) FROM work_dispatches WHERE strpos(payload::text, :'uid') > 0
UNION ALL SELECT 'queue, waiting or dead-lettered', count(*) FROM worker_queue WHERE claim_token IS NULL AND strpos(job::text, :'uid') > 0
UNION ALL SELECT 'dead letters', count(*) FROM worker_dead_letters WHERE strpos(entry::text, :'uid') > 0
UNION ALL SELECT 'job state', count(*) FROM worker_state WHERE key LIKE 'workers:jobs:%' AND strpos(value::text, :'uid') > 0
   AND substr(key, 14) NOT IN (SELECT job_id FROM worker_queue WHERE claim_token IS NOT NULL);
ROLLBACK;

BEGIN;
DELETE FROM work_dispatches WHERE strpos(payload::text, :'uid') > 0;
DELETE FROM worker_queue WHERE claim_token IS NULL AND strpos(job::text, :'uid') > 0;
DELETE FROM worker_dead_letters WHERE strpos(entry::text, :'uid') > 0;
DELETE FROM worker_state WHERE key LIKE 'workers:jobs:%' AND strpos(value::text, :'uid') > 0
   AND substr(key, 14) NOT IN (SELECT job_id FROM worker_queue);
-- stop here: compare each count with the dry run, then run COMMIT; or ROLLBACK;
```

**AI agent sessions.** The site's AI agents (Resources questions, chat
titles, the blogging pipeline) keep each session's messages in `worker_state`
(`runstate:<session>`), `worker_archive_snapshots` and the event stream
`agents:run:<session>`. A session goes whole if it mentions the person (the
id, or a name anywhere, ignoring case) or, for the site account, holds `uid`.
The blogging pipeline copies blog topic candidates into its sessions, so this
is where copies of the candidates deleted in step 6 go. Do not run this while
a Resources question or a blogging run that involves them is still in
progress.

Collect the sessions. Run the last three `INSERT`s only with `uid` set:

```sql
DROP TABLE IF EXISTS pg_temp.sessions;
CREATE TEMP TABLE sessions (session_id text PRIMARY KEY);
INSERT INTO sessions SELECT substr(key, 10) FROM worker_state
 WHERE key LIKE 'runstate:%' AND pg_temp.mentions_json(value::jsonb) ON CONFLICT DO NOTHING;
INSERT INTO sessions SELECT DISTINCT substr(key, 10) FROM worker_archive_snapshots
 WHERE key LIKE 'runstate:%' AND pg_temp.mentions_json(value::jsonb) ON CONFLICT DO NOTHING;
INSERT INTO sessions SELECT DISTINCT substr(stream, 12) FROM worker_events
 WHERE stream LIKE 'agents:run:%' AND pg_temp.mentions_json(event::jsonb) ON CONFLICT DO NOTHING;
-- site only:
INSERT INTO sessions SELECT substr(key, 10) FROM worker_state
 WHERE key LIKE 'runstate:%' AND strpos(value::text, :'uid') > 0 ON CONFLICT DO NOTHING;
INSERT INTO sessions SELECT DISTINCT substr(key, 10) FROM worker_archive_snapshots
 WHERE key LIKE 'runstate:%' AND strpos(value::text, :'uid') > 0 ON CONFLICT DO NOTHING;
INSERT INTO sessions SELECT DISTINCT substr(stream, 12) FROM worker_events
 WHERE stream LIKE 'agents:run:%' AND strpos(event::text, :'uid') > 0 ON CONFLICT DO NOTHING;
```

Dry run, then delete:

```sql
BEGIN READ ONLY;
SELECT 'sessions' AS store, count(*) FROM sessions
UNION ALL SELECT 'session events', count(*) FROM worker_events WHERE stream IN (SELECT 'agents:run:' || session_id FROM sessions)
UNION ALL SELECT 'session state', count(*) FROM worker_state WHERE key IN (SELECT 'runstate:' || session_id FROM sessions)
UNION ALL SELECT 'session snapshots', count(*) FROM worker_archive_snapshots WHERE key IN (SELECT 'runstate:' || session_id FROM sessions);
ROLLBACK;

BEGIN;
DELETE FROM worker_events WHERE stream IN (SELECT 'agents:run:' || session_id FROM sessions);
DELETE FROM worker_state WHERE key IN (SELECT 'runstate:' || session_id FROM sessions);
DELETE FROM worker_archive_snapshots WHERE key IN (SELECT 'runstate:' || session_id FROM sessions);
-- stop here: compare each count with the dry run, then run COMMIT; or ROLLBACK;
```

A name is matched as a whole word, so a name that is also a common word
(`user`, `content`, `agents`) matches sessions that have nothing to do with
the person. Before deleting, compare the session count with how many
sessions you would expect (the person's Resources questions and blog runs
that drew on their conversations); if it is much higher, stop and ask a
developer.

## 9. Clear Redis caches

These expire on their own within hours to days; delete them so nothing
waits on a clock. With `redis-cli` against the bot's Redis:

```
DEL chatlimit:<DID> chatlimit-notice:<DID> hcap:dmuser:<DID> hdm:chan:<DID>
SCAN 0 MATCH chatlimit-warning:<DID>:* COUNT 1000   # then DEL each key found
SCAN 0 MATCH hcap:dmtrig:*:<DID> COUNT 1000         # then DEL each key found
```

Repeat each `SCAN` from the cursor it returns until it returns `0`. Do not
touch `chat_agent:*`, `proactive:*` or `chat_agent:guild:*` keys (see the top
of this runbook).

## 10. Check and close

Close the request only when, after the check below, the purge page shows
`complete`: every hit list then reads "Nothing found" and "Possible remains
reported by the runtimes" reads "None.". The one exception is a request held
only by an unchecked name (point 1). Otherwise `needs_review` is not done,
and neither is `closed` on its own: a closed purge is only a receipt. Until then, points 3 to 6 are not done and the
member is not told it is complete. If it is still not done as the 30 days
run out, tell the member what is left and that the request is open.

1. On the request's Privacy Purge page, press "Run the check again". It
   runs only the search, with no model calls, and sets the status from what
   it finds. Read the report:
   - `complete`: go on.
   - **"Chat audit tables"**: rerun the step 3 counts (point 2) and, for
     each name, step 6's read-only name counts. If they find rows, clear
     them the step 6 way and run the check again. If they read 0, the hit is
     in text the 48-hour sweep clears (the bot's own replies in turns,
     compaction summaries): wait until those rows are 48 hours old and run
     the check again. A name hit in a column neither clears, such as another
     member's `activation_username`, is a namesake: note the count and ask a
     developer to decide.
   - **"Raw operational copies"**: wait for them to age out (the limits are
     under "Ages out") and run the check again. Do not close on them. If one
     is still there after its limit, or its key has no limit listed there,
     ask a developer.
   - **"Still found where the purge rewrites"**, **"Possible remains
     reported by the runtimes"**, an unchecked name or a tombstoned history:
     handle it as in step 4, point 5.
   - **Only an unchecked name is left**: if the page reads `needs_review`
     but every hit list reads "Nothing found", "Possible remains" reads
     "None.", no history is tombstoned, no step shows `failed` or
     `unresolved`, and the purge already ran with a longer form of each
     unchecked name, treat it as `complete`. Write in the receipt note that
     the short name went unchecked.

2. Rerun the dry-run counts of steps 3, 7 and 8 (for agent sessions, rerun
   the collect block first, or the count shows the old list). Every deleted store reads
   0; the anonymise rows read 0; moderation is unchanged; the agent purge rows
   match the purge page. Then, for each name (`\set name` as in step 6),
   count it inside longer text in automation memory. Judge any hit by reading
   the memory: the handler's admin page shows it, and a guild key is read
   with `SELECT key, value FROM guild_handler_memory WHERE …`. Remove a real
   one the step 7 way, with a developer:

   ```sql
   SELECT count(*) FROM (
     SELECT memory::jsonb::text AS t FROM channel_handlers
     UNION ALL SELECT memory::jsonb::text FROM admin_handlers
     UNION ALL SELECT value::jsonb::text FROM guild_handler_memory) m
    WHERE strpos(lower(t), lower(:'name')) > 0;
   ```
3. *Site:* strip the deletion job's row down to the receipt:

   ```sql
   BEGIN;
   UPDATE account_deletion_requests
      SET user_id = gen_random_uuid(), subscription_ids = '[]', error = NULL
    WHERE user_id = :'uid' AND status = 'complete';
   -- expect UPDATE 1, then run COMMIT; (anything else: ROLLBACK;)
   ```

4. Press "Close to a receipt" and confirm. This strips the request to a
   bare receipt (times, outcome, counts); the blocked-list entry stays. The
   page lets you close from any status, so check first that it reads
   `complete` (or that point 1's unchecked-name case applies).
   If it refuses with "Not closed: N guild(s) still have a tombstoned
   history", press "Run the purge again" and close once the worker has
   rewritten them.
5. Finish the receipt note: receipt id, dates, "completed", and the classes
   handled (purged, deleted, anonymised, kept). No id, username,
   counts per person or message text. Discard the names noted in step 1.
6. Reply to the member with the receipt id, and repeat what was kept. Once
   they have it, you may delete the DM thread on your side.

## Retention windows

The limits a request relies on are under "Ages out" at the top. The full list,
with what enforces each one, is `docs/data-retention.md`.
