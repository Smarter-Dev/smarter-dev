# Search queries: openai/gpt-6-luna

- **Model:** `openai/gpt-6-luna`
- **Reasoning:** medium
- **Prompt:** `v5`
- **Output mode:** tool
- **Run at:** 2026-09-30T17:58:47+00:00
- **Succeeded:** 25/25
- **Cost:** $0.006621 total, $0.000265 per request (list price)
- **Tokens:** 16886 in, 9864 out (6993 reasoning)
- **Query length:** 4.8 words on average, 8 at most
- **Retried for stray characters:** 0

## System prompt

```text
You write web search queries for a user's request.

Return 5 queries. Each one is what an experienced engineer would actually type into Google: usually 3 to 5 words, never more than 7 (an exact error message in quotes may be longer). Search engines do not reward extra keywords; every added word narrows or muddies the results.

Write each query as a phrase that the answering page would contain, such as an error message, a page title, a setting name or a short question. Do not string related terms together.

Make the 5 queries find 5 different kinds of page. Pick the 5 kinds that best fit the request, for example:
- the primary source: specification, reference docs, release notes, official data (use `site:` to target it)
- an explanation of the underlying concept or cause
- how to do it or fix it, with a worked example
- a comparison, alternative or trade-off
- a known pitfall, limitation or edge case
- people's first-hand experience: forums, Q&A threads, postmortems
- listings or aggregated data: calendars, salary surveys, benchmarks, directories

No two queries may be the same kind, and no query may be another one reworded. Name the kind in each query's angle. The test: if two of your queries would bring up mostly the same results, replace one. When the request is narrow, widen to another kind rather than rephrase; when few kinds fit (for example "what's happening this week"), split by subject area (AI, security, developer tools, consumer hardware), audience or region instead.

Rules:
- No filler words such as "official", "docs", "guide", "explanation", "best practices", "troubleshooting". To target a source, use `site:` (for example `site:nginx.org`).
- Do not put guesses or answers in a query. Search for what the user needs to learn, not what you think the answer is.
- Add a year or dates only when the request is about recent or current things. Today's date is 2026-09-30.
- Write in the user's language.

Bad (a pile of terms): `nginx 502 bad gateway upstream reverse proxy error causes troubleshooting fix configuration`
Bad (terms strung together): `nginx upstream timeout proxy buffer keepalive 502`
Bad (the same query reworded): `nginx 502 error`, `nginx 502 bad gateway fix`, `why nginx returns 502`
Good: `site:nginx.org proxy_read_timeout` (primary source), `nginx 502 bad gateway upstream` (cause), `nginx 502 only under load` (first-hand experience)
```

## Results

### py-async-timeout

> How do I put a timeout on an asyncio task in Python without cancelling the whole gather?

1. `site:docs.python.org asyncio.wait_for` — Primary source — Python reference
2. `asyncio gather cancellation semantics` — Concept — gather cancellation behavior
3. `asyncio individual timeout gather example` — Worked example — per-task timeout
4. `asyncio.timeout vs wait_for` — Comparison — timeout APIs
5. `site:stackoverflow.com asyncio gather wait_for` — Q&A — sibling task cancellation pitfall

_$0.000264 · 677 in / 392 out (281 reasoning) · 6.09 s_

### rust-vs-go-cli

> Should I write my next CLI tool in Rust or Go? I care about startup time and easy cross-compiling.

1. `site:go.dev/doc GOOS GOARCH cross compile` — Official reference for Go cross-compilation
2. `Rust Go CLI startup benchmark` — Measured CLI startup comparison
3. `Rust cross compile CLI example` — Worked cross-compilation example
4. `Go Rust cross compilation comparison` — Cross-compilation trade-offs
5. `Rust Go CLI startup experience site:reddit.com` — Developers’ first-hand experience

_$0.000371 · 682 in / 605 out (493 reasoning) · 8.43 s_

### postgres-slow-count

> Why is SELECT COUNT(*) so slow on my big Postgres table and what can I do about it?

1. `site:postgresql.org/docs index-only scans visibility map` — Primary source: PostgreSQL reference on index-only scans
2. `PostgreSQL why COUNT(*) scans table` — Concept: why exact counts scan rows
3. `PostgreSQL approximate row count pg_class` — How-to: estimate rows without exact count
4. `PostgreSQL cached count trigger counter` — Alternative: maintain a cached exact count
5. `site:stackoverflow.com PostgreSQL COUNT(*) slow large table` — Q&A: real-world slow count queries

_$0.000320 · 680 in / 503 out (368 reasoning) · 6.52 s_

### k8s-oomkilled

> my pod keeps getting OOMKilled but the memory graph never gets near the limit

1. `site:kubernetes.io container memory limit OOMKilled` — Primary source: Kubernetes memory-limit behavior
2. `Kubernetes OOMKilled below limit cgroup` — Cause: why OOM happens below the limit
3. `kubectl inspect container OOMKilled memory` — Worked diagnosis: inspect pod termination details
4. `container_memory_working_set_bytes memory limit OOM` — Monitoring pitfall: metrics may miss relevant usage
5. `site:stackoverflow.com OOMKilled memory graph below limit` — First-hand Q&A: similar graph discrepancy

_$0.000354 · 675 in / 574 out (436 reasoning) · 8.5 s_

### react-server-components

> Explain React Server Components like I only know classic client-side React.

1. `site:react.dev Server Components` — Primary source: React reference
2. `React Server Components mental model` — Concept: how rendering differs
3. `React Server Components example` — Worked example: using components
4. `React Server Components vs client components` — Comparison: server versus client
5. `React Server Components use client boundary` — Pitfall: server client boundary

_$0.000168 · 672 in / 202 out (99 reasoning) · 4.94 s_

### latest-python-release

> What's new in the latest Python release?

1. `site:python.org/downloads/release Python release 2026` — Official release listing
2. `site:docs.python.org/3/whatsnew/ Python what's new` — Feature reference
3. `Python latest release upgrade migration` — Upgrade and migration steps
4. `Python latest release performance benchmarks` — Performance comparison
5. `Python release 2026 discussion` — Developer reactions

_$0.000334 · 667 in / 534 out (427 reasoning) · 6.13 s_

### discord-bot-rate-limits

> My Discord bot gets 429s when it sends a lot of messages at once. How are the rate limits supposed to work?

1. `site:discord.com/developers/docs/topics/rate-limits` — Primary source: Discord’s rate-limit reference
2. `Discord API rate limit buckets global` — Concept: how route buckets and global limits work
3. `Discord 429 retry_after example` — Implementation: handling 429 responses in code
4. `discord.js REST rate limit queue` — Library behavior: how discord.js queues messages
5. `Discord bot mass messages 429 Reddit` — First-hand experience: developers’ mass-send issues

_$0.000214 · 685 in / 290 out (160 reasoning) · 5.67 s_

### first-job-portfolio

> I'm self-taught and trying to land my first dev job. What should actually be in my portfolio?

1. `junior developer portfolio hiring manager` — Hiring-manager perspective
2. `entry level developer portfolio examples` — Portfolio examples
3. `developer portfolio project case study` — How to present projects
4. `self taught developer portfolio Reddit` — First-hand experiences
5. `developer portfolio mistakes recruiters` — Common pitfalls

_$0.000342 · 680 in / 549 out (460 reasoning) · 8.54 s_

### css-center-div

> center a div vertically and horizontally

1. `site:developer.mozilla.org place-items CSS` — Primary source: CSS property reference
2. `CSS vertical horizontal centering methods` — Concept: centering techniques explained
3. `center div flexbox example` — How-to: flexbox worked example
4. `CSS grid vs flexbox centering` — Comparison: grid versus flexbox
5. `center div vertically horizontally Stack Overflow` — Q&A: practical developer answers

_$0.000222 · 665 in / 312 out (199 reasoning) · 4.45 s_

### git-undo-pushed-commit

> I pushed a commit with a secret in it to a public repo. What do I do now?

1. `site:docs.github.com removing sensitive data repository` — Primary source: removing leaked data from GitHub history
2. `revoke exposed API key` — Immediate credential response
3. `git filter-repo remove secret` — Worked example: rewriting Git history
4. `secret exposure git forks caches` — Pitfall: copies outside the repository
5. `site:stackoverflow.com leaked secret public repository` — First-hand Q&A experiences

_$0.000370 · 679 in / 605 out (491 reasoning) · 7.34 s_

### llm-local-laptop

> What's the best open-weight LLM I can run locally on a laptop with 16 GB of RAM?

1. `site:ollama.com/library 8B models` — Primary source: model library
2. `open LLM leaderboard 8B` — Comparison: model benchmarks
3. `llama.cpp quantization memory requirements` — How-to: memory-efficient local inference
4. `local LLM 16GB RAM context length` — Limitation: RAM and context length
5. `site:reddit.com 16GB laptop local LLM` — First-hand experience: laptop recommendations

_$0.000380 · 680 in / 625 out (502 reasoning) · 6.62 s_

### sqlite-prod

> Is SQLite actually fine for a production web app with a few thousand users?

1. `site:sqlite.org wal concurrency` — Primary source: SQLite concurrency and WAL reference
2. `SQLite production workload write concurrency` — Concept: how workload affects suitability
3. `SQLite web application production deployment` — How-to: deployment and backup example
4. `SQLite versus PostgreSQL production workload` — Comparison: SQLite versus PostgreSQL trade-offs
5. `SQLite multiple application servers filesystem` — Pitfall: multi-instance and filesystem limits

_$0.000166 · 674 in / 198 out (84 reasoning) · 3.51 s_

### typescript-generics-error

> TypeScript says 'Type T could be instantiated with an arbitrary type which could be unrelated to T'. What does that mean?

1. `site:typescriptlang.org/docs generic functions` — Primary source: TypeScript generic-function reference
2. `TypeScript arbitrary type generic instantiation` — Concept: generic type instantiation
3. `TypeScript generic type assignability example` — Worked example: resolving the compiler error
4. `"could be instantiated with an arbitrary type"` — Q&A: discussions of the exact error
5. `TypeScript generic constraint assignability pitfall` — Pitfall: generic constraints and safe assignments

_$0.000203 · 684 in / 270 out (144 reasoning) · 4.7 s_

### mechanical-keyboard

> looking for a quiet mechanical keyboard for coding in a shared office, budget around $150

1. `site:keychron.com silent mechanical keyboard` — Manufacturer product pages
2. `quiet mechanical keyboards under $150` — Buying roundups and listings
3. `silent mechanical switch comparison` — Switch comparison
4. `quiet mechanical keyboard shared office Reddit` — First-hand shared-office experiences
5. `silent mechanical keyboard sound test` — Typing sound demonstrations

_$0.000239 · 676 in / 343 out (247 reasoning) · 5.89 s_

### ergonomic-rsi

> My wrists hurt after long coding sessions. What can I change?

1. `site:osha.gov computer workstation wrists` — Primary source — workplace ergonomics recommendations
2. `computer use wrist pain causes` — Cause — why prolonged computer use can hurt
3. `keyboard mouse wrist ergonomics setup` — How-to — adjust desk and input-device setup
4. `vertical mouse trackball wrist pain comparison` — Comparison — alternative pointing devices
5. `wrist pain numbness when seek care` — Health guidance — symptoms needing medical attention

_$0.000241 · 672 in / 348 out (229 reasoning) · 4.56 s_

### oauth-pkce

> What is PKCE and do I need it if my OAuth app has a backend?

1. `site:rfc-editor.org RFC 7636 PKCE` — Primary source — PKCE specification
2. `OAuth PKCE code verifier challenge` — Concept — how PKCE works
3. `server side OAuth PKCE flow example` — Implementation — backend flow example
4. `PKCE versus client secret OAuth` — Comparison — PKCE and client secrets
5. `site:stackoverflow.com PKCE backend confidential client` — First-hand experience — backend app questions

_$0.000310 · 676 in / 485 out (365 reasoning) · 5.22 s_

### vague-slow-website

> my website is slow

1. `site:web.dev/articles core web vitals` — Primary source: performance metrics
2. `why is my website slow` — Cause: common bottlenecks
3. `Google PageSpeed Insights test website` — How-to: measure performance
4. `Lighthouse versus WebPageTest` — Comparison: diagnostic tools
5. `site:stackoverflow.com website slow causes` — First-hand experience: real-world diagnosis

_$0.000173 · 663 in / 214 out (105 reasoning) · 3.9 s_

### docker-image-size

> How can I make my Python Docker image smaller? It's 1.2 GB right now.

1. `site:hub.docker.com/_/python slim alpine` — Official reference for Python base image variants
2. `Docker image layers size explained` — Underlying explanation of image layer size
3. `Python Docker multi stage build example` — Worked example for shrinking a Python image
4. `dive Docker image layers analyze` — Tooling to inspect image contents
5. `site:stackoverflow.com smaller Python Docker image` — Real-world advice and pitfalls from engineers

_$0.000126 · 678 in / 117 out (0 reasoning) · 3.95 s_

### game-jam-engine

> Doing my first game jam next weekend, 2D, solo. Godot, Unity, or something else?

1. `Godot vs Unity 2D game jam` — Engine comparison
2. `site:godotengine.org 2D game development` — Primary source: engine capabilities
3. `Godot first 2D game tutorial` — Worked beginner example
4. `solo game jam Godot experience` — First-hand solo jam experience
5. `first solo game jam engine Reddit` — Community Q&A on choosing an engine

_$0.000329 · 682 in / 522 out (410 reasoning) · 6.35 s_

### regex-email

> whats the regex to validate an email address

1. `site:rfc-editor.org RFC 5322 addr-spec` — Primary standard
2. `site:developer.mozilla.org email input validation` — Reference page
3. `JavaScript email validation regex example` — Worked example
4. `why email regex validation is difficult` — Underlying limitation
5. `email validation regex edge cases Stack Overflow` — Q&A edge cases

_$0.000227 · 668 in / 321 out (221 reasoning) · 5.95 s_

### salary-remote-junior

> What do remote junior backend developers get paid in the US these days?

1. `remote junior backend developer salary US 2026` — Aggregated salary data
2. `remote entry level backend engineer salary jobs` — Current job listings
3. `junior backend developer salary remote Glassdoor` — Salary estimate comparison
4. `US software engineer salary survey entry level 2026` — Compensation survey
5. `remote junior developer salary Reddit US` — First-hand reports

_$0.000172 · 673 in / 210 out (104 reasoning) · 64.4 s_

### websocket-vs-sse

> For a live notifications feed, should I use WebSockets or Server-Sent Events?

1. `site:html.spec.whatwg.org EventSource server-sent events` — Primary source — browser API specification
2. `WebSockets vs SSE notifications` — Comparison — choose a transport
3. `SSE WebSocket bidirectional communication` — Concept explanation — communication patterns
4. `EventSource live notifications example` — Worked example — implement a feed
5. `SSE proxy buffering connection limits` — Pitfall — proxy and connection limits

_$0.000250 · 676 in / 364 out (249 reasoning) · 66.28 s_

### spanish-request

> ¿Cómo configuro un entorno virtual de Python en Windows?

1. `site:docs.python.org/es/3/library/venv.html venv Windows` — Fuente primaria: referencia de Python
2. `crear entorno virtual Python Windows PowerShell` — Cómo hacerlo: ejemplo en PowerShell
3. `para qué sirve entorno virtual Python` — Concepto: para qué sirve
4. `venv o conda Windows Python` — Comparación: alternativas a venv
5. `Activate.ps1 ejecución scripts deshabilitada` — Limitación frecuente: activación en PowerShell

_$0.000260 · 670 in / 387 out (258 reasoning) · 66.63 s_

### event-today

> Is there any big tech conference happening this week?

1. `technology conferences September 28 October 4 2026` — General tech event calendar
2. `AI conferences September 2026` — AI conference listings
3. `developer conferences October 2026` — Developer event schedule
4. `cybersecurity conferences October 2026` — Cybersecurity event calendar
5. `consumer tech conferences October 2026` — Consumer technology events

_$0.000301 · 669 in / 468 out (363 reasoning) · 67.29 s_

### learn-dsa-interview

> I have a FAANG interview in 3 weeks and I'm rusty on data structures and algorithms. Where should I focus?

1. `site:leetcode.com/studyplan interview preparation` — Curated interview problem list (primary source)
2. `FAANG coding interview topic frequency` — Topic priorities and frequency (aggregated data)
3. `three week coding interview study plan` — A practical three-week schedule (worked plan)
4. `site:reddit.com/r/leetcode FAANG interview preparation` — Candidate experiences (first-hand reports)
5. `coding interview preparation common mistakes` — Common preparation pitfalls (edge cases)

_$0.000281 · 683 in / 426 out (298 reasoning) · 67.45 s_
