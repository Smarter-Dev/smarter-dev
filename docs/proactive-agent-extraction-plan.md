# Proactive Agent Service Extraction Plan

## Purpose

Extract the proactive agent from the Discord bot into a separately deployed
repository and service without changing its observable Discord behavior.

The Discord bot will continue to watch and classify activity on its current
schedule. It will publish notifications to Redis. Shared proactive-agent
workers will run one isolated logical agent per guild, consume that guild's
notifications, obtain durable application data through REST, and perform
Discord operations through Discord's REST API.

This plan records the architecture decisions made before implementation and
breaks the extraction into independently testable stages.

## Agreed decisions

- The classifier/watcher remains in the Discord bot.
- The existing passive and active-ingest schedules do not change.
- Waking notifications are written to a guild-specific Redis wake stream.
- Non-waking notifications are written to a guild-specific pending list and
  drain into the next waking activation.
- Two guilds have two logical agents, separate queues, separate histories,
  separate locks, and separate settings, even when shared worker processes run
  both agents.
- The worker opens no Discord Gateway connection. Discord reads and writes use
  the same bot identity through Discord REST.
- Relational database access is available to the worker only through the
  application REST API.
- Agent history is written to Redis immediately for fast retrieval, then
  persisted to PostgreSQL through REST with a write-behind debounce.
- PostgreSQL is the recovery source when Redis history is absent or invalid.
- Queue delivery is at least once. Wake IDs and action checkpoints limit
  duplicate processing and duplicate Discord side effects.
- Rollout is controlled per guild, and the old and new consumers must never
  execute side effects for the same guild at the same time.

## Goals

1. Preserve current Discord-visible behavior, timing, prompts, tools, memory,
   history, watch instructions, response fitting, and usage attribution.
2. Give each guild a persistent, isolated agent identity and execution lane.
3. Wake a worker immediately when a waking notification reaches Redis.
4. Retain non-waking context without causing additional agent activations.
5. Make worker crashes and deployments recoverable without losing queued work.
6. Remove all in-process dependencies between the watcher and agent.
7. Allow the bot and agent repositories to deploy and roll back independently.

## Non-goals

- Replacing or retuning the watcher or agent models.
- Changing the 15-minute passive sweep, 15-second quiet debounce, 60-second
  maximum debounce, or active-window duration.
- Redesigning the proactive agent's personality or response policy.
- Giving the new worker direct SQL access.
- Moving `/proactive` commands out of the Discord bot.
- Opening a second Discord Gateway session for the worker.
- Guaranteeing exactly-once Discord message creation; Discord provides no
  general idempotency key for this operation.

## Current implementation map

The split starts from these boundaries in the current repository:

- `smarter_dev/bot/plugins/proactive.py` owns Discord events, buffering,
  scheduling, queue consumption, Discord dispatch, history persistence,
  settings, memory refresh, and recovery.
- `smarter_dev/bot/proactive/adapter.py` contains both `WatcherProducer` and
  `AgentConsumer`.
- `smarter_dev/bot/proactive/notifications.py` provides the typed notification
  model and an in-memory queue. Only notifications with `wakes=True` currently
  set the wake event.
- `smarter_dev/bot/proactive/watcher.py` and `windows.py` implement watcher
  classification and batching behavior.
- `smarter_dev/bot/proactive/agent.py`, `environment.py`, and `parity.py`
  implement the agent, its native tools, and chat-tool parity.
- `smarter_dev/bot/proactive/history_store.py` stores guild model history,
  recovery cursors, and active-ingest deadlines directly in Redis.
- `smarter_dev/bot/services/proactive_settings_service.py` already accesses
  proactive settings, watch addenda, and usage persistence through REST.

The implementation must separate these responsibilities rather than copying
the current plugin wholesale into the new repository.

## Target architecture

```text
Discord Gateway
      |
      v
Discord bot
  - event conversion
  - per-channel buffers
  - active/passive scheduler
  - deterministic engagement detection
  - watcher inference
  - recovery cursors
      |
      | Redis: guild-specific pending list + wake stream
      v
Shared proactive-agent workers
  - one logical AgentRuntime per guild
  - per-guild lock, queue drain, history, model runner
  - wake brief, tools, compaction, action journal
      |                         |
      | application REST       | Discord REST
      v                         v
Web API / PostgreSQL          Discord
```

### Discord bot responsibilities

- Maintain the only Discord Gateway connection.
- Convert Hikari events into the versioned notification contract.
- Preserve current per-channel buffering and scheduling.
- Run deterministic mention/reply checks and watcher inference.
- Publish waking and pending notifications to the correct guild keys.
- Persist watcher usage through the existing REST service.
- Own active-ingest windows and restart-recovery cursors.
- Continue to own `/proactive on`, `off`, and `status`.
- Consume runtime-mode commands sent back by an agent.

### Proactive-agent service responsibilities

- Discover guilds with queued work and block on wake streams.
- Acquire a renewable guild lease before processing a guild.
- Create or reuse exactly one logical `AgentRuntime` for each active guild in a
  worker process.
- Drain a waking batch and all pending context assigned to it.
- Load settings, memory, and durable data through authenticated REST calls.
- Load history from Redis, falling back to PostgreSQL through REST.
- Run the same agent prompt, model history, compaction, tools, and budgets.
- Fetch Discord context and execute Discord actions through REST.
- Save history immediately to Redis and schedule a debounced PostgreSQL write.
- Persist watch-addendum updates and usage through REST.
- Record wake and action checkpoints for retry safety.

### Web API responsibilities

- Continue serving proactive settings, usage, memory notes, handler management,
  and image-quota operations.
- Add authenticated guild-agent history endpoints.
- Apply optimistic history revisions so an older worker cannot overwrite newer
  history.
- Enforce request-size limits compatible with compacted model history.
- Expose runtime-mode persistence only if the control-stream design is not used.

## Per-guild isolation

Every guild-scoped resource includes the guild ID in its key or primary key:

- Wake stream
- Pending-notification list
- Dropped-notification counter
- Worker lease
- In-progress wake batch
- Wake/action checkpoint journal
- Redis history cache
- PostgreSQL history row
- In-memory `AgentRuntime`
- Model runner history
- Settings and instruction stores

A shared worker may service many guilds, but it must never share mutable agent
state between them. A slow guild must not block unrelated guilds; the worker
uses bounded concurrency across guilds and strict serialization within a guild.

## Redis design

### Key scheme

Use a versioned prefix and Redis hash tags so all keys for one guild occupy the
same Redis Cluster slot:

```text
proactive:v1:{guild:<guild_id>}:wake
proactive:v1:{guild:<guild_id>}:pending
proactive:v1:{guild:<guild_id>}:pending-dropped
proactive:v1:{guild:<guild_id>}:lease
proactive:v1:{guild:<guild_id>}:batch:<wake_id>
proactive:v1:{guild:<guild_id>}:checkpoint:<wake_id>
proactive:v1:{guild:<guild_id>}:history
proactive:v1:guilds-with-wakes
```

The exact prefix may change during implementation, but the guild hash tag and
schema version are required.

### Notification envelope

Both repositories validate the same JSON Schema:

```json
{
  "schema_version": 1,
  "notification_id": "uuid",
  "guild_id": "discord-snowflake",
  "channel_id": "discord-snowflake",
  "channel_name": "general",
  "kind": "mention",
  "created_at": "2026-09-01T16:00:00Z",
  "body": "render-independent structured content",
  "message_ids": ["discord-snowflake"],
  "wakes": true,
  "passive": false,
  "watcher_usage": {},
  "trace_id": "uuid"
}
```

Prefer structured fields over a pre-rendered prompt wherever the current
notification type permits it. The agent repository owns final wake-brief
rendering. Unknown optional fields are ignored within a schema version;
unknown versions are dead-lettered instead of guessed.

### Pending notifications

- Reactions, mode changes, instruction expiries, and other non-waking events
  append to the guild's pending list.
- Preserve the current queue limit of 20 notifications and its dropped count.
- Appending and trimming are one atomic Lua operation.
- A pending append does not add a wake-stream record.
- On a waking record, the consumer atomically moves the current pending list
  into an in-progress batch keyed by the wake ID. New pending records then land
  in a fresh list for the next activation.
- The batch remains until the wake is acknowledged, so a worker crash cannot
  lose notifications already removed from the pending list.

### Wake stream and consumption

- Each guild has its own Redis Stream and consumer group.
- A lightweight global ready set/stream tells shared workers which guild
  streams have work; it is a scheduling hint, not the source of truth.
- Workers use blocking reads and acquire the guild lease before draining.
- After the first wake record, the worker may coalesce already-pending waking
  records for that guild into one activation, matching current batching.
- A wake is acknowledged only after agent history, action checkpoints, and
  required persistence work reach their defined safe points.
- Stale pending entries are reclaimed with `XAUTOCLAIM`.
- Records that exceed the retry limit move to a dead-letter stream with their
  error class, attempt count, and original payload.

### Mid-run arrivals

The existing `drain_notifications` tool can observe notifications that arrive
while the agent is working. Its replacement atomically claims newly arrived
pending entries and wake records for the same guild into the current batch.
Mentions and replies remain verbatim and are never reduced to watcher summaries.

## History cache and persistence

### Data model

Add a PostgreSQL table similar to:

```text
proactive_agent_histories
  guild_id        text primary key
  schema_version  integer not null
  revision        bigint not null
  history         jsonb not null
  checksum        text not null
  updated_at      timestamptz not null
```

The migration belongs to the application/API repository, not the extracted
worker repository.

### REST contract

```text
GET /guilds/{guild_id}/proactive-agent/history
PUT /guilds/{guild_id}/proactive-agent/history
```

`GET` returns `404` when no durable history exists. `PUT` accepts the schema
version, monotonically increasing revision, checksum, and serialized history.
The API rejects a revision older than the stored revision and treats an
identical revision/checksum as idempotent.

### Read path

1. Read the guild history and revision from Redis.
2. Validate its schema and checksum.
3. On a valid hit, use it without calling PostgreSQL.
4. On a miss or invalid value, call the history REST `GET` endpoint.
5. Repopulate Redis from the REST result.
6. Start with empty history only when both stores contain no history.
7. Treat temporary REST failure on a Redis miss as a retryable wake failure;
   silently starting a new personality would violate guild isolation and
   continuity.

During migration, also check the legacy key
`proactive:guild-history:<guild_id>` once, validate it, then populate the new
cache and schedule its first PostgreSQL write.

### Write path

1. After each successful wake, increment the guild history revision.
2. Serialize and validate the compacted history.
3. Write the new value to Redis immediately.
4. Mark the guild revision dirty in the worker.
5. Debounce REST persistence for five seconds, collapsing multiple completed
   wakes to the newest revision.
6. Retry REST failures with bounded exponential backoff while retaining the
   dirty revision.
7. Perform a bounded flush during graceful shutdown.
8. Use newer-revision-wins semantics if ownership moves between workers.

The accepted recovery-point objective is up to the debounce interval if Redis
is lost before PostgreSQL is flushed. If that loss window becomes unacceptable,
the history PUT must move onto the synchronous wake-completion path.

## Cross-service control path

The agent's `set_monitoring_mode` tool currently mutates watcher state through
an in-process callback. Preserve it with an authenticated agent-to-bot Redis
control stream:

```json
{
  "schema_version": 1,
  "command_id": "uuid",
  "guild_id": "...",
  "channel_id": "...",
  "mode": "active",
  "minutes": 10,
  "created_at": "..."
}
```

The bot consumes the command, updates its active-window state, persists the
deadline using its existing mechanism, and records the command ID for
idempotency. The agent does not directly mutate watcher-owned keys.

If operations prefer an API control plane, replace this stream with an
authenticated REST endpoint and a bot-side notification mechanism; do not use
polling that weakens the current immediate mode change.

## Discord REST boundary

The new worker uses the existing bot identity and must support current agent
operations:

- Fetch recent channel messages and referenced messages.
- Fetch guild and channel names when they are absent from a notification.
- Send messages and replies with the existing length fitting/splitting rules.
- Add reactions and list available guild emoji.
- Upload generated images and anchor them to the same reply target.
- Post the existing web-search, web-read, image, and code-execution status
  messages.

Use a REST-only client initialized with the bot token. It must implement
Discord rate-limit headers and 429 retries, bounded timeouts, and test seams
for fake HTTP transports. It must not instantiate Hikari's Gateway bot.

Discord message creation has an unavoidable ambiguity if a worker crashes
after Discord accepts a send but before its checkpoint is saved. Record action
IDs and Discord response IDs immediately, avoid replay after a confirmed
checkpoint, and document the small remaining duplicate-send risk.

## Application REST inventory

Confirm or add worker-safe clients for:

- List enabled proactive channels for a guild.
- Read and update channel watch addenda.
- Record watcher and agent model usage.
- Load the guild long-term-memory snapshot and today's notes.
- Save agent memory notes.
- Register, list, and delete handlers.
- Read, reserve, and release image-generation quota.
- Read and write versioned proactive-agent history.

All calls use a service credential scoped to these routes. Guild IDs in the
request must match the credential's permitted tenancy where applicable.

## Repository boundaries

### Remain in the Discord bot repository

- Watcher prompts, models, and decision types
- Burst-window and passive-sweep scheduling
- Hikari event conversion
- Producer-side notification construction
- Redis producer and control-command consumer
- `/proactive` commands and settings cache
- Recovery cursors and active-ingest deadlines
- Watcher usage reporting

### Move to the `proactive-agent` repository

- Agent prompts, runner, model setup, and compaction
- Consumer-side notification types and wake-brief rendering
- Agent environment, actions, tool budget, and instruction stores
- Chat-tool parity wrappers adapted to REST clients
- Redis queue consumer, leases, batches, retries, and checkpoints
- Per-guild runtime registry
- Redis history cache and debounced REST history writer
- Discord REST and application REST clients
- Worker entry point, configuration, health checks, and metrics

### Shared contract, not shared implementation

Keep the notification and control-message JSON Schemas in one canonical
location and publish versioned generated models or copy a generated artifact
into each repository. Contract fixtures must be tested in both repositories.
Do not make the new service import the Discord bot's Python package.

## Configuration and security

The new service needs at least:

```text
REDIS_URL
API_BASE_URL
PROACTIVE_AGENT_API_KEY
DISCORD_BOT_TOKEN
PROACTIVE_AGENT_MODEL
PROACTIVE_SKIM_MODEL
model-provider credentials
worker concurrency and timeout settings
history debounce and shutdown-flush settings
```

- Keep secrets out of queue payloads and logs.
- Use separate API credentials for the bot and proactive-agent worker.
- Scope the worker key to the endpoints in the REST inventory.
- Redact Discord content from normal metrics and structured error fields.
- Rotate the shared Discord token without requiring repository changes.

## Observability and operations

Every producer, queue, wake, REST request, model run, and Discord action carries
the same trace ID and guild ID. Record:

- Pending-list length and dropped count per guild
- Wake-stream length and oldest-record age
- Ready-to-start and end-to-end wake latency
- Worker lease contention and renewal failures
- Retry, reclaim, and dead-letter counts
- Agent activations, responses, reactions, and deliberate silence
- Token usage by model and operation
- Redis history hit/miss/invalid counts
- History revision lag between Redis and PostgreSQL
- History flush failures and dirty guild count
- Discord REST rate limits, failures, and possible duplicate actions

Readiness fails when Redis is unavailable or the worker cannot authenticate to
the application API. A temporary Discord failure should make the affected wake
retryable without taking unrelated guilds out of service.

## Implementation stages

### Stage 1 — Freeze behavior and define contracts

Deliverables:

- Golden fixtures for every notification kind and wake-brief rendering.
- Baseline tests for current passive/active timing and direct engagement.
- Versioned notification and control-command JSON Schemas.
- Queue key, retry, dead-letter, and per-guild isolation specifications.
- A parity inventory of every current agent tool and external dependency.

Exit criteria:

- Both future repositories can validate the same contract fixtures.
- Current observable behavior is captured before moving code.

### Stage 2 — Add durable history API support

Deliverables:

- PostgreSQL migration for guild agent histories.
- Authenticated history `GET` and conditional `PUT` endpoints.
- Revision, checksum, schema validation, and request limits.
- API service tests for missing, create, idempotent retry, update, stale update,
  and concurrent update cases.

Exit criteria:

- History round-trips through REST and stale workers cannot overwrite a newer
  revision.

### Stage 3 — Add Redis producer infrastructure to the bot

Deliverables:

- Redis queue producer behind an interface that can also use the current
  in-memory queue during rollout.
- Per-guild pending-list and wake-stream atomic operations.
- Global ready signal and queue metrics.
- Agent-to-bot control-command consumer.
- Shadow-publish mode with no external worker side effects.

Exit criteria:

- Golden notifications emitted in memory and Redis are semantically identical.
- Existing timing and watcher tests remain unchanged.
- Non-waking notifications never create a wake signal.

### Stage 4 — Bootstrap the `proactive-agent` repository

Deliverables:

- Python project, locked dependencies, lint/type/test configuration, container,
  configuration model, and local Redis/API dependencies.
- Worker lifecycle, health/readiness, structured logging, and metrics.
- Guild runtime registry, leases, stream consumer, pending-batch handling,
  reclaim, retries, and dead-lettering.
- Redis history cache and debounced REST persistence.

Exit criteria:

- Two test guilds execute concurrently with isolated queues and histories.
- A worker crash can reclaim an unfinished wake and its pending batch.

### Stage 5 — Move the agent and restore tool parity

Deliverables:

- Agent prompts, runner, compaction, environment, and consumer adapter.
- REST-only Discord client and application API clients.
- Every existing native and parity tool.
- Mid-run notification drain and runtime-mode command production.
- Response fitting, splitting, images, replies, reactions, instruction updates,
  memory refresh, and usage reporting.

Exit criteria:

- Golden wake briefs match the current implementation.
- Tool names, arguments, budget use, outputs, and side effects pass parity tests.
- The worker has no import or runtime dependency on the bot repository.

### Stage 6 — Reliability and end-to-end validation

Deliverables:

- Integration environment with Redis, fake application API, and fake Discord
  REST transport.
- Tests for Redis/API/Discord/model failure and recovery.
- Action checkpoint and duplicate-risk tests.
- Legacy Redis history migration tests.
- Load tests covering many guilds, one hot guild, and lease handoff.

Exit criteria:

- One guild's slow or failed wake does not delay another guild.
- No queued notification is silently lost across process termination.
- History recovers from PostgreSQL after the Redis cache is cleared.

### Stage 7 — Shadow, canary, and cutover

Deliverables:

- Per-guild destination/consumer ownership flag.
- Shadow comparison of queue payloads, wake briefs, and expected actions.
- Dashboards and rollback runbook.
- Canary allowlist and progressive rollout procedure.

Procedure:

1. Shadow-publish notifications while the in-process consumer remains owner.
2. Compare payloads and simulated wake outputs without external side effects.
3. Stop and drain the in-process consumer for a canary guild.
4. Switch that guild's ownership flag to the external worker.
5. Verify latency, silence/response decisions, Discord actions, token usage,
   history revisions, and pending counts.
6. Expand in small cohorts.
7. Roll back by stopping external ownership, reclaiming/draining its wake, and
   restoring in-process ownership; never overlap consumers.

Exit criteria:

- Canary guilds meet the behavior and reliability acceptance matrix.
- Rollback is exercised successfully before broad rollout.

### Stage 8 — Remove the in-process agent

Deliverables:

- Delete the old consumer, agent runtime, and direct agent-history writes from
  the bot repository.
- Remove agent-only model credentials and dependencies from the bot deployment.
- Retain producer contracts, watcher logic, recovery, commands, and rollback
  compatibility for the agreed support window.
- Update operational and developer documentation in both repositories.

Exit criteria:

- The bot cannot execute proactive agent side effects locally.
- All enabled guilds are externally owned and stable through the support
  window.

## Acceptance matrix

The current and extracted implementations must be compared for:

| Scenario | Required result |
| --- | --- |
| Passive ordinary chat | Same watcher schedule; no agent wake when declined |
| Passive cold entry | Same watcher decision and sweep-latency class |
| Mention or reply | Immediate deterministic waking notification, verbatim |
| Active conversation | Same quiet/max debounce and active-window extension |
| Non-waking reaction | Added to pending context; no activation by itself |
| Mode change/expiry | Pending until the next waking notification |
| Mid-run message | Available through the notification-drain tool |
| Two guilds | Separate histories, queues, settings, locks, and concurrent progress |
| Restart/redeploy | Pending and waking records remain recoverable |
| Redis history miss | Restore the latest PostgreSQL revision through REST |
| History write burst | Immediate Redis updates; one debounced newest REST write |
| Watch instruction | Same prompt visibility, TTL behavior, and REST persistence |
| Memory | Same hourly refresh and note-saving behavior through REST |
| Discord response | Same content, reply anchor, fitting, splitting, and identity |
| Reaction/image | Same Discord REST effect and channel routing |
| Usage | Watcher remains bot-attributed; agent/skim remain worker-attributed |
| Disabled channel | Actions rejected and no new producer notifications accepted |
| Worker failure | Retry/reclaim without losing the in-progress notification batch |

## Test strategy

### Contract tests

- Cross-repository fixtures for every notification and control command.
- Forward-compatible optional fields and rejection of unknown versions.
- Exact wake-brief golden tests.

### Unit tests

- Queue limit, trimming, dropped counts, and pending-vs-waking behavior.
- Per-guild runtime lookup and state isolation.
- History revision, checksum, debounce, and retry logic.
- Response fitting and Discord action routing.
- All agent tools and budgets.

### Integration tests

- Real Redis Streams, lists, leases, Lua operations, and reclaim behavior.
- API endpoints against PostgreSQL.
- Worker against fake Discord and application HTTP transports.
- Graceful and ungraceful worker shutdown.
- Redis cache deletion followed by PostgreSQL recovery.

### Load and fault tests

- Many idle guilds plus several simultaneously active guilds.
- One hot guild without starving others.
- Lease loss during a model run.
- Redis interruption before and after batch creation.
- REST persistence outage longer than the debounce window.
- Discord success followed by worker termination before checkpointing.

## Rollback and data compatibility

- Keep legacy history readable during the migration window.
- Version every Redis and REST payload before the first canary.
- Make database migrations additive until the external service is stable.
- Do not delete old Redis history keys during rollout.
- The per-guild `proactive:v1:{guild:<guild_id>}:owner` Redis key is the single
  authority for which consumer may perform Discord side effects. The bot
  synchronizes it at startup and on every notification; worker lease acquisition
  and renewal require the value `external`.
- Rollback tooling must inspect and reclaim outstanding external wakes before
  returning ownership to the bot.

## Risks and mitigations

### Duplicate Discord sends

Discord message creation cannot be made fully exactly once. Minimize the risk
with per-action checkpoints, immediate recording of returned message IDs, and
careful reclaim rules. Surface possible duplicates in metrics and logs.

### History loss inside the debounce window

Redis loss before a dirty revision reaches PostgreSQL can lose up to five
seconds of completed wake history. Monitor revision lag and make persistence
synchronous if this recovery point is unacceptable in practice.

### Cross-guild state leakage

Key all state by guild, prohibit global mutable runners, and make two-guild
isolation tests mandatory in every stage that touches runtime state.

### Contract drift between repositories

Use generated models or identical schema fixtures in both CI pipelines. Deploy
consumers before producers when adding optional fields; use a new version for
breaking changes.

### Bot/worker ownership overlap

Make ownership explicit and observable. Refuse to start a guild runtime without
the external ownership flag and lease; refuse local consumption when the flag
selects the external service.

### Large history payloads

Preserve existing compaction thresholds, set explicit API body limits, measure
serialized sizes, and add compression only if measurements justify it.

## Definition of done

- The proactive agent runs from its own repository and deployment.
- The Discord bot contains the watcher/classifier but no executable agent
  consumer.
- Each guild has an isolated logical agent and Redis queue namespace.
- Non-waking notifications wait for and drain with the next waking notification.
- Agent history reads from Redis and is durably persisted to PostgreSQL through
  REST with the agreed debounce and fallback behavior.
- All database-backed worker operations use authenticated REST endpoints.
- All Discord operations use Discord REST under the existing bot identity.
- The acceptance matrix and failure tests pass.
- A canary rollout shows no user-visible regression.
- Rollback has been tested and documented.
