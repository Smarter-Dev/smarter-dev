# Feature Parity Planning — Legacy Bot Migration

Implementation planning for porting the documented production features of the two legacy
bots into smarter-dev, leaning hard on the handler system: features become authored
handlers wherever possible, small railed extensions to the handler system are preferred
over new bot plugins, and only genuinely privileged/interactive functionality lands in
bot core.

**Legacy sources evaluated (11 docs, all covered):**
- `beginner.codes-bot/docs/features/` — auto-mod, celebration-engagement, disboard-bumping, dm-relay, mod-chat, onboarding, server-stat-counter
- `beginner.py-bot/docs/prod-functionality/` — 01-moderation, 02-sus-command, 03-rules, 04-resources-command

## Groups

| Plan | Features | Character |
|---|---|---|
| [Automated & Command Moderation](automated-and-command-moderation.md) | codes-auto-mod, py-moderation | Spam/scam/TLD/invite engine as admin handlers; `/ban` `/kick` `/purge` in bot core; one `ModerationAction` audit system for manual/AI/handler actions |
| [Staff Communication Channels](staff-communication-channels.md) | codes-dm-relay, codes-mod-chat | DM bridge (`dm_message` trigger + `send_dm`) and private mod threads (thread-ops emit family) |
| [Member Lifecycle & Role Automation](member-lifecycle-and-role-automation.md) | codes-onboarding, codes-celebration-engagement, py-sus-command | Member-event trigger family (`member_join` / `member_rules_accepted` / `member_role_change`), `add_role`/`remove_role`, persisted one-shot timers |
| [Engagement Loops & Server Stats](engagement-loops-and-server-stats.md) | codes-disboard-bumping, codes-server-stat-counter | Schedule-driven counters in handler memory; `rename_channel`. Online-count family (presence aggregator + `GUILD_PRESENCES`) dropped 2026-07-18 — no bot-core work remains |
| [Community Content Commands](community-content-commands.md) | py-rules (py-resources-command dropped 2026-07-18) | Almost entirely handler-today; `edit_message` for the canonical rules post |
| [Threads & Member Events](threads-and-member-events.md) | cross-group (approved design) | Five admin-only event triggers (`member_join` / `member_leave` / `member_rules_accepted` / `member_role_change` / `thread_create`), thread-aware message dispatch, thread scripting functions; amends lifecycle E1 to admin-tier-only |

## Cross-cutting handler-system extensions

Each plan designs its own extensions in detail, but these are shared — build once,
consume everywhere. Where two plans sketch the same extension, reconcile to a single
design before implementing (owners noted).

| Extension | Designed in | Also consumed by |
|---|---|---|
| Message-context enrichment (`author_role_ids`, `author_has_manage_messages`, `channel_parent_id`, mention id lists) | moderation §3.1 | all five groups |
| Default `allowed_mentions` suppression + `ping_role_id` rail on the emitter | moderation §3.2 | staff-comms, engagement, content (safety hardening for every existing handler) |
| Guild-scoped shared memory (`guild_memory_*`) | staff-comms E4 | lifecycle, engagement |
| `add_role` / `remove_role` metered emits (allowlist rails, role-change caps) | lifecycle E2 | engagement (Bump King crown) |
| Member-event trigger family (`member_join`, `member_rules_accepted`, `member_role_change`) | lifecycle E1 | moderation (rejoin alert) |
| Persisted one-shot self-refire timer (`schedule_timer`) | lifecycle E3 | any handler needing durable delayed work |

Group-local extensions: `message_edit` trigger, `delete_webhook`, mod-action read
functions, `mod_action` trigger (moderation); `dm_message` trigger, `send_dm`,
thread-ops family (staff-comms); bot-authored message opt-in, `rename_channel`,
guild-count reads (engagement); `edit_message` (content).

## Suggested build order

1. **Shared rails** — context enrichment + mention suppression (small, no migration,
   unblocks and hardens everything).
2. **Community content commands** — mostly handler-today; fastest end-to-end validation
   of the "feature as handler" approach.
3. **Member lifecycle** — the member-event trigger family and role mutation, which
   moderation and engagement both consume.
4. **Moderation** — the largest group; admin-handler auto-mod plus the bot-core
   slash-command slice.
5. **Staff communication** and **engagement loops** — independent of each other; order
   by appetite.

Each plan carries its own disposition table (every legacy capability appears with an
explicit disposition — handler-today / handler-extension / bot-core / drop), TDD-ordered
implementation phases, and open questions needing a decision before build.
