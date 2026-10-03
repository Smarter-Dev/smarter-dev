# Handling a data deletion request by hand

Until deletion is automated (#75), a member asks for their data to be deleted
by sending the admin a direct message on Discord, and the admin follows this
runbook. The public promise is the notice at `/privacy`
(`smarter_dev/shared/privacy_notice.md`); this runbook is how it is kept. If
the two disagree, fix whichever is wrong in the same change.

Every step that changes production is run by the admin, by hand. An agent may
help prepare or check a step, but does not run mutations.

## What a request removes, keeps and cannot remove yet

| | Stores |
| --- | --- |
| **Delete** | Bytes balances and every transfer the person sent or received, squad memberships, quest and challenge submissions and quest progress, member activity dates, forum subscriptions, campaign signups, `/help` and `/tldr` records they started, legacy `/scan` records, rate-limit and DM caches, and their site account with its chat, attachments, searches, resources questions, profile, linked logins, push subscriptions and security log rows. |
| **Detach** | Audit rows other people share: the person's id and username are replaced on chat engagements they started, and their Discord id and details are cleared from usage cost rows. The rows and their numbers stay. |
| **Keep** | Moderation history (`moderation_actions`, the guild's mod-log posts). Billing in anonymised form: usage cost rows with no person linked, and Polar's own order records. A bare receipt that the request was completed. |
| **Cannot remove yet** | The chat bot's permanent memory, its revisions and notes, the compaction summaries inside both agents' working histories, the proactive agents' raw histories and queues, the external worker's copies, ids inside shared audit JSON (chat turns, handler runs), forum agent rows that name a post's author only by display name, Skrift worker job records, logs, backups, and copies held by AI providers or Discord. |

Do **not** reset, blank or hand-edit the chat bot's memory to satisfy a
request. Only the agent edits its own memory, and the bot never resets it. Do
not delete or rewrite a guild's proactive history or its Redis keys either:
it is the proactive agent's memory for the whole guild, and the external
worker falls back to `proactive:guild-history:{guild}` and to the
`proactive_agent_histories` row, so removing one copy does not remove the
history. These wait for the agent purge (#73, #75).

## 1. Take the request

1. The request must come from the Discord account whose data is to be
   deleted, in a direct message. Discord has authenticated the sender, so the
   account sending the DM is the account you delete. Do not act on a request
   sent on someone else's behalf, by email, or from another account.
2. Copy the sender's user id (Developer Mode → right-click the user → Copy
   User ID). Call it `DID` below. Never look a person up by username, display
   name or email.
3. Reply with the link to the notice and what the request will not remove (the
   rows in the table above). Ask them to confirm they want to go ahead,
   because deletion cannot be undone. Wait for a yes.
4. Start a private note for this request with a random receipt id
   (`uuidgen`), the date received and the date confirmed. The note never holds
   the person's id, username or messages. Everything else you write down while
   working (counts, errors) goes in it as numbers only.

## 2. Find the site account

Run every query in this runbook in `psql` against the production database,
with these variables set:

```sql
SET search_path TO skrift;
\set did '123456789012345678'   -- DID, the sender's Discord user id
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
Discord-only has no Discord link. Its owner can delete it themselves from
Account → Security; do not try to match it to a Discord account.

## 3. Dry run

Run the counts and keep the numbers in the receipt note. Nothing changes.

```sql
BEGIN READ ONLY;
SELECT 'bytes_balances' AS store, count(*) FROM bytes_balances WHERE user_id = :'did'
UNION ALL SELECT 'bytes_transactions', count(*) FROM bytes_transactions WHERE giver_id = :'did' OR receiver_id = :'did'
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
UNION ALL SELECT 'chat_agent_engagements (detach)', count(*) FROM chat_agent_engagements WHERE activation_user_id = :'did'
UNION ALL SELECT 'usage_cost_rows (detach)', count(*) FROM usage_cost_rows WHERE discord_user_id = :'did'
UNION ALL SELECT 'security_logs', count(*) FROM security_logs WHERE user_identifier = :'did'
-- kept:
UNION ALL SELECT 'moderation_actions (kept)', count(*) FROM moderation_actions WHERE target_user_id = :'did' OR moderator_user_id = :'did'
-- cannot remove yet; counted so the reply to the member is accurate:
UNION ALL SELECT 'chat memory mentions (not removed)', count(*) FROM chat_agent_guild_memory WHERE content LIKE '%' || :'did' || '%' OR behavior LIKE '%' || :'did' || '%' OR personality LIKE '%' || :'did' || '%'
UNION ALL SELECT 'memory revisions mentioning (not removed)', count(*) FROM chat_agent_memory_revisions WHERE content LIKE '%' || :'did' || '%'
UNION ALL SELECT 'memory notes mentioning (not removed)', count(*) FROM chat_agent_memory_notes WHERE content LIKE '%' || :'did' || '%'
UNION ALL SELECT 'proactive histories mentioning (not removed)', count(*) FROM proactive_agent_histories WHERE history::text LIKE '%' || :'did' || '%'
UNION ALL SELECT 'chat turns mentioning (not removed)', count(*) FROM chat_agent_turns WHERE triggering_messages::text LIKE '%' || :'did' || '%'
UNION ALL SELECT 'handler runs mentioning (not removed)', count(*) FROM handler_runs WHERE trigger_context::text LIKE '%' || :'did' || '%';
ROLLBACK;
```

A zero for the memory and history rows only means the id is not written
there. The bot may still remember the person by name. Treat those counts as
"at least", never as proof the bot holds nothing.

*Site*, with `uid` set:

```sql
BEGIN READ ONLY;
SELECT 'web_search_runs' AS store, count(*) FROM web_search_runs WHERE owner_user_id = :'uid'
UNION ALL SELECT 'web_search_links', count(*) FROM web_search_links WHERE owner_user_id = :'uid'
UNION ALL SELECT 'push_subscriptions', count(*) FROM push_subscriptions WHERE user_id = :'uid'
UNION ALL SELECT 'security_logs (site)', count(*) FROM security_logs WHERE user_identifier = :'uid'
UNION ALL SELECT 'web_chat_conversations', count(*) FROM web_chat_conversations WHERE owner_user_id = :'uid'
UNION ALL SELECT 'sudo_memberships', count(*) FROM sudo_memberships WHERE user_id = :'uid'
UNION ALL SELECT 'open subscriptions', count(*) FROM sudo_memberships WHERE user_id = :'uid' AND subscription_id IS NOT NULL AND revoked_reason IS NULL
UNION ALL SELECT 'usage_cost_rows (site)', count(*) FROM usage_cost_rows WHERE user_id = :'uid'
UNION ALL SELECT 'work_dispatches mentioning', count(*) FROM work_dispatches WHERE payload::text LIKE '%' || :'uid' || '%';
ROLLBACK;
```

If the person has an open subscription, tell them it will be cancelled
before going on.

## 4. Delete the site account *(site)*

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
   DELETE FROM security_logs WHERE user_identifier = :'uid';
   -- the row counts must equal the dry run; otherwise ROLLBACK and recheck
   COMMIT;
   ```

2. Ask the person to sign in and delete the account themselves from Account →
   Security → Delete account. This also proves they still control it. When
   they cannot sign in, a developer runs the `chat.account.delete` job for
   them; do not delete the `users` row by hand, because that skips the
   attachment and subscription cleanup.
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

## 5. Delete what is keyed by Discord id

One transaction. Compare each `DELETE n` / `UPDATE n` with the dry run before
committing.

```sql
BEGIN;
-- Bytes: delete every transfer the person sent or received. The other
-- member's balance and totals are stored, not summed from this table, so
-- they do not change; the transfer simply leaves their history.
DELETE FROM bytes_transactions WHERE giver_id = :'did' OR receiver_id = :'did';
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
DELETE FROM security_logs WHERE user_identifier = :'did';
COMMIT;
```

Expected side effects, all accepted:

- Another member who sent bytes to this person recently may be able to send
  again sooner, because the send cooldown reads recent transfers.
- Squad scoreboards are summed from submissions, so the person's squad loses
  the points they earned.
- Their Discord squad and sudo roles are not touched by this; remove them in
  Discord if they are still in the server and asked for it.

Do not touch `moderation_actions`, including rows where the person was the
moderator.

## 6. Clear Redis caches

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

## 7. Check and close

1. Rerun the dry-run counts. Every deleted store reads 0; the detach rows
   read 0; moderation and the "not removed" rows are unchanged.
2. *Site:* strip the deletion job's row down to the receipt:

   ```sql
   BEGIN;
   UPDATE account_deletion_requests SET subscription_ids = '[]', error = NULL
    WHERE user_id = :'uid' AND status = 'complete';
   COMMIT;
   ```

3. Finish the receipt note: receipt id, dates, "completed", and the classes
   handled (deleted, detached, kept, not removable yet). No id, username,
   counts per person or message text.
4. Reply to the member with the receipt id, and repeat what was kept and what
   cannot be removed yet. Once they have it, you may delete the DM thread on
   your side.

## Retention windows

Copies that this runbook cannot reach expire on their own: chat agent
working history 2 hours after it goes quiet, AI-written audit text 48 hours,
proactive wake streams 48 hours, Skrift worker job records 7 to 90 days, the
remaining security logs (by IP address only) 90 days. They are left to
expire rather than edited by hand, and the reply to the member says so.
Backups and logs are not edited.
