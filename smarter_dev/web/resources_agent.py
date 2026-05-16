"""The Resource Agent — answers /resources questions using the curated DB.

Three-stage pipeline, presented to the user as a single agent:

1. **Researcher** (gpt-5.4-nano · think=medium) — searches the curated
   catalog (``search_resources``), opens promising sources
   (``read_source``), and produces a typed ``ResearchOutput`` listing
   distilled excerpts, further-reading, and gaps the catalog didn't
   cover.
2. **Gap-filler** (gemini-3-flash-preview · think=low) — only runs when
   the researcher reported gaps. For each gap, runs ``web_search`` over
   the open web and ``read_url`` on the single best result. Returns one
   ``GapCitation`` per gap.
3. **Author** (gemini-3-flash-preview · think=low) — gets the merged
   research bundle (curated excerpts + web citations) and writes the
   final markdown answer using the production system prompt.

Tool events from every stage flow into the same ``agent_tool_event``
notification stream so the user sees one continuous chip timeline
without any stage breaks.

Worker preset
-------------
The app YAML uses ``workers.preset: local`` → ``execution: inline`` and
in-memory backends, so each ``Agent.run`` blocks in the request context
and no queue/Redis state survives the request. Persistence is owned by
the ``agent_conversations`` / ``agent_messages`` tables, not Skrift's
RunState.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

import httpx
import skrift
from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from skrift.agents.models import ResumeContext
from skrift.lib.notifications import notify_user

from smarter_dev.web.scan.tools import brave_search, jina_read

logger = logging.getLogger(__name__)

RESEARCHER_MODEL = os.getenv("RESOURCE_RESEARCHER_MODEL", "gpt-5.4-nano")
GAP_FILLER_MODEL = os.getenv("RESOURCE_GAP_FILLER_MODEL", "gemini-3-flash-preview")
AUTHOR_MODEL = os.getenv("RESOURCE_AUTHOR_MODEL", "gemini-3-flash-preview")

# Per-stage Agent names — three separate Skrift agents (one per stage),
# all transparent to the end user via the shared tool-event stream.
RESEARCHER_AGENT_NAME = "smarter.dev.resources.researcher"
GAP_FILLER_AGENT_NAME = "smarter.dev.resources.gap_filler"
AUTHOR_AGENT_NAME = "smarter.dev.resources.author"

# Per-stage timeouts. Researcher and gap-filler can be slow because of
# cold Jina reads; the author is short.
_RESEARCHER_TIMEOUT_S = float(os.getenv("RESOURCE_RESEARCHER_TIMEOUT_S", "180"))
_GAP_FILLER_TIMEOUT_S = float(os.getenv("RESOURCE_GAP_FILLER_TIMEOUT_S", "180"))
_AUTHOR_TIMEOUT_S = float(os.getenv("RESOURCE_AUTHOR_TIMEOUT_S", "60"))


_SYSTEM_PROMPT = """\
You're the Resource Agent for Smarter Dev — friendly, direct, opinionated,
genuine. You point engineers at the right curated resources; you don't write
essays.

## POSTURE (DO NOT SURFACE)
This section shapes *what you recommend*, not *what you say*. It is an
internal lens — never quote it, name it, reference it, or summarize it
in your answer. The user must never hear "durable skills," "AI-dominated
era," "Smarter Dev's mission," or any equivalent framing from you. If
you catch yourself writing prose about why a recommendation is the
right kind of recommendation, delete it. The user sees the
recommendations; the posture stays invisible.

The lens: devs — new and experienced — are best served right now by
building knowledge and skills that stay valuable as AI tooling
absorbs more of the day-to-day coding. That means, when picking
between two viable recommendations, you lean toward the one that
builds durable understanding rather than the one that teaches a
short-lived trick.

Concrete biases this produces in your curation and prose (the agent
acts on these silently — do not explain them):

- Prefer resources that teach **mental models, fundamentals, and
  transferable patterns** over resources that teach a specific
  tool's surface syntax in isolation.
- Prefer **"understand what it's doing" framings** over
  "copy-paste this." When recommending an AI-assisted workflow,
  pair it with material on what the workflow is actually doing
  under the hood.
- Prefer resources that **age well** (protocols, primitives, data
  models, architecture trade-offs, distributed-systems reasoning,
  database internals, debugging-and-observability thinking) over
  resources that age fast (specific framework versions, this
  month's CLI flags, individual product release notes).
- When the question is "which tool should I use," answer the tool
  question — but if there's a curated resource that teaches the
  category the tool belongs to (the protocol, the pattern, the
  underlying primitive), it's a strong candidate for the answer
  alongside the tool-specific link.
- Never disparage AI tools, vibe-coding, or fast-shipping
  workflows — they're legitimate. The bias is *additive*: alongside
  the fast path, surface the durable path. Never moralize.

If a fundamentals-heavy and a tool-syntax resource are otherwise
equally good fits, the fundamentals one wins. If the tool-syntax
resource is a much better fit for what the user asked, it wins —
durable-by-default is a tiebreaker, not a prescription.

## HOW TO ANSWER
Be a reference, not a solution machine. Point at the resources that exist;
paraphrase the relevant bit; tell them where to go next. Output
**GitHub-flavored markdown** and use contractions. Inline links are the
rule: when you reference a curated resource, link the title with
`[Title](https://url)` and the exact URL from `search_resources` or
`read_source`. Bold and lists are fine when they help; never invent
headings. If nothing in the curated set fits, say so plainly and suggest
the closest adjacent directory.

## DON'T SOUND LIKE AN LLM
Write the way a sharp, opinionated engineer writes a Slack message,
not the way ChatGPT writes a blog post. Concretely:

- **No em-dashes (—)**. Use a period, a comma, or parentheses. If you
  catch yourself reaching for `—`, restructure the sentence instead.
- **No hedging filler.** Cut "It's important to note that…", "It's
  worth mentioning…", "In essence…", "At the end of the day…",
  "Ultimately…", "That said…" used as conjunctive throat-clearing.
- **No "not just X, but Y" / "not only X, but also Y" patterns.**
  Pick the actual point and state it.
- **No marketing adjectives.** Avoid "seamless," "robust,"
  "powerful," "leverage," "delve," "comprehensive," "cutting-edge,"
  "robust," "elegant," "intricate," "nuanced" as filler.
- **No section transitions.** Skip "Furthermore," "Moreover," "In
  conclusion," "Additionally," at the start of sentences.
- **No meta-narration.** Don't say "Let me explain," "Here's the
  thing," "The key takeaway is," "In summary," or "To wrap up."
- **No performative empathy.** No "Great question," "That's a smart
  thing to think about," or any acknowledgment of the asker before
  the answer.
- **Don't over-bold.** Bold the load-bearing noun in a sentence at
  most. Don't bold whole sentences for emphasis.

A reader should not be able to tell an LLM wrote this.

## RESPONSE SHAPE
Before you write, think about what each part of your answer is doing.
Most answers want this skeleton — adapt it; don't pad it.

- **Opening (1 sentence, sometimes 2).** Lead with the answer. Not "great
  question," not a restatement, not a thesis statement — the actual
  recommendation. If the user asked "what should I read," the opening
  names what to read. If they asked "should I use X or Y," the opening
  picks one (or says "depends on…" and what it depends on).
- **Middle (the substance).** Back the opening with the reasoning a
  human would want to hear: the *why*, the trade-off, the one thing
  that's easy to miss. Cite by linking source titles inline; don't
  recap their contents — link them and tell the reader what they'll
  *take away*. Keep paragraphs short (2–4 sentences) and never longer
  than a screen of reading.
- **Close (1 sentence).** End with a forward-pointing thought the
  reader doesn't already have: a thing to watch out for when they
  try it, a question to ask themselves before they start, the next
  decision they'll face, a follow-up they should chase next. **Do
  not** end by redirecting them to a card, path, or link you already
  showed ("start with the first one above," "the path lays it out,"
  "check the snippet"). The cards/path are the destination — the
  close gives them something new to carry into it. If you can't
  think of anything worth adding, end after the middle and stop.
  Better a missing close than a hollow one.

For "junior" level, add one extra sentence of context anywhere it
helps (often in the opening or the close). For "senior" level, drop the
intro and get straight to the trade-off in the middle.

Length target: 1–3 short paragraphs of prose total, plus the close. If
you're heading past four, you're writing an essay — cut.

## PACING — MATCH THE RAMP TO THE READER
Every resource you suggest has a difficulty and a time cost. Spend that
budget like it belongs to the reader, not to you.

- Read the question for skill cues: phrases like "I'm new to," "just
  started," "first time," "what is" → newcomer. Phrases like "we're
  evaluating," "in prod," "scaling past," specific tool names, or
  trade-off framing → senior. The explicit `level` hint overrides; use
  it as a tiebreaker otherwise.
- For a newcomer, lead with the gentlest credible entry — a focused
  article, a quickstart, or the first 30 minutes of something larger.
  An 8-hour course can still appear later in a path, but it should not
  be the FIRST thing they click. Earn the long commitment.
- For a senior reader, skip the primer and jump to the decision-grade
  material — comparisons, deeper architecture pieces, production
  postmortems. Don't waste their time re-explaining basics.
- When you reach for a long resource (course, book, multi-hour talk),
  say what slice of it pays off for the question — "ch. 1–3 cover X,"
  "the first hour is the bit you want." Don't link a 12-hour course as
  if it's a 10-minute read.
- In a `path`, sort by difficulty: foundations first (cheap, short,
  newcomer-friendly), then intermediate, then the deep material. The
  reader should be able to stop early and still have learned something.

## WHEN A RICH BLOCK EARNS ITS PLACE
A block costs the reader attention; only use one when it does work
prose can't:

- Use **cards** when the answer is "here are 2–4 things worth looking at
  side-by-side" — comparable in shape, no ordering implied. If you'd
  naturally list them with "or," cards fit. If you'd naturally list them
  with "then," they don't — that's a path.
- Use **path** when ordering matters: each step builds on the prior one
  and skipping ahead would hurt. "Where do I start," "how do I get from
  A to B," "what's the ramp from junior to mid."
- Use **collection** (inside cards) when one tight cluster of links
  belongs together — e.g. four runbooks on the same topic.
- Use **snippet** when a small chunk of code or config IS the answer —
  not as a teaser, but as the thing the reader will paste.
- Use **tradeoff** when the prose has just framed a choice between 2–3
  options and the reader will only remember it if they can see the
  options side-by-side. "If you need X, go with A. If you need Y, go
  with B." Don't use it when one option is obviously right — that's
  prose with a recommendation, not a trade-off.
- Use **prereq** when the question is downstream of background the
  reader may be missing. Lists what they should already know before
  the rest of your answer pays off. Keep it to 2–5 items; if you'd need
  more, the reader is too far upstream and you should say so plainly.
- Use **gotcha** when there's a specific wrong pattern that's worth
  more than a sentence to warn about — usually because the wrong shape
  *looks* right. The crossed-out snippet is the point; if you can't
  show the wrong pattern in 1–8 lines, it's not a gotcha card.
- **Skip the block entirely** when prose + 1–3 inline links already
  delivers the answer. A redundant block is worse than no block.

Don't include more than one block per response. Pick the one that
carries the most weight; the rest goes inline.

## BLOCK SCHEMAS
You can include at most ONE `sdanswer` fenced JSON block per answer.

**Valid top-level `type` values**: `cards`, `path`, or any one of the
card kinds below as a standalone block — `article`, `snippet`,
`collection`, `tradeoff`, `prereq`, `gotcha`, `links`, `hint`,
`tip`, `warning`. The server wraps a bare card-kind as a single-card
row, so emitting `{"type": "warning", "body": "…"}` at the top level
is equivalent to wrapping it in `cards`. **`cards` and `path` are
themselves NOT valid card kinds** — those cannot nest inside `cards`.
Anything else (`"type": "list"`, `"type": "sources"`, etc.) is
dropped on the floor with nothing rendered — invent at your own risk.

- **cards** — a row of 2–4 cards. Each card is one of:
  - `article` — `{ "type": "article", "url": "<exact curated url>" }`.
    Title/byline/blurb are filled in server-side from the catalog.
  - `snippet` — an inline code snippet you wrote yourself: requires
    `title`, `description`, `snippet` (the code), optional `language` and
    optional `category`. Opens in a modal on click.
  - `collection` — a grouped set of curated articles: requires `title`,
    `description`, optional `category`, and `links` (2–6 exact curated URLs).
    Opens in a modal on click.

  - `tradeoff` — a side-by-side decision card. Requires `title` and
    `options` (a list of 2–3 entries, each `{ "label": "<short option
    name>", "bullets": ["<one consequence>", "<another>"] }`). Each
    option's bullets stay short (3–6 words ideal, ≤14 words max). This
    is a mnemonic, not a tutorial — the prose did the explaining.
  - `prereq` — what the reader should already know. Requires `items`
    (2–5 entries). Each item is `{ "label": "<topic>" }` for a free-form
    prerequisite, or `{ "label": "<topic>", "url": "<exact curated url>" }`
    to make it clickable to a curated source. Optional `title`
    (defaults to "Before this clicks, you should know"). URLs must
    come from `search_resources` / `read_source` — never invent.
  - `gotcha` — a "don't do this" card. Requires `title` (the warning,
    written as a directive: "Don't put X in Y"), `wrong` (the wrong
    code pattern, 1–8 lines), and optional `description` (one sentence
    on *why* it's wrong), `right` (one sentence on what to do instead
    — text, not code), and `language` (e.g. "python", "ts"). The wrong
    snippet renders crossed out.
  - `links` — a titled vertical list of labelled URLs. Requires
    `links`: a list of 2–8 entries, each `{ "label": "<short label>",
    "url": "<url>", "description": "<one short line>" }` (description
    optional). URLs may be curated (we'll pull the catalog title) or
    freeform — but bias toward curated. Optional `title` for a header
    above the list. Use this when the answer is "here's a small set of
    further reading," shorter than a `path` and looser than a `cards`
    row. Don't combine with a `cards` block — pick one.
  - `hint` / `tip` / `warning` — single-paragraph callouts, tinted by
    kind. Each requires a `body` (the paragraph) and optional `title`
    (one short line). Use **hint** for sideline context the reader
    might want ("if you're new to X, start with…"). Use **tip** for
    actionable, "here's the cheat code" practical advice. Use
    **warning** for cautions and footguns. One sentence to one short
    paragraph each — these are calls-to-attention, not essays. If you
    need code, that's a `snippet` or `gotcha`, not a callout.

  `category` is **only** for `snippet` and `collection` cards. Valid values:
  `agentic-coding-courses`, `system-architecture`, `infrastructure-hosting`,
  `software-delivery`, `production-operations` — those are the five
  Smarter Dev directories. **Only set `category` when one of those slugs
  is a genuinely good fit for the snippet/collection in the context of
  the answer.** If none of the five fits cleanly, leave `category` off
  entirely — the card renders fine without a chip. Don't reach: a
  vaguely-related slug is worse than no slug, because the chip lies
  about where this thing lives in the catalog.
- **path** — `{ "type": "path", "links": [...] }`. Step count is
  determined by the material — a tight ramp-up may be 3 steps; a deep
  one may be 8. Don't reflexively pick 5. Order steps so each one
  builds on the prior: foundations and prerequisites first, then
  intermediate, then advanced.

  Each entry in `links` is an object: `{ "url": "<exact url>",
  "description": "<one sentence on why it's here and what to take from
  it>", "estimate": "<time>" }`. **Every step must include an `estimate`** —
  a focused realistic time-to-complete for *that one step*, not the whole
  path. Use formats like `"30m"`, `"1h"`, `"1h 30m"`, `"2h"`, `"45m"`.
  Server-side we sum the per-step estimates into the path's total —
  do **not** set a top-level `estimate` on the path block. Bare URL
  strings are still accepted but skip the description/estimate
  affordances, so always prefer the object form. Descriptions are
  strongly recommended on every step.

  **Non-curated URLs in a path require a `title` field.** Curated URLs
  (from `search_resources`) hydrate title/byline/blurb from the
  catalog automatically. If a step points at a URL the gap-filler
  surfaced from the open web — i.e. a URL that came from the
  pre-fetched research bundle but is NOT one of the catalog
  entries — include a `title` field so the rendered card has a
  human-readable header instead of just a hostname:
  `{ "url": "<web url>", "title": "<the source's title>",
  "description": "…", "estimate": "…" }`.

  **Introduce path resources in the prose before the path.** The path
  card is the ramp; the prose is the entrance. Every distinct
  resource the path links must either (a) already be cited inline in
  the prose above OR (b) get a short lead-in sentence right before
  the path that names what's new ("There's also a write-up worth
  reading first — …"). The reader should never reach the path and
  encounter a step they have no context for.

  Estimate honestly: a 30-minute article is 30 minutes, a 2-hour
  video is 2 hours, a chapter excerpt of a book is the chapter, not
  the whole book. If a step is "ch. 1–3 of <book>," estimate the
  three chapters, not the full book.

  **Then pad it.** Your first-instinct estimate is the speed-reader,
  already-fluent pace — the reader is *learning*, which means they'll
  stop to think, re-read passages, run the code, click into footnotes
  and tangents, and lose time switching contexts. As a rule of thumb,
  multiply your gut estimate by ~1.5–2× before writing it down. A
  "20-minute article" you skim becomes 30–40 minutes for someone
  encountering the ideas for the first time. A "2-hour video" with
  hands-on portions is closer to 3 hours of real wall-clock time. Err
  generous: an estimate that's 20 minutes too long costs nothing, but
  an estimate that's 20 minutes too short trains the reader to
  distrust the whole path.

  A path's steps do **not** need to be sources you fully `read_source`d.
  Run extra `search_resources(query)` calls to surface candidates and pull
  URLs straight from the search hits — title + blurb is enough to place a
  step. Reserve `read_source` for the 1–3 sources you'll paraphrase in
  prose.

Rules:
- ONLY use URLs returned by `search_resources` or `read_source` — never invent.
- Don't duplicate prose and block content; the block IS the recommendation.

Example cards block:

```sdanswer
{ "type": "cards", "cards": [
  { "type": "article", "url": "https://example.com/postgres-replication" },
  { "type": "snippet", "title": "Set up logical replication",
    "description": "Three lines on the primary, one on the replica.",
    "language": "sql",
    "snippet": "ALTER SYSTEM SET wal_level = logical;\\nSELECT pg_create_logical_replication_slot('s1', 'pgoutput');\\n-- on replica:\\nCREATE SUBSCRIPTION s1 CONNECTION '…' PUBLICATION p1;",
    "category": "production-operations" }
] }
```

Example path blocks (length varies with the topic — pick whatever fits):

A short ramp (3 steps — sometimes the topic just doesn't need more):

```sdanswer
{ "type": "path", "links": [
  { "url": "https://example.com/feature-flags-101",
    "description": "What feature flags actually are and the three patterns worth knowing.",
    "estimate": "45m" },
  { "url": "https://example.com/rollout-strategies",
    "description": "Percentage rollouts, targeting rules, kill switches — when each one earns its place.",
    "estimate": "1h 15m" },
  { "url": "https://example.com/flag-debt",
    "description": "Last rung — how flags rot if you don't have a cleanup discipline.",
    "estimate": "30m" }
] }
```

A deeper one (7 steps — used here because the topic genuinely needs the layering):

```sdanswer
{ "type": "path", "links": [
  { "url": "https://example.com/queue-fundamentals",
    "description": "Start here — what queues solve and the vocabulary the rest of the path uses.",
    "estimate": "1h 30m" },
  { "url": "https://example.com/kafka-vs-rabbit",
    "description": "Decide which broker fits your shape before learning either one in depth.",
    "estimate": "1h" },
  { "url": "https://example.com/kafka-quickstart",
    "description": "Hands-on with a local cluster. Skip the producer/consumer code samples on your first read.",
    "estimate": "3h" },
  { "url": "https://example.com/partitioning-and-keys",
    "description": "Why partitions exist and how key choice quietly decides your throughput ceiling.",
    "estimate": "2h" },
  { "url": "https://example.com/exactly-once-semantics",
    "description": "Now that you've shipped a producer, this is the failure-mode reading that pays off.",
    "estimate": "2h 30m" },
  { "url": "https://example.com/schema-evolution",
    "description": "What breaks when your payload changes and how to roll forward without a stop-the-world deploy.",
    "estimate": "1h 30m" },
  { "url": "https://example.com/observability-for-streams",
    "description": "Last rung — what to wire up before you let this anywhere near prod.",
    "estimate": "1h 30m" }
] }
```

## HOW TO RESEARCH
- Call `search_resources(query)` as many times as you need. Three well-aimed
  queries is the common case, but if you're building a path or comparing
  multiple tools, do more. Search is cheap; cast a wide net.
- `read_source(url)` is also cheap — curated URLs are served from a warm
  Postgres cache (no network call). Read as many sources as you need to
  feel confident, especially before recommending one in prose. Don't dump
  raw body text into the answer; paraphrase.
- For citation cards, path steps, and collection links, the title + blurb
  from `search_resources` is already enough — no need to read just to link.

## PLAN BEFORE YOU WRITE
Research and authoring are different jobs. When you finish gathering and
before you write a single sentence, run this short planning beat in
your head:

1. **Curate.** Out of everything `search_resources` returned, pick the
   2–6 resources you'll actually show the reader. The rest were
   useful background; they don't earn airtime. Drop anything that's
   tangential, redundant, or weaker than what you've already chosen.

   **Every kept resource must actively support the response you're
   planning to write.** "Adjacent," "kind of related," or "the best of
   what came back" doesn't qualify — the reader sees the resource and
   assumes you're vouching for it as a *good answer to their question*.
   For each candidate, ask: does this directly help with what they
   asked? If yes, keep it. If no, you have exactly two options:
   - Search again with a better query — a missing angle is often
     just a `search_resources` call away. Cast a wider net before
     settling.
   - Drop the resource entirely. A shorter answer with a tight
     curated set beats a padded answer with weak filler.

   It's fine — and sometimes correct — to ship an answer with zero
   curated resources if nothing in the catalog genuinely fits. Say so
   plainly and point at the closest adjacent directory; don't reach.
2. **Order.** Decide the sequence — what comes first, what comes last,
   what carries the most weight. If the answer is a path, foundation
   → intermediate → deep. If it's cards, lead with the strongest. If
   it's prose with inline links, the first link mentioned is the
   first recommendation.
3. **Pick the block (or no block).** Given the curated set, decide
   which rich block — if any — carries the most weight. One block max;
   the rest stays inline. Skip the block entirely if prose + a couple
   inline links already does the job.
4. **Lock the opening.** Write the opening sentence in your head — the
   actual recommendation, not a restatement of the question. If you
   can't say the recommendation cleanly in one sentence, your curation
   isn't tight enough; go back to step 1.
5. **Lock the close.** Decide the forward-pointing thought the reader
   doesn't yet have. *Not* "see the path above," *not* "start with the
   first card" — a new thought. If nothing comes, plan to end after
   the middle and stop.
6. **Build the middle as connective tissue.** Every resource you kept
   in step 1 should be named in the answer — either in the rich block
   or as an inline link in the prose. If a resource didn't fit
   anywhere, you didn't actually need it; drop it from the curated
   set. If a resource only appears in the rich block, the prose
   should still gesture at *why* it's there.

Output just the final answer markdown — no preamble, no "based on my
research" filler, no enumeration of the planning beat itself.
"""


_RESEARCHER_PROMPT = """\
You're a novice tasked with researching a topic using the Smarter Dev document corpus. Each document is represented by a URL that you can use to get the full document content. You must carefully copy the URLs provided by the search tool to read the documents you want; guessing URLs only wastes time and can return errors.

You should look for documents you can cite. For each citation, you must have read the document, then provide a brief (single sentence) purpose for the citation, the verbatim excerpt (1-4 sentences), and the exact (copy carefully) URL for reference. Each source can be cited multiple times. You'll want to shoot for 4-8 citations in total.

You'll also want to note down further reading that would likely add depth but wasn't directly within the scope of the topic. For each, carefully copy over the URL and provide a blurb explaining why you think it would be relevant for further reading. Shoot for 2 to 5 uncited further reading entries.

If, after at least two distinct search queries for a particular concept, you can't find a relevant document in the corpus, report the gap in `gaps` (with the missing `concept`, the `tried_queries` you ran, and what kind of source would be `needed`) and move on. Do not keep searching endlessly, and do not invent citations to paper over a gap.

**Tool budget: at most 5 `search_resources` calls and at most 5 `read_source` calls per run.** Plan your queries before firing them. If you've burned your budget and still don't have enough citations, finalize with what you have — better a slightly thinner answer than an over-thrashed one.

**[CRITICAL] URLs NOT PRESENT IN search_resources RESULTS WILL NOT OPEN**
All URLs passed to `read_source` must have been present in the `search_resources` results or it will return an error. This is to prevent abuse."""


_GAP_FILLER_PROMPT = """\
You are filling specific gaps in a curated document corpus. The user turn lists `gaps`, each describing a concept the corpus didn't cover. For each gap, your job is:

1. Run exactly 2 `web_search` queries targeting **primary or authoritative** sources for the gap's concept. Prefer official documentation (e.g., postgresql.org/docs, kubernetes.io/docs), RFCs, canonical academic papers, or domain-expert deep-dives. Avoid SEO blogspam, vendor marketing, and listicles.
2. Skim the search results and pick the **single highest-quality** URL that genuinely fills the gap.
3. Read that URL with `read_url`, then write one `GapCitation` for that gap. The `excerpt` must be a verbatim 1-4 sentence quote from the source you actually read.

Return exactly one `GapCitation` per input gap.

**[CRITICAL] URLs NOT PRESENT IN web_search RESULTS WILL NOT READ**
All URLs passed to `read_url` must have been returned by `web_search` in this run. URLs you didn't get from `web_search` will error. Do not guess URLs."""


# ---------------------------------------------------------------------------
# Typed schemas (researcher + gap-filler outputs)
# ---------------------------------------------------------------------------


class Excerpt(BaseModel):
    """A cited passage from a curated document."""

    purpose: str = Field(
        ...,
        description=(
            "One short sentence stating why this citation matters for "
            "the user's question."
        ),
    )
    excerpt: str = Field(
        ...,
        description="Verbatim excerpt (1-4 sentences) from the document.",
    )
    source_url: str = Field(
        ...,
        description=(
            "URL copied verbatim from a `search_resources` hit you "
            "opened. URLs you didn't see in `search_resources` results "
            "this run will be dropped."
        ),
    )


class FurtherReading(BaseModel):
    """A document worth pointing the reader at for a deeper dive after
    the answer, but not directly cited."""

    source_url: str = Field(
        ...,
        description=(
            "URL copied verbatim from a `search_resources` hit. Must "
            "come from this run's `search_resources` results."
        ),
    )
    blurb: str = Field(
        ...,
        description=(
            "One sentence on why this is worth a deeper look in the "
            "context of the user's question."
        ),
    )


class Gap(BaseModel):
    """A concept the user's question implicates that the corpus didn't
    cover after at least two distinct search queries."""

    concept: str = Field(
        ...,
        description=(
            "The concept or sub-topic you couldn't find a relevant "
            "document for."
        ),
    )
    tried_queries: list[str] = Field(
        ...,
        description=(
            "The search queries you actually ran while looking for "
            "this concept (at least two distinct attempts)."
        ),
    )
    needed: str = Field(
        ...,
        description=(
            "One sentence describing what kind of source would fill "
            "this gap."
        ),
    )


class ResearchOutput(BaseModel):
    """Structured researcher payload — distilled excerpts the author
    weaves into prose, plus further-reading and any gaps in the
    catalog."""

    excerpts: list[Excerpt] = Field(default_factory=list)
    further_reading: list[FurtherReading] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)


class GapCitation(BaseModel):
    """The single best citation the web searcher found for one gap."""

    gap_concept: str = Field(
        ...,
        description=(
            "The `concept` of the gap this citation fills — copy "
            "verbatim from the input gap."
        ),
    )
    source_title: str = Field(
        ...,
        description="Title of the cited source (verbatim from web_search).",
    )
    source_url: str = Field(
        ...,
        description=(
            "URL of the cited source. Must be one of the URLs returned "
            "by `web_search` in this run."
        ),
    )
    excerpt: str = Field(
        ...,
        description="1-4 sentence verbatim excerpt that addresses the gap.",
    )
    rationale: str = Field(
        ...,
        description=(
            "One sentence on why this is an authoritative source for "
            "the gap (e.g., 'official Postgres docs', 'canonical paper "
            "by X')."
        ),
    )


class GapFillerOutput(BaseModel):
    """One citation per input gap, drawn from the open web."""

    citations: list[GapCitation] = Field(default_factory=list)


# A list of dicts (one per tool hit) accumulated during a single run.
# Kept for legacy callers; new pipeline does not depend on it for
# citations (citations are surfaced via the structured Pydantic
# outputs).
_HITS: ContextVar[Optional[list[dict]]] = ContextVar("resource_agent_hits", default=None)


def begin_run() -> list[dict]:
    """Reset the hit log for a new agent invocation. Returns the list to drain later."""
    bucket: list[dict] = []
    _HITS.set(bucket)
    return bucket


def _record_hit(hit: dict) -> None:
    bucket = _HITS.get()
    if bucket is not None:
        bucket.append(hit)


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------


def _build_google_model(model_id: str) -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


def _build_openai_model(model_id: str) -> OpenAIResponsesModel:
    # gpt-5 family with reasoning_effort + function tools requires the
    # Responses API (`/v1/responses`); chat-completions rejects the
    # combo.
    api_key = os.getenv("OPENAI_API_KEY") or ""
    return OpenAIResponsesModel(model_id, provider=OpenAIProvider(api_key=api_key))


def _researcher_model_settings() -> OpenAIResponsesModelSettings:
    return OpenAIResponsesModelSettings(openai_reasoning_effort="medium")


def _gap_filler_model_settings() -> GoogleModelSettings:
    return GoogleModelSettings(
        google_thinking_config={"thinking_level": "LOW"},
    )


def _author_model_settings() -> GoogleModelSettings:
    return GoogleModelSettings(
        google_thinking_config={"thinking_level": "LOW"},
    )


@dataclass
class RunDeps:
    """Per-run dependencies injected into tool calls via ``RunContext.deps``.

    Carries the identifiers needed to push real-time ``agent_tool_event``
    notifications back to the asker's browser. ``seen_urls`` is the
    gap-filler's per-run allowlist: `web_search` populates it, `read_url`
    enforces it. The researcher and author tools don't touch it.
    """

    conversation_id: str
    owner_user_id: str
    seen_urls: set[str] = dataclasses.field(default_factory=set)


def _build_deps(ctx: ResumeContext) -> RunDeps:
    return RunDeps(
        conversation_id=str(ctx.deps_ref.get("conversation_id", "")),
        owner_user_id=str(ctx.deps_ref.get("owner_user_id", "")),
    )


# ---------------------------------------------------------------------------
# Skrift agents — one per pipeline stage
# ---------------------------------------------------------------------------


researcher_agent = skrift.Agent(
    _build_openai_model(RESEARCHER_MODEL),
    name=RESEARCHER_AGENT_NAME,
    system_prompt=_RESEARCHER_PROMPT,
    output_type=ResearchOutput,
    model_settings=_researcher_model_settings(),
    deps_type=RunDeps,
    deps_factory=_build_deps,
)

gap_filler_agent = skrift.Agent(
    _build_google_model(GAP_FILLER_MODEL),
    name=GAP_FILLER_AGENT_NAME,
    system_prompt=_GAP_FILLER_PROMPT,
    output_type=GapFillerOutput,
    model_settings=_gap_filler_model_settings(),
    deps_type=RunDeps,
    deps_factory=_build_deps,
)

author_agent = skrift.Agent(
    _build_google_model(AUTHOR_MODEL),
    name=AUTHOR_AGENT_NAME,
    system_prompt=_SYSTEM_PROMPT,
    model_settings=_author_model_settings(),
    deps_type=RunDeps,
    deps_factory=_build_deps,
)


async def _emit_tool_event(
    deps: RunDeps | None,
    *,
    tool: str,
    label: str,
    summary: str,
) -> None:
    """Push an `agent_tool_event` to the asker's open answer page.

    Silently no-ops when no owner is set (e.g. tests or replay paths). Any
    notification failure is logged but never propagated — tool callers must
    not see clipboard/SSE issues.
    """
    if not deps or not deps.owner_user_id:
        return
    try:
        await notify_user(
            deps.owner_user_id,
            "agent_tool_event",
            conversation_id=deps.conversation_id,
            tool=tool,
            label=label,
            summary=summary,
        )
    except Exception:  # noqa: BLE001
        logger.exception("agent_tool_event notify_user failed")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_READ_CACHE: dict[str, tuple[float, str]] = {}
_READ_TTL_SECONDS = 60 * 60  # 60 minutes, in-memory fallback for non-curated URLs
_READ_MAX_CHARS = 10_000
_READ_DB_TTL_DAYS = 30  # how long a stored jina body is considered fresh


@researcher_agent.tool
async def search_resources(
    ctx: RunContext[RunDeps], query: str, limit: int = 8
) -> list[dict]:
    """Search the curated resource catalog.

    Use this first. Returns a ranked list of candidate sources and tools,
    each with a title/byline/blurb you can read before deciding which (if any)
    to fetch via ``read_source``. Aim for 1–3 well-chosen queries per run.

    Args:
        query: A short phrase or keyword. e.g. "postgres replication",
            "feature flags", "incident response runbooks".
        limit: Max number of hits to return. Default 8, cap 20.

    Returns:
        A list of dicts: ``[{kind, title, url, byline, blurb, learning_type,
        directory, category}]``. ``kind`` is "source" for curated articles
        and "tool" for indexed tools.
    """
    from sqlalchemy import text

    from smarter_dev.shared.config import get_settings
    from smarter_dev.shared.database import (
        convert_postgres_url_for_asyncpg,
    )
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    if not query or not query.strip():
        return []
    limit = max(1, min(limit, 20))

    settings = get_settings()
    engine = create_async_engine(
        convert_postgres_url_for_asyncpg(settings.effective_database_url),
        poolclass=NullPool,
    )
    # Word-level matching: `to_tsvector @@ plainto_tsquery` matches if any
    # query word appears (with stemming) in the title or blurb. Trigram
    # similarity gates a fuzzy second pass so phrases like "postgres queue"
    # still surface entries titled "Postgres SKIP LOCKED queues".
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET search_path TO skrift, public"))
            sources_sql = text(
                """
                SELECT s.title, s.url, s.byline, s.blurb, s.learning_type,
                       d.slug AS directory, COALESCE(c.slug, '') AS category,
                       GREATEST(
                         similarity(s.title, :q),
                         similarity(coalesce(s.blurb, ''), :q),
                         CASE WHEN to_tsvector('english',
                                coalesce(s.title,'') || ' ' || coalesce(s.blurb,'')
                              ) @@ plainto_tsquery('english', :q) THEN 0.4 ELSE 0 END
                       ) AS score
                FROM resource_sources s
                LEFT JOIN resource_directory_spine dsp ON dsp.source_id = s.id
                LEFT JOIN resource_directories d ON d.id = dsp.directory_id
                LEFT JOIN resource_tool_sources ts ON ts.source_id = s.id
                LEFT JOIN resource_tools t ON t.id = ts.tool_id
                LEFT JOIN resource_categories c ON c.id = t.category_id
                WHERE
                  to_tsvector('english',
                    coalesce(s.title,'') || ' ' || coalesce(s.blurb,'')
                  ) @@ plainto_tsquery('english', :q)
                  OR similarity(s.title, :q) > 0.15
                  OR similarity(coalesce(s.blurb, ''), :q) > 0.15
                ORDER BY score DESC NULLS LAST, s.first_indexed_at DESC
                LIMIT :limit
                """
            )
            tools_sql = text(
                """
                SELECT t.name AS title, t.url, '' AS byline, t.blurb,
                       'Tool' AS learning_type, d.slug AS directory, c.slug AS category,
                       GREATEST(
                         similarity(t.name, :q),
                         similarity(coalesce(t.blurb, ''), :q),
                         CASE WHEN to_tsvector('english',
                                coalesce(t.name,'') || ' ' || coalesce(t.blurb,'')
                              ) @@ plainto_tsquery('english', :q) THEN 0.4 ELSE 0 END
                       ) AS score
                FROM resource_tools t
                JOIN resource_categories c ON c.id = t.category_id
                JOIN resource_directories d ON d.id = c.directory_id
                WHERE
                  to_tsvector('english',
                    coalesce(t.name,'') || ' ' || coalesce(t.blurb,'')
                  ) @@ plainto_tsquery('english', :q)
                  OR similarity(t.name, :q) > 0.15
                  OR similarity(coalesce(t.blurb, ''), :q) > 0.15
                ORDER BY score DESC NULLS LAST, t.name
                LIMIT :tool_limit
                """
            )
            src_rows = (
                await conn.execute(
                    sources_sql, {"q": query, "limit": limit}
                )
            ).mappings().all()
            tool_rows = (
                await conn.execute(
                    tools_sql,
                    {"q": query, "tool_limit": max(2, limit // 2)},
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    hits: list[dict] = []
    seen: set[str] = set()
    for row in src_rows:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        hits.append(
            {
                "kind": "source",
                "title": row["title"],
                "url": row["url"],
                "byline": row["byline"] or "",
                "blurb": row["blurb"] or "",
                "learning_type": row["learning_type"],
                "directory": row["directory"],
                "category": row["category"] or "",
            }
        )
    for row in tool_rows:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        hits.append(
            {
                "kind": "tool",
                "title": row["title"],
                "url": row["url"],
                "byline": "",
                "blurb": row["blurb"] or "",
                "learning_type": "Tool",
                "directory": row["directory"],
                "category": row["category"] or "",
            }
        )

    hits = hits[:limit]
    for hit in hits:
        _record_hit({"source": "search", **hit})
        if hit.get("url"):
            ctx.deps.seen_urls.add(hit["url"])

    await _emit_tool_event(
        ctx.deps,
        tool="search_resources",
        label=query,
        summary=f"{len(hits)} hit" + ("s" if len(hits) != 1 else ""),
    )
    return hits


@researcher_agent.tool
async def read_source(ctx: RunContext[RunDeps], url: str) -> str:
    """Read the full text of a curated resource.

    Reads come from a tiered cache:

    1. ``resource_sources.jina_content`` — durable Postgres cache. Set by the
       precrawl (``scripts/warm_jina_cache.py``) and refreshed on a 30-day
       TTL. The agent should reach for ``read_source`` freely because curated
       URLs are almost always already hot here.
    2. In-process LRU (60 min) — covers any non-curated URL the agent might
       fetch (rare given the prompt's "only curated URLs" rule).
    3. Live Jina Reader — fallback on miss; writes back into whichever cache
       layer applies before returning.

    Args:
        url: The exact ``url`` field from a ``search_resources`` hit.

    Returns:
        Plain-text body (title + content concatenated), truncated to 10k chars.
        On failure, returns a short error string starting with ``"[error]"``.
    """
    if not url:
        return "[error] no URL provided"

    if url not in ctx.deps.seen_urls:
        # Enforce the per-run allowlist: every URL the model passes to
        # `read_source` must have been returned by `search_resources`
        # in this run. Stops URL fabrication cold.
        await _emit_tool_event(
            ctx.deps,
            tool="read_source",
            label=url,
            summary="[error] URL not in search results",
        )
        return (
            "[error] URL not in search_resources results from this run. "
            "Run `search_resources` first and use a URL from the results."
        )

    body: str | None = None

    db_body = await _read_from_db_cache(url)
    if db_body is not None:
        _record_hit({"source": "read", "url": url})
        body = db_body
    else:
        now = time.time()
        cached = _READ_CACHE.get(url)
        if cached and (now - cached[0]) < _READ_TTL_SECONDS:
            _record_hit({"source": "read", "url": url})
            body = cached[1]
        else:
            async with httpx.AsyncClient() as client:
                result = await jina_read(client, url)

            if "error" in result:
                # Emit a failing tool event so the UI can show the error
                # alongside the chip rather than swallowing silently.
                await _emit_tool_event(
                    ctx.deps,
                    tool="read_source",
                    label=url,
                    summary=f"[error] {result['error']}",
                )
                return f"[error] {result['error']}"

            title = result.get("title", "") or ""
            content = result.get("content", "") or ""
            body = f"{title}\n\n{content}".strip()[:_READ_MAX_CHARS]

            stored = await _write_to_db_cache(url, body)
            if not stored:
                # Non-curated URL — fall back to the in-process LRU.
                _READ_CACHE[url] = (now, body)

            _record_hit({"source": "read", "url": url})

    label = body.split("\n", 1)[0].strip() if body else url
    if len(label) > 80:
        label = label[:79].rstrip() + "…"
    await _emit_tool_event(
        ctx.deps,
        tool="read_source",
        label=label or url,
        summary=url,
    )
    return body


# ---------------------------------------------------------------------------
# Durable Jina cache (resource_sources.jina_content / jina_fetched_at)
# ---------------------------------------------------------------------------


def _open_db_session():
    """Open a short-lived AsyncSession bound to the main DB.

    ``read_source`` runs inside an in-progress request, but the request's
    ``db_session`` isn't reachable from a ``@tool_plain`` (Skrift hands the
    tool a plain function, not a context-aware one). Opening our own session
    keeps the tool decoupled and avoids interfering with the request's
    transaction state.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from smarter_dev.shared.config import get_settings
    from smarter_dev.shared.database import convert_postgres_url_for_asyncpg

    settings = get_settings()
    engine = create_async_engine(
        convert_postgres_url_for_asyncpg(settings.effective_database_url),
        poolclass=NullPool,
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    return engine, Session()


async def _read_from_db_cache(url: str) -> str | None:
    """Return cached Jina body if fresh, else None. None also means non-curated."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text as sql_text

    engine, session = _open_db_session()
    try:
        async with session as s:
            await s.execute(sql_text("SET search_path TO skrift, public"))
            row = (
                await s.execute(
                    sql_text(
                        "SELECT jina_content, jina_fetched_at "
                        "FROM resource_sources WHERE url = :url"
                    ),
                    {"url": url},
                )
            ).first()
    finally:
        await engine.dispose()

    if row is None:
        return None  # Non-curated URL — caller falls back to in-memory + Jina.
    content, fetched_at = row
    if not content or not fetched_at:
        return None  # Curated but not warmed yet — caller will warm it.
    if datetime.now(timezone.utc) - fetched_at > timedelta(days=_READ_DB_TTL_DAYS):
        return None  # Stale — caller will refresh.
    return content


async def _write_to_db_cache(url: str, body: str) -> bool:
    """Persist a freshly fetched Jina body. Returns True if the URL was curated."""
    from datetime import datetime, timezone

    from sqlalchemy import text as sql_text

    engine, session = _open_db_session()
    try:
        async with session as s:
            await s.execute(sql_text("SET search_path TO skrift, public"))
            result = await s.execute(
                sql_text(
                    "UPDATE resource_sources "
                    "SET jina_content = :body, jina_fetched_at = :now "
                    "WHERE url = :url"
                ),
                {
                    "body": body,
                    "now": datetime.now(timezone.utc),
                    "url": url,
                },
            )
            await s.commit()
            return result.rowcount > 0
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Gap-filler tools (open-web search + read, with per-run URL allowlist)
# ---------------------------------------------------------------------------


@gap_filler_agent.tool
async def web_search(ctx: RunContext[RunDeps], query: str) -> list[dict]:
    """Search the open web for authoritative/primary sources.

    Returns up to 5 English-language hits, each with `title`, `url`,
    and `description`. To read a hit's full body, call `read_url` with
    the URL.
    """
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            raw = await brave_search(
                client,
                query,
                num_results=5,
                country="US",
                search_lang="en",
                ui_lang="en-US",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("brave_search raised for %s: %s", query, exc)
        await _emit_tool_event(
            ctx.deps,
            tool="web_search",
            label=query,
            summary=f"[error] {exc}",
        )
        return []

    hits: list[dict] = []
    for h in raw:
        if "error" in h or not h.get("url"):
            continue
        hits.append(
            {
                "title": h.get("title", "") or "",
                "url": h["url"],
                "description": h.get("description", "") or "",
            }
        )
        ctx.deps.seen_urls.add(h["url"])

    await _emit_tool_event(
        ctx.deps,
        tool="web_search",
        label=query,
        summary=f"{len(hits)} hit" + ("s" if len(hits) != 1 else ""),
    )
    logger.debug("web_search %r → %d hits in %.2fs", query, len(hits), time.monotonic() - t0)
    return hits


@gap_filler_agent.tool
async def read_url(ctx: RunContext[RunDeps], url: str) -> str:
    """Read the full body of a URL.

    The URL must have been returned by `web_search` in this run;
    URLs you didn't get from `web_search` will error. Returns the
    plain-text body (truncated to ~10k chars) or an ``"[error] …"``
    string on failure.
    """
    if not url:
        return "[error] no URL provided"

    if url not in ctx.deps.seen_urls:
        await _emit_tool_event(
            ctx.deps,
            tool="read_url",
            label=url,
            summary="[error] URL not in web_search results",
        )
        return (
            "[error] URL not in web_search results from this run. "
            "Run `web_search` first and use a URL from the results."
        )

    # Reuse the same tiered cache the researcher uses: durable DB cache
    # first, in-process LRU second, live Jina Reader last. Web URLs
    # won't be in `resource_sources` so the DB-cache write is a no-op
    # for them (handled by `_write_to_db_cache` returning False).
    body: str | None = None
    db_body = await _read_from_db_cache(url)
    if db_body is not None:
        body = db_body
    else:
        now = time.time()
        cached = _READ_CACHE.get(url)
        if cached and (now - cached[0]) < _READ_TTL_SECONDS:
            body = cached[1]
        else:
            async with httpx.AsyncClient() as client:
                result = await jina_read(client, url)
            if "error" in result:
                await _emit_tool_event(
                    ctx.deps,
                    tool="read_url",
                    label=url,
                    summary=f"[error] {result['error']}",
                )
                return f"[error] {result['error']}"

            title = result.get("title", "") or ""
            content = result.get("content", "") or ""
            body = f"{title}\n\n{content}".strip()[:_READ_MAX_CHARS]
            stored = await _write_to_db_cache(url, body)
            if not stored:
                _READ_CACHE[url] = (now, body)

    label = body.split("\n", 1)[0].strip() if body else url
    if len(label) > 80:
        label = label[:79].rstrip() + "…"
    await _emit_tool_event(
        ctx.deps,
        tool="read_url",
        label=label or url,
        summary=url,
    )
    return body


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


def _format_gap_payload(
    question: str, gaps: list[Gap]
) -> str:
    """Format the researcher's gaps into a user turn for the gap-filler."""
    return (
        "Original user question:\n\n"
        f"{question}\n\n"
        "Curated-corpus gaps to fill (one citation each):\n\n"
        "```json\n"
        f"{json.dumps([g.model_dump() for g in gaps], indent=2, ensure_ascii=False)}\n"
        "```\n"
    )


def _build_author_payload(
    research: ResearchOutput,
    web_citations: list[GapCitation],
) -> dict:
    """Merge curated excerpts + web citations into the author payload.

    The author sees a unified shape: every excerpt has a `purpose`,
    `excerpt`, `source_url`. Web citations become additional excerpts
    keyed by their gap concept. Further-reading flows through unchanged.
    """
    excerpts: list[dict] = []
    for ex in research.excerpts:
        excerpts.append({
            "purpose": ex.purpose,
            "excerpt": ex.excerpt,
            "source_url": ex.source_url,
        })
    for c in web_citations:
        excerpts.append({
            "purpose": c.gap_concept,
            "excerpt": c.excerpt,
            "source_url": c.source_url,
            "source_title": c.source_title,
        })
    further_reading: list[dict] = [
        {"source_url": fr.source_url, "blurb": fr.blurb}
        for fr in research.further_reading
    ]
    return {"excerpts": excerpts, "further_reading": further_reading}


def _build_author_user_turn(question: str, payload: dict) -> str:
    return (
        "Pre-fetched research (cite using these URLs only — every URL "
        "is from the verified catalog or a vetted web source; do not "
        "invent any):\n\n"
        "- `excerpts`: passages with citations that directly answer "
        "the question. `purpose` is why the citation matters; "
        "`excerpt` is the verbatim supporting text.\n"
        "- `further_reading`: related sources for the reader to dig "
        "into after the answer.\n\n"
        "```json\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n"
        "```\n\n"
        "User question:\n\n"
        f"{question}"
    )


async def _await_text_result(session) -> str:
    """Extract the author's markdown answer from a Skrift session."""
    result = await session.result()
    if isinstance(result, str):
        return result
    return getattr(result, "output", None) or str(result)


async def _await_typed_result(session, model_cls):
    """Extract a typed Pydantic result, defensively handling the
    deferred-tool-request union Skrift wraps `output_type` with."""
    result = await session.result()
    if isinstance(result, model_cls):
        return result
    # Fallback paths if Skrift returns the raw pydantic-ai run-result
    inner = getattr(result, "output", None)
    if isinstance(inner, model_cls):
        return inner
    raise RuntimeError(
        f"Expected {model_cls.__name__} from agent, got {type(result).__name__}"
    )


async def run_resources_pipeline(
    question: str,
    *,
    message_history: Optional[list],
    actor: str,
    conversation_id: str,
    owner_user_id: str,
) -> str:
    """Run the three-stage pipeline (researcher → gap-filler → author)
    as a single user-visible agent run. Tool events from every stage
    are emitted into the same `agent_tool_event` stream so the user
    sees one continuous chip timeline. Returns the author's markdown.

    Behavior on failure:
    - Researcher failure: re-raised so the caller fires `agent_run_error`.
    - Gap-filler failure: logged + a degraded `tool="gap_filler"` chip
      is emitted; pipeline continues to the author with curated-only
      excerpts.
    - Author failure: re-raised so the caller fires `agent_run_error`.
    """
    deps_ref = {
        "conversation_id": conversation_id,
        "owner_user_id": owner_user_id,
    }
    # Minimal deps copy the orchestrator uses to emit its own degraded
    # status chips when a stage misbehaves.
    orchestrator_deps = RunDeps(
        conversation_id=conversation_id,
        owner_user_id=owner_user_id,
    )

    # ── Stage 1: Researcher ──────────────────────────────────────────────
    researcher_session = await asyncio.wait_for(
        researcher_agent.run(
            question,
            message_history=message_history,
            actor=actor,
            deps_ref=deps_ref,
        ),
        timeout=_RESEARCHER_TIMEOUT_S,
    )
    research = await _await_typed_result(researcher_session, ResearchOutput)

    # ── Stage 2: Gap-filler (only if the researcher reported gaps) ──────
    web_citations: list[GapCitation] = []
    if research.gaps:
        try:
            gap_session = await asyncio.wait_for(
                gap_filler_agent.run(
                    _format_gap_payload(question, research.gaps),
                    actor=actor,
                    deps_ref=deps_ref,
                ),
                timeout=_GAP_FILLER_TIMEOUT_S,
            )
            gap_output = await _await_typed_result(
                gap_session, GapFillerOutput
            )
            web_citations = list(gap_output.citations)
        except Exception:  # noqa: BLE001
            logger.exception("gap_filler stage failed; continuing without web cites")
            await _emit_tool_event(
                orchestrator_deps,
                tool="gap_filler",
                label=(
                    f"{len(research.gaps)} gap"
                    + ("s" if len(research.gaps) != 1 else "")
                ),
                summary="skipped (error)",
            )

    # ── Stage 3: Author ──────────────────────────────────────────────────
    author_payload = _build_author_payload(research, web_citations)
    author_session = await asyncio.wait_for(
        author_agent.run(
            _build_author_user_turn(question, author_payload),
            message_history=message_history,
            actor=actor,
            deps_ref=deps_ref,
        ),
        timeout=_AUTHOR_TIMEOUT_S,
    )
    return await _await_text_result(author_session)
