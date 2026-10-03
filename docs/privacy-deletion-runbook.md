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
| **Purge (agent)** | Everything the chat bot holds about the person: the guild memory, behavior and personality blocks, pending notes and retained revisions, both agents' working histories and their compaction summaries, the proactive recovery copy and watch instructions, and the external worker's history. The agent does the edit; see step 4. |
| **Delete** | Bytes balances, squad memberships, quest and challenge submissions and quest progress, member activity dates, forum subscriptions, campaign signups, `/help` and `/tldr` records they started, legacy `/scan` records, rate-limit and DM caches, and their site account with its chat, attachments, searches, resources questions, profile, linked logins, push subscriptions and security log rows. |
| **Anonymise** | Rows other people share. Bytes transfers the person sent or received keep their amount and date for the other member, with the person's id and username replaced and the reason cleared. Chat engagements they started lose the starter's id and username. Usage cost rows lose their Discord id and details. |
| **Keep** | Moderation history (`moderation_actions`, the guild's mod-log posts). Billing in anonymised form: usage cost rows with no person linked, and Polar's own order records. A bare receipt that the request was completed. |
| **Not covered by this runbook** | **[PLACEHOLDER: short-lived copies (the 48-hour and 90-day windows). Zech has asked for that retention to be dropped rather than described; the stores and the wording are being confirmed on #71. Fill this in before the runbook is used.]** |

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
UNION ALL SELECT 'bytes_transactions (anonymise)', count(*) FROM bytes_transactions WHERE giver_id = :'did' OR receiver_id = :'did'
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
UNION ALL SELECT 'security_logs', count(*) FROM security_logs WHERE user_identifier = :'did'
-- kept:
UNION ALL SELECT 'moderation_actions (kept)', count(*) FROM moderation_actions WHERE target_user_id = :'did' OR moderator_user_id = :'did'
-- chat bot stores, purged by the agent in step 4; counted to compare after:
UNION ALL SELECT 'chat memory mentions (agent purge)', count(*) FROM chat_agent_guild_memory WHERE content LIKE '%' || :'did' || '%' OR behavior LIKE '%' || :'did' || '%' OR personality LIKE '%' || :'did' || '%'
UNION ALL SELECT 'memory revisions mentioning (agent purge)', count(*) FROM chat_agent_memory_revisions WHERE content LIKE '%' || :'did' || '%'
UNION ALL SELECT 'memory notes mentioning (agent purge)', count(*) FROM chat_agent_memory_notes WHERE content LIKE '%' || :'did' || '%'
UNION ALL SELECT 'proactive histories mentioning (agent purge)', count(*) FROM proactive_agent_histories WHERE history::text LIKE '%' || :'did' || '%'
UNION ALL SELECT 'chat turns mentioning (agent purge)', count(*) FROM chat_agent_turns WHERE triggering_messages::text LIKE '%' || :'did' || '%'
UNION ALL SELECT 'handler runs mentioning (agent purge)', count(*) FROM handler_runs WHERE trigger_context::text LIKE '%' || :'did' || '%';
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

## 4. Purge the chat bot

Run the admin purge for `DID` that #79 adds. It asks the agent to remove
everything tied to the person from its memory blocks, notes and revisions,
forces a compaction of both agents' working histories that leaves them out
and drops the raw history holding their messages, and reaches the external
worker's history. It is safe to run twice. Its report lists anything still
mentioning the id or the person's known names in any store it touched.

**[PLACEHOLDER: the exact command and its report format, filled in when #79
lands.]**

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

## 6. Delete what is keyed by Discord id

One transaction. Compare each `DELETE n` / `UPDATE n` with the dry run before
committing.

```sql
BEGIN;
-- Bytes: anonymise every transfer the person sent or received. The row
-- stays in the other member's history with its amount and date; 'DELETED'
-- stands in for the id the way 'SYSTEM' does for system rewards.
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
DELETE FROM security_logs WHERE user_identifier = :'did';
COMMIT;
```

Expected side effects, all accepted:

- Anonymised transfers keep the other member's balance, totals, history and
  send cooldown exactly as they were. They show as "[deleted user]".
- Squad scoreboards are summed from submissions, so the person's squad loses
  the points they earned.
- Their Discord squad and sudo roles are not touched by this; remove them in
  Discord if they are still in the server and asked for it.

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
   UPDATE account_deletion_requests SET subscription_ids = '[]', error = NULL
    WHERE user_id = :'uid' AND status = 'complete';
   COMMIT;
   ```

3. Finish the receipt note: receipt id, dates, "completed", and the classes
   handled (purged, deleted, anonymised, kept). No id, username,
   counts per person or message text.
4. Reply to the member with the receipt id, and repeat what was kept. Once they have it, you may delete the DM thread on
   your side.

## Retention windows

**[PLACEHOLDER: short-lived copies (the 48-hour and 90-day windows). Zech has asked for that retention to be dropped rather than described; the stores and the wording are being confirmed on #71. Fill this in before the runbook is used.]**
