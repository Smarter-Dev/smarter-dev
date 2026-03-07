# Scan — API & Site Architecture Spec

## Overview

Scan is a research service hosted at `scan.smarter.dev`. It runs all research logic server-side and exposes an SSE-based API for clients (initially the Smarter Dev Discord bot). Results are persisted and viewable on the site. The Discord bot becomes a thin client that triggers research and relays the condensed response, linking to the full result.

---

## Phase 1: Base Implementation

Flash Lite + Brave Search → Jina Read. Mirrors the existing Discord bot's research capability, moved server-side with persistence and a web viewer.

### Core Flow

```
Discord Bot                        Scan API                          Site
───────────                        ────────                          ────
/research [query]  ──POST /api/research──►  Create session
   or @bot mention                         Start research agent
                   ◄──SSE stream──────────  Stream tool use events
                                           Stream final response
                   ◄──final event─────────  { result_url, summary }
                                                    │
Post summary +                                      ▼
link in Discord                              Persist to DB
                                             Viewable at /r/{id}
```

### API Endpoints

#### `POST /api/research`

Starts a new research session. Returns an SSE stream.

**Request:**
```json
{
  "query": "how does FastAPI dependency injection work",
  "user_id": "discord_user_id",
  "context": {
    "channel_id": "discord_channel_id",
    "guild_id": "discord_guild_id"
  }
}
```

**Headers:**
```
Authorization: Bearer {api_key}
Content-Type: application/json
```

**SSE Event Stream:**

```
event: status
data: {"stage": "planning", "message": "Analyzing query..."}

event: tool_use
data: {"tool": "brave_search", "input": {"query": "FastAPI dependency injection"}, "status": "running"}

event: tool_result
data: {"tool": "brave_search", "results": [{...}], "status": "complete"}

event: tool_use
data: {"tool": "jina_read", "input": {"url": "https://fastapi.tiangolo.com/tutorial/dependencies/"}, "status": "running"}

event: tool_result
data: {"tool": "jina_read", "results": [{...}], "status": "complete"}

event: status
data: {"stage": "synthesizing", "message": "Composing response..."}

event: response_chunk
data: {"text": "FastAPI's dependency injection system ", "done": false}

event: response_chunk
data: {"text": "works by declaring function parameters...", "done": false}

event: complete
data: {"result_id": "abc123", "result_url": "https://scan.smarter.dev/r/abc123", "summary": "Condensed version for Discord..."}
```

**Event types:**
- `status` — Stage transitions (planning, searching, reading, synthesizing). Bot uses these for tool use indicators.
- `tool_use` — Agent is invoking a tool. Bot can show "Searching..." / "Reading..." indicators.
- `tool_result` — Tool completed. Includes result metadata (not full content — that stays server-side).
- `response_chunk` — Streamed final response text. Bot can progressively update its Discord message.
- `complete` — Research finished. Contains the result URL and a pre-condensed summary suitable for Discord.
- `error` — Something broke. `{"error": "message", "recoverable": bool}`

#### `POST /api/research/{result_id}/followup`

Appends a follow-up to an existing research session. Returns an SSE stream (same event format as above).

**Request:**
```json
{
  "message": "how does that compare to Flask's approach?",
  "user_id": "discord_user_id"
}
```

The `user_id` here is whoever is asking the follow-up — not necessarily the original researcher. Multiple users can follow up on the same session. The Discord bot may also aggregate multiple user follow-ups and send them under its own bot ID.

The server loads the full prior context (original query, all sources fetched, prior response, all prior follow-ups) and feeds it to the agent. The agent decides whether it can answer from existing context or needs to search again. If it searches, new sources get added to the result page.

**Response:** Same SSE stream format. The `complete` event returns the same `result_url` — the page now includes the follow-up.

#### `GET /api/research/{result_id}`

Returns the full result as JSON. For programmatic access or if the bot needs to re-fetch.

```json
{
  "id": "abc123",
  "query": "how does FastAPI dependency injection work",
  "response": "Full markdown response...",
  "sources": [
    {
      "url": "https://fastapi.tiangolo.com/tutorial/dependencies/",
      "title": "Dependencies - FastAPI",
      "type": "docs",
      "cited": true
    },
    {
      "url": "https://github.com/tiangolo/fastapi",
      "title": "tiangolo/fastapi",
      "type": "repo",
      "cited": false
    }
  ],
  "followups": [
    {
      "user_id": "discord_user_id",
      "message": "how does that compare to Flask's approach?",
      "response": "...",
      "additional_sources": [...]
    }
  ],
  "created_at": "2026-03-06T...",
  "user_id": "discord_user_id"
}
```

### Authentication

API key in the `Authorization` header. Simple bearer token scheme. Keys are created manually — one for the Discord bot, potentially more later for other clients.

No user-facing auth for the site in Phase 1 — result pages are accessible to anyone with the URL but not indexed or discoverable. Security through obscurity on the result IDs (UUIDs or similar).

### Data Model

```
Research Session
├── id: uuid
├── query: text
├── user_id: text (Discord user ID)
├── guild_id: text
├── channel_id: text
├── response: text (markdown)
├── sources: json[]
│   ├── url
│   ├── title
│   ├── type (docs | repo | article | video | forum | other)
│   ├── snippet: text (relevant excerpt for sidebar display)
│   └── cited: bool (referenced in the response body)
├── tool_log: json[] (full record of tool calls for transparency)
├── followups: json[]
│   ├── user_id
│   ├── message
│   ├── response
│   ├── additional_sources
│   └── timestamp
├── created_at: timestamp
└── updated_at: timestamp
```

### Result Page (`/r/{id}`)

The web view of a completed research result.

**Layout:**
- **Main content area**: The full research response rendered as markdown. Code blocks with syntax highlighting. Follow-ups displayed as a continued conversation below the original.
- **Sidebar**: All sources, grouped by type (Docs, Repos, Articles, Videos, Discussions). Cited sources marked. Each source shows title, domain, and snippet.
- **Header**: Original query, timestamp, Smarter Dev branding.

### Agent Architecture (Phase 1)

Minimal — mirrors the existing bot's capability:

1. **Plan**: Flash Lite evaluates the query, decides search terms
2. **Search**: Brave Search API
3. **Read**: Jina Reader on the most relevant results
4. **Synthesize**: Flash Lite composes the response from gathered context
5. **Classify sources**: Tag each source by type, determine which were cited vs. supplementary

The agent loop is simple: plan → search → read top results → synthesize. No multi-turn agentic reasoning yet — that's Phase 2.

---

## Phase 2: sudo Enhanced Research

Unlocked per-user based on Discord user ID enrollment in a sudo membership tier. Same API, better pipeline.

### What Changes

- **Model upgrade**: Flash Lite → Flash (or better) for planning and synthesis
- **Richer source pipeline**: Docs site crawling, GitHub repo/issues search, package registry lookups, YouTube API for tutorials
- **Query routing**: Classifies query type (package-specific, concept, general) and adjusts search strategy
- **Example generation**: Dedicated synthesis step that produces runnable code examples for coding queries
- **User profiling**: Background agent that builds a skill profile over time, tailoring response depth and terminology

### API Changes

The API stays the same. The `user_id` in the request is checked against sudo enrollment. If enrolled, the request gets routed through the enhanced pipeline. The response format is identical — sudo users just get better content in the same structure.

### User Profile (Phase 2 addition)

```
User Profile
├── user_id: text (Discord user ID)
├── skill_level: enum (beginner | intermediate | advanced) — self-reported
├── languages: json {language: proficiency_estimate}
├── frameworks: json {framework: familiarity_level}
├── concepts_known: text[] (inferred from interactions)
├── concepts_learning: text[] (inferred from questions needing explanation)
├── preferred_style: json (terse vs detailed, examples-first, etc.)
├── query_history_summary: text (agent-maintained rolling summary)
├── updated_at: timestamp
└── interaction_count: int
```

The profile agent runs post-response as a background task. It evaluates the interaction and updates the profile — no latency impact on the research response itself.

---

## Discord Bot Integration

The bot's research logic is replaced with API calls to Scan. The bot handles:

1. **Query detection**: Same as today — mention or `/research` command
2. **API call**: POST to `/api/research`, receive SSE stream
3. **Live indicators**: Map `tool_use` / `status` events to the existing tool use indicator system in chat
4. **Response posting**: Use `response_chunk` events to progressively edit a Discord message with the response, or post the `summary` from the `complete` event with the result link
5. **Follow-up detection**: When a user continues a conversation, bot calls `/api/research/{id}/followup` instead of starting a new session

### Upgrade path decision

The bot determines whether to use the research API or answer conversationally (current behavior) based on query complexity — same logic discussed earlier where it can auto-upgrade.

---

## Infrastructure Notes

- Hosted on Skrift (same as all Zech properties)
- Database: whatever Skrift uses — sessions and profiles are simple relational data
- SSE: standard server-sent events, no websocket complexity
- Jina Reader API for page content fetching
- Brave Search API for web search
- Background tasks for profile updates (post-response, async)
