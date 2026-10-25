Snapshot of production admin_handlers.script, 2026-10-25, SELECT-only read.

One file per live admin handler, named `live-<handler name>.monty`. The read ran
inside `BEGIN READ ONLY` and printed each row with psql's unaligned format, so
every file ends with one extra newline (psql's record separator). The read's
closing `ROLLBACK` command tag, which psql printed after the last row, was
removed from `live-server-birthday-announcement.monty`; nothing else was changed.

The rows' trigger, channel scope and settings at the time of the read:

| Handler | Trigger | Channels | Settings |
| --- | --- | --- | --- |
| disboard-bump-tracker | message | 702178352395583498 | include_bot_messages, allowed_role_ids 715584554861330465 |
| dm-relay-forum-reply | message | 1529245717678264320 | none |
| dm-relay-mirror | dm_message | 1529245717678264320 | none |
| leaver-post-janitor | member_leave | none | none |
| member-count-display | schedule | 968972011407826954 | interval_seconds 600 |
| onboard-and-promote | member_rules_accepted | none | allowed_role_ids 888160821673349140, 644325811301777426 |
| onboarding-reconcile | schedule | none | daily_time 04:00, allowed_role_ids 644325811301777426, 888160821673349140 |
| rejoin-alert | member_join | none | none |
| server-birthday-announcement | schedule | 1266565514793062400 | daily_time 01:00 |

`tests/web/live_admin_handlers_test.py` checks that every script still passes
the save-time checks and runs against the handler runtime. Do not edit these
files to make a test pass: they are what production runs.
