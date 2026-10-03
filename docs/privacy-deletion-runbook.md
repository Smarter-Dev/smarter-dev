# Handling a data deletion request by hand

Until deletion is automated (#75), a member asks for their data to be deleted
by sending the admin a direct message on Discord, and the admin follows this
runbook. The public promise is the notice at `/privacy`
(`smarter_dev/shared/privacy_notice.md`); this runbook is how it is kept. If
the two disagree, fix whichever is wrong in the same change.

Every step that changes production is run by the admin, by hand. An agent may
help prepare or check a step, but does not run mutations.

## What a request removes and keeps

| | Stores |
| --- | --- |
| **Purge (agent)** | Everything the chat bot holds about the person: the guild memory, behavior and personality blocks, pending notes and retained revisions, both agents' working histories and their compaction summaries, the proactive recovery copy and watch instructions, and the external worker's history. The agent does the edit; see step 4. |
| **Delete** | Bytes balances, squad memberships, quest and challenge submissions and quest progress, member activity dates, forum subscriptions, campaign signups, `/help` and `/tldr` records they started, legacy `/scan` records, rate-limit and DM caches, bot API security log rows whose request named them, and their site account with its chat, attachments, searches, resources questions, profile, linked logins (and their stored Discord tokens) and push subscriptions. |
| **Anonymise** | Rows other people share. Bytes transfers the person sent or received keep their amount and date for the other member, with the person's id and username replaced and the reason cleared. Chat engagements they started lose the starter's id and username. Usage cost rows lose their Discord id and details. Shared audit rows (chat agent turns, handler runs, forum agent responses) have the person's id and names replaced. |
| **Keep** | Moderation history: `moderation_actions` and the bot's posts in the guild's moderation and audit log channels. Anonymised billing: usage cost rows with no person linked (the membership rows are deleted with the account; Polar keeps its payment records under its own terms). A bare receipt that the request was completed. The person's Discord id alone in `chat_bot_blocked_users`, written by the purge in step 4, so the chat bot sees their messages only as `[BLOCKED BY USER]` and does not respond to them. |
| **Not covered yet** | **[PLACEHOLDER: short-lived copies (the 48-hour and 90-day windows). Zech has asked for that retention to be dropped rather than described; the stores and the wording are being confirmed on #71. Fill this in before the runbook is used.]** Queues with no time limit are also open, pending a decision with the same placeholder: the proactive pending list and dead-letter stream, batches the external worker claimed and never acknowledged, and Skrift dead letters left open (which keep handler fire payloads). |

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
3. Reply with the link to the notice and what the request keeps (moderation
   history, anonymised billing, a bare receipt). Ask them to confirm they want to go ahead,
   because deletion cannot be undone. Wait for a yes.
4. Note the names the person goes by on Discord: their username, display
   name and server nickname, as shown on their profile in the server. Step 6
   replaces them in shared audit rows. Keep them only until the request is
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
UNION ALL SELECT 'security_logs', count(*) FROM security_logs WHERE details ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR event_metadata::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)')
UNION ALL SELECT 'chat_agent_turns (anonymise)', count(*) FROM chat_agent_turns WHERE triggering_messages::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR model_messages_delta::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR agent_output::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)')
UNION ALL SELECT 'handler_runs (anonymise)', count(*) FROM handler_runs WHERE trigger_context::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)')
-- kept:
UNION ALL SELECT 'moderation_actions (kept)', count(*) FROM moderation_actions WHERE target_user_id = :'did' OR moderator_user_id = :'did'
-- chat bot stores, purged by the agent in step 4; counted to compare after:
UNION ALL SELECT 'chat memory mentions (agent purge)', count(*) FROM chat_agent_guild_memory WHERE content LIKE '%' || :'did' || '%' OR behavior LIKE '%' || :'did' || '%' OR personality LIKE '%' || :'did' || '%'
UNION ALL SELECT 'memory revisions mentioning (agent purge)', count(*) FROM chat_agent_memory_revisions WHERE content LIKE '%' || :'did' || '%' OR behavior LIKE '%' || :'did' || '%' OR personality LIKE '%' || :'did' || '%'
UNION ALL SELECT 'memory notes mentioning (agent purge)', count(*) FROM chat_agent_memory_notes WHERE content LIKE '%' || :'did' || '%'
UNION ALL SELECT 'proactive histories mentioning (agent purge)', count(*) FROM proactive_agent_histories WHERE history::text LIKE '%' || :'did' || '%';
ROLLBACK;
```

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

Open Admin → Bot → Privacy purge (#79), enter `DID` and the names noted in
step 1, and start the purge. Creating the request writes `DID` to
`chat_bot_blocked_users`, where it stays: from then on the chat bot sees the
person's messages only as `[BLOCKED BY USER]` and does not respond to them.
The agent then removes everything tied to the person from its memory blocks,
notes and revisions, a forced compaction of both agents' working histories
leaves them out and drops the raw history holding their messages, and the
external worker's history is purged too. The page refuses to start unless
both the bot and the worker are enforcing the blocked list. It is safe to run
twice. When it finishes, read its check report, which lists anything still
mentioning the id or the names in any store it touched.

**[PLACEHOLDER: the page's final wording and report format, filled in from
#79 when its PRs are up.]**

Do not go on until the report shows nothing remaining, or every item it lists
has been resolved through the agent (run the purge again with the names it
flags). Never fix a leftover by editing memory or history by hand.

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
-- Bot API calls that named the person in their path.
DELETE FROM security_logs
 WHERE details ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)') OR event_metadata::text ~ ('(^|[^0-9])' || :'did' || '([^0-9]|$)');
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
  Multilingual Plane, which the escape below does not produce.

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
UNION ALL SELECT 'forum responses', count(*) FROM forum_agent_responses WHERE author_display_name = :'name';
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
-- stop here: compare each UPDATE count with its line above, then run COMMIT; or ROLLBACK;
```

The chat turn updates are a safety net: turns are written with member text
already redacted and their triggering messages carry ids, not names, so they
usually change 0 rows. The handler run and forum updates are the ones that
normally match. A name the model repeated in its own words is not matched.

The AI-written text in these rows (replies, topics, notes, forum replies) is
covered by the short-lived copies placeholder above.

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

## 7. Clear Redis caches

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

## 8. Check and close

1. Rerun the dry-run counts. Every deleted store reads 0; the anonymise rows
   read 0; moderation is unchanged; the agent purge rows match the purge
   report.
2. *Site:* strip the deletion job's row down to the receipt:

   ```sql
   BEGIN;
   UPDATE account_deletion_requests
      SET user_id = gen_random_uuid(), subscription_ids = '[]', error = NULL
    WHERE user_id = :'uid' AND status = 'complete';
   -- expect UPDATE 1, then run COMMIT; (anything else: ROLLBACK;)
   ```

3. Finish the receipt note: receipt id, dates, "completed", and the classes
   handled (purged, deleted, anonymised, kept). No id, username,
   counts per person or message text. Discard the names noted in step 1.
4. Reply to the member with the receipt id, and repeat what was kept. Once
   they have it, you may delete the DM thread on your side.

## Retention windows

**[PLACEHOLDER: short-lived copies (the 48-hour and 90-day windows). Zech has asked for that retention to be dropped rather than described; the stores and the wording are being confirmed on #71. Fill this in before the runbook is used.]**
