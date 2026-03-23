# Scan — Product Overview & Technical Specification

## What Scan Is

Scan is the deep research agent for **Smarter Dev** — a tool that answers technical questions the way a sharp senior developer would: with synthesis, context, and practical guidance. It serves the Smarter Dev community (software developers at intermediate-to-advanced levels navigating the AI era) and handles general knowledge queries with the same rigor.

Scan is available to all Smarter Dev members. What changes between tiers is **how hard it works for you**.

---

## Model Matrix

Scan's pipeline uses different models at each stage, with cost and quality scaling together.

| | Research Agent | Synthesis | Code Examples |
|---|---|---|---|
| **Lite stack** | GPT 5.4 Nano | Gemini Flash Lite | Gemini Flash (with thinking) |
| **Full stack** | Gemini Flash | Gemini Flash | Gemini Flash (with thinking) |

GPT 5.4 Nano is a strong agentic model — it persists until done and is excellent at finding answers and extracting relevant information. Flash Lite is a good writer but a poor researcher — it handles synthesis well but shouldn't drive the research process. The full stack (Flash + Flash) brings deeper reasoning, better judgment about source quality, and more polished output at both stages.

Code examples always use Flash with thinking enabled, regardless of stack. The thinking budget produces better-targeted, more practical examples.

This means the cost difference between modes is dramatic. Quick modes are nearly free to operate. Standard and Deep are meaningfully more expensive per query — which is why they're gated by tier, not just preference.

---

## Researcher Modes

Every query runs through the same two-stage pipeline: **Research** → **Synthesis**. The tools are identical across all modes — Search and Read. What changes is the **skill** the agent loads and the **models** running the pipeline. The skill governs research strategy, iteration behavior, depth of analysis, and synthesis quality bar.

### Mode Selection

The agent auto-detects the appropriate mode based on query characteristics, with a manual override available in the UI. Users see the selected mode before results stream and can switch with one click.

**Auto-detect signals:**

| Mode | Triggers |
|------|----------|
| Quick Answer | Short queries (<15 words), "what is," "how to," "syntax for," single-concept lookups, questions with a single obvious answer, error messages |
| Quick Research | "How to" with context, "best way to," "explain," simple comparisons, "what should I use for," questions that benefit from 1–2 related angles |
| Standard | Complex comparisons ("X vs Y in context of Z"), "how should I architect," multi-part questions, requests for examples with alternatives, "recommend" with constraints |
| Deep | "Tradeoffs," "architecture," "current state of," "help me understand," "evaluate," complex/multi-concept queries, follow-up depth requests |

The boundary between Quick Answer and Quick Research is the thinnest — both are fast and cheap. The agent should lean toward Quick Research when there's any ambiguity, since the cost difference is negligible and the quality bump is noticeable.

When a user escalates a query (reruns at a higher mode), the agent retains the full research context from the prior run. The higher-mode skill instructs it to evaluate existing research, identify gaps, and deepen selectively rather than starting over.

---

## Mode Specifications

### Quick Answer — Retrieval Mode

**Models:** GPT 5.4 Nano (research) → Flash Lite (synthesis)
**Cost:** Fraction of a cent
**Speed:** Under a second
**Behavior:** Find the answer. Return it with sources.

Quick Answer is a comprehensive, intelligent search. The agent searches once or twice, reads a source summary, and delivers the direct answer with the most relevant sources. Think of it as what Google should be — you ask a question, you get the answer, you get the links, done.

No exploration, no related questions, no alternatives. Just the answer.

**What the user gets:**
- A direct, confident answer in under a second
- One code example if applicable
- 2–3 most relevant source links
- YouTube link only if the query is explicitly tutorial-oriented

**What Quick Answer is for:**
- "What's the syntax for pattern matching in Python 3.12?"
- "What port does Redis use by default?"
- "FastAPI dependency injection example"
- "How do I install uv?"
- "What does error code ECONNREFUSED mean?"

**What Quick Answer is not for:**
- Anything where "it depends" is the honest answer
- Questions that benefit from seeing related context

---

### Quick Research — Contextual Retrieval Mode

**Models:** GPT 5.4 Nano (research) → Flash Lite (synthesis)
**Cost:** Fraction of a cent
**Speed:** Under a second
**Behavior:** Find the answer, but think about what else is relevant.

Quick Research uses the same lite models as Quick Answer but adds a planning step — the agent briefly considers what related questions would help give a fuller answer. It might search from a second angle or note an important caveat that Quick Answer would skip.

The cost difference from Quick Answer is negligible. The quality difference is noticeable — Quick Research answers feel more complete without feeling slow.

**What the user gets:**
- A direct answer with brief additional context
- Code example tailored to detected language/framework
- Relevant caveats or "watch out for" notes
- 2–4 source links with relevance context
- YouTube results if relevant

**What Quick Research is for:**
- "How do I set up a virtual environment in uv?"
- "What's the best way to handle environment variables in Python?"
- "Explain Python's GIL"
- "Difference between `asyncio.gather` and `asyncio.TaskGroup`"
- "How do I handle CORS in FastAPI?"

**What Quick Research is not for:**
- Deep comparisons where both sides need thorough evaluation
- Architectural decisions with real tradeoffs

---

### Standard — Exploration Mode

**Models:** Flash (research) → Flash (synthesis)
**Cost:** Low single-digit cents
**Speed:** 15–30 seconds
**Behavior:** Understand the landscape. Present the best path.

Standard is where the model upgrade hits. Flash brings meaningfully better reasoning about source quality, more nuanced synthesis, and richer code examples. The agent searches from multiple angles, reads sources with enough depth to identify consensus and disagreements, and delivers an answer that covers the main approach plus meaningful alternatives.

This is the mode where users feel the quality jump from the free tier. Same question, noticeably better answer.

**What the user gets:**
- Synthesized explanation covering the primary approach and 1–2 alternatives
- Tailored code examples using the user's known stack and patterns
- Curated resource section: relevant docs/articles + YouTube videos with relevance context
- Enough depth to make a decision, not so much that it becomes a research paper

**What Standard is for:**
- "How should I structure authentication in a FastAPI app?"
- "Compare SQLAlchemy vs Tortoise ORM for async PostgreSQL"
- "Best practices for error handling in TypeScript"
- "What testing framework should I use for a new Python CLI tool?"

**What Standard is not for:**
- Simple lookups that Quick modes handle in under a second
- Deep architectural decisions where the user needs a full picture before committing

---

### Deep — Investigation Mode

**Models:** Flash (research, maximum token budget) → Flash (synthesis, maximum token budget)
**Cost:** Higher single-digit cents
**Speed:** 1–2 minutes
**Behavior:** Do the spike. Leave no gaps.

Deep uses the same Flash models as Standard but with a significantly higher token budget — the agent has room to think longer, search more, read more fully, and synthesize more comprehensively. It decomposes the question into sub-questions, searches broadly then narrows, reads sources fully, cross-references claims, identifies outdated information, and iterates until it has addressed the query's full scope.

**What the user gets:**
- Comprehensive synthesis structured as a technical brief
- Multiple code examples showing different approaches with tradeoff annotations
- Version-specific notes and gotchas
- Cross-referenced claims (conflicting sources are noted and evaluated)
- Annotated resource section: docs, articles, and YouTube videos with timestamps and "start here" guidance
- A clear recommendation with reasoning: "here's what I'd actually do and why"

**What Deep is for:**
- "What are the tradeoffs of event sourcing vs CRUD for my order management system?"
- "Current state of Python async database drivers — what's production-ready?"
- "Help me understand the security implications of JWTs vs session tokens for my multi-service architecture"
- "I need to choose a deployment strategy for a Python monorepo with 4 services on Kubernetes"

**What Deep is not for:**
- Anything you could answer with a quick search — Deep is a considered action, not a default

---

## Escalation Behavior

When a user reruns a query at a higher mode, the agent receives the full history from the prior run: all search results, all read content, the previous synthesis, and the previous output (sources, insights, outline). The new skill instructs the agent to:

1. Review existing research against the new mode's quality bar
2. Identify specific gaps: missing perspectives, shallow coverage, unresolved conflicts, outdated claims
3. Conduct targeted follow-up research to fill those gaps
4. Re-synthesize with the combined body of research

This makes escalation fast and cost-efficient. A Quick → Standard escalation is particularly powerful — the lite models already found the core answer and sources, and the Flash models can evaluate that foundation and add depth intelligently rather than re-searching from scratch.

---

## Tier Access & Limits

All tiers get the same pipeline, the same tools, the same skill quality. Tiers control **which modes are available** and **how often you can use them**.

### Permissions

Access is controlled by three per-mode permissions assigned to roles:

| Permission | Grants Access To |
|---|---|
| `use-scan-quick` | Quick Answer + Quick Research |
| `use-scan-standard` | Standard mode |
| `use-scan-deep` | Deep mode |

### Role → Permission Assignments

| Role | Permissions | Display |
|---|---|---|
| `member` (free) | `use-scan-quick` | Free tier |
| `sudo-r` | `use-scan-quick`, `use-scan-standard` | r-- ($5/mo) |
| `sudo-rw` | `use-scan-quick`, `use-scan-standard`, `use-scan-deep` | rw- ($10/mo) |
| `sudo-rwx` | `use-scan-quick`, `use-scan-standard`, `use-scan-deep` | rwx ($20/mo) |

### Rate Limits

| | Free | r-- ($5/mo) | rw- ($10/mo) | rwx ($20/mo) |
|---|---|---|---|---|
| **Quick Answer** | 50/week (shared) | Unlimited | Unlimited | Unlimited |
| **Quick Research** | ↑ shared budget | Unlimited | Unlimited | Unlimited |
| **Standard** | 3/month | 15/week | 30/week | 60/week |
| **Deep** | — | — | 5/week | 15/week |

**Budget resets weekly** for paid tiers (not monthly). This creates a natural usage rhythm and reduces the perceived cost of hitting a limit — you're never more than a few days from a reset.

### Why these numbers

**Free (Quick Answer + Quick Research, 50/week shared + 3 Standard/month):** Both Quick modes are dirt cheap to operate, so the free tier can be genuinely generous. 50 queries per week is roughly 7 per day — enough for a developer who uses Scan as their primary search tool for dev questions. The shared budget means users naturally discover Quick Research when it auto-selects, without worrying about a separate limit. 3 Standard queries per month let free users experience the quality jump regularly — an ongoing taste, not a one-time trial.

**r-- (+ Standard, 15/week):** The upgrade moment. Unlimited Quick removes all friction for daily use. 15 Standards per week (~2/day) is enough for a working developer to lean on it for real decisions. The model upgrade from GPT 5.4 Nano/Flash Lite to Flash/Flash is immediately noticeable — same question, meaningfully better answer. No Deep access keeps the next tier visible.

**rw- (+ Deep, 5/week):** The power unlock. 30 Standards covers daily use with room for bigger days. 5 Deep queries per week means each one is a considered action — "I'm going to use a Deep on this." This tier is for developers actively building and making architectural decisions.

**rwx (Full access, 15 Deep/week):** For the most engaged members. 60 Standards and 15 Deeps means they can realistically run 2–3 Deep investigations per day. This tier gets the most value from profiling and cross-session context.

---

## User Profiling

Profiling is a **paid tier feature**. Free users receive no profiling — Scan infers language and framework from the query itself (a question mentioning FastAPI gets Python/FastAPI-flavored answers), but nothing persists between queries.

| Tier | Profile Depth |
|---|---|
| Free | **No profiling.** Stack detection from query text only. No persistence. |
| r-- | **Remembered stack + experience level.** Scan knows you write Python, prefer async patterns, use pytest, and are comfortable with advanced concepts. Persists across sessions. |
| rw- | **+ Project context and style preferences.** Scan knows you're building a microservices platform, prefer concise answers over detailed walkthroughs, and like seeing the "why" before the "how." |
| rwx | **+ Cross-session memory and codebase patterns.** Scan remembers previous research threads, knows your architectural decisions, and can reference past investigations. Closest to having a dedicated research assistant. |

For paid tiers, profile context is injected **before** mode selection so it can influence auto-detect behavior. A query like "how should I handle this?" from a user with rich project context may auto-select Standard or Deep, while the same query from a free user (no context) would default to Quick Research with a clarifying note.

---

## Pipeline Architecture

Every query flows through four sequential stages: **Meta → Research → Synthesis → Code Examples**.

### Stage 0 — Meta Analysis

A fast classification step that runs before research begins. Produces:
- **Session name:** Human-readable title for the query
- **Topic classification:** Used to determine whether YouTube/resources/code examples are relevant
- **Skill level detection:** Beginner, intermediate, or advanced — informs research depth and synthesis tone
- **Query format:** Simple vs. complex — affects rendering
- **Research mode:** Auto-detected mode (Quick Answer, Quick Research, Standard, Deep) based on query characteristics, unless manually overridden

### Stage 1 — Research (Agent)

The research agent has three tools:

- **Search(query):** Web search, returns ranked results with snippets
- **Read(url, summarization_instructions?):** Fetches and reads a URL. Optional summarization instructions control depth — omit for full content, include for targeted extraction
- **YouTubeSearch(query):** Searches for relevant YouTube videos, returns video metadata (title, channel, duration)

The agent loads the research skill for the selected mode. The skill governs all subsequent behavior: how to plan searches, when to iterate, how thoroughly to read, when to stop, and whether/how to search for YouTube content.

Within the research stage, conversation history is threaded across iterations — the agent's planning, searching, reading, and reasoning all share context. This allows the agent to build on prior findings, avoid redundant searches, and make informed decisions about when to stop.

**Research stage output (structured contract):**
- **Sources:** List of URLs with titles, types (docs/tutorial/blog/comparison/video/reference), and the content/summaries extracted from them. These serve dual purpose: inline citations in the synthesis and curated resource links for sidebar display.
- **Key insights:** The critical findings, patterns, and conclusions from the research
- **Outline:** Structured plan for the synthesis — what to cover, in what order, what to emphasize
- **YouTube URLs (optional):** Relevant video resources discovered during research, with titles and relevance context

### Stage 2 — Synthesis (Clean Context)

Synthesis receives a **clean, curated context** — the structured research output — not the raw conversation history from the research stage. This separation is deliberate: the research agent's internal reasoning, failed searches, and iterative process should not leak into synthesis. The synthesis model gets exactly what it needs to write well.

Synthesis receives:
- The user's original query
- User profile context (paid tiers only)
- The structured research output: key insights, outline, source content, YouTube URLs

The synthesis model varies by mode (Flash Lite for Quick modes, Flash for Standard/Deep). The synthesis skill governs the structure, depth, and style of the response. The response streams back to the user in real time.

### Stage 3 — Code Examples (Post-Synthesis)

Code examples are generated **after synthesis completes**, as a sequential post-processing step. This ensures examples have full context: the original query, the complete synthesis response, and the user's profile.

Code examples use Flash with thinking enabled (regardless of mode stack) for quality reasoning about what to illustrate.

- **Input:** User query + full synthesis response + user profile
- **Output:** Structured list of `CodeExample` objects (title, language, code, explanation)
- **Scale varies by topic:** Up to 5 short examples (5–15 lines) for syntax/patterns, 2–3 medium (15–40 lines) for APIs/algorithms, or 1 large (40–100 lines) for complete programs
- **Progressive difficulty:** Each example builds on the previous, simple → sophisticated
- **Skipped when inappropriate:** Non-code topics (comparisons, career advice, conceptual questions) get no examples

### Post-Research Enrichment

After the research stage completes, the pipeline enriches raw URLs from the research output before delivering them to the frontend:

- **YouTube metadata:** Video IDs extracted → YouTube Data API v3 fetches title, channel, thumbnail, duration → videos under 5 minutes filtered out → top results ranked by relevance
- **Resource metadata:** Source URLs → OG metadata fetch (title, description, image, favicon, site name) → domain quality tiers applied (authoritative > generic > low-quality) → enriched for sidebar card display

This enrichment is a data processing step, not additional research — it adds display metadata to URLs the researcher already found.

---

## Skill System

Each mode has two skill files: one for Research, one for Synthesis. Skills are markdown documents that define behavior, strategy, quality bars, and output expectations. The agent reads the skill and follows its instructions — the tools don't change, the thinking does.

**Skill files:**

| Mode | Research Skill | Synthesis Skill |
|---|---|---|
| Quick Answer | `research-quick-answer.md` | `synthesis-quick-answer.md` |
| Quick Research | `research-quick-research.md` | `synthesis-quick-research.md` |
| Standard | `research-standard.md` | `synthesis-standard.md` |
| Deep | `research-deep.md` | `synthesis-deep.md` |

**Skill selection flow:**
1. Query arrives with user tier and profile context
2. Agent receives skill descriptions for all accessible modes
3. Agent selects a mode (auto-detect with user override)
4. Agent loads the full Research skill for that mode
5. Research executes per skill instructions → outputs structured results
6. Synthesis call loads the Synthesis skill for that mode
7. Synthesis produces the final streamed response

**Skill descriptions shown to agent for mode selection:**

```
## quick-answer
Fast, direct answers for straightforward questions. Syntax, definitions, 
simple how-tos, quick lookups, error messages. Use when there's one clear 
answer and the user just needs it. Think: better Google search.

## quick-research
Fast answers with a bit more context. Same speed, but considers related 
questions and surfaces relevant caveats. Use when the query benefits from 
a second angle but doesn't need deep exploration. Think: Google search 
plus a knowledgeable friend's commentary.

## standard
Thorough answers for questions that require judgment. Comparisons, best 
practices, architecture patterns, "how should I" questions. Use when the 
user needs to understand options and make a decision. Model upgrade 
delivers noticeably better reasoning and synthesis.

## deep
Comprehensive research for complex or high-stakes questions. Architecture 
decisions, tradeoff analysis, current landscape evaluation, technology 
selection. Use when the user needs a complete picture before committing 
to an approach.
```

Skills can be iterated independently of the pipeline code. Improving research quality is an edit to a markdown file, not a code change.

---

## Cost Architecture

The mode structure creates a natural cost curve:

| Mode | Approx. Cost | What Drives It |
|---|---|---|
| Quick Answer | < $0.005 | GPT 5.4 Nano agent (1–2 searches, 1 read) + Flash Lite synthesis + Flash code examples |
| Quick Research | < $0.005 | GPT 5.4 Nano agent (2 searches, 1–2 reads) + Flash Lite synthesis + Flash code examples |
| Standard | $0.02–0.05 | Flash agent (4–6 searches, 3–5 reads) + Flash synthesis + Flash code examples |
| Deep | $0.05–0.15 | Flash agent (8–15 searches, 6–10 reads, max budget) + Flash synthesis (max budget) + Flash code examples |

Code examples add a small fixed cost per query (Flash with thinking). This cost is consistent across modes since examples are always generated by the same model — what varies is the richness of context they receive from the synthesis.

The Quick modes are cheap enough to offer generously at the free tier. Standard and Deep are where per-query costs become meaningful, which is why they're gated behind paid tiers with weekly limits.
