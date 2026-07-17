# Legacy-Sunset Runbooks (HUMAN steps only)

Every deploy-time action in the sunset — key minting, secret rotation, data
copy execution, database drop — is performed by a human, never by an agent.
Implementation phases write their runbooks here:

| File | Written by phase | Covers |
| --- | --- | --- |
| `01-key-rotation.md` | 01 skrift-api-keys | Mint the bot service key, rotate the `bot-api-key` k8s secret, remove the legacy fallback after soak |
| `02-db-cutover.md` | 02 db-consolidation | Pause bot, run `scripts/copy_legacy_data.py --execute`, deploy flip, verify, set bc_websites read-only |
| `03-bot-admin-removal.md` | 03 admin-parity | Legacy `/bot-admin` now 404s (operator notes, bookmark map, deferred `WEB_SESSION_SECRET` cleanup) |
| `05-final-decommission.md` | 05 decommission | Final backup, revoke legacy keys, remove secrets, drop bc_websites |

Each runbook must state: preconditions, exact commands, verification checks,
and the rollback path. See the corresponding numbered plan doc in the parent
directory for the spec each runbook implements.
