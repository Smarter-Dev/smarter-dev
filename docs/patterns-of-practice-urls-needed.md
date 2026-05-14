# Patterns of Practice — URLs needing your eyes

Sixteen reading-path entries where the agent couldn't verify a stable URL or no canonical writeup exists yet. Grouped by "kind of problem" because the remediation differs.

Each entry shows:
- **Where it lives** in `docs/patterns-of-practice-research.md` (section + pattern)
- **Placeholder URL** the agent put down
- **What the entry is trying to capture** (so you can hunt for an equivalent if the original is gone)
- **Recommendation** for what to do — keep, swap, or drop

---

## Kind A — Dead or moved (artifact exists, URL is wrong)

These have real underlying artifacts; the URL just rotted. Cheapest to fix.

### A1. "Hammering Usernames" (Facebook Engineering, 2009)

- **Where:** Section 2, Architecture Patterns → Evolution & Migration → **Dark Launches**
- **Placeholder:** `https://www.facebook.com/notes/facebook-engineering/hammering-usernames/96390263919`
- **Capturing:** The origin story of "dark launch" as a term — Facebook pre-warming the username feature against real production load before users could see it.
- **Recommendation:** Try **Wayback Machine** for the original. If that fails, swap to one of:
  - High Scalability summary: search "Hammering Usernames" on highscalability.com
  - Mike Krieger's later "Lessons Learned Scaling Instagram" mentions the technique.
  - Or drop this entry; the LaunchDarkly definition above it covers the term adequately.

### A2. Incident Response at Heroku (Eckhardt, Neva)

- **Where:** Section 3, Patterns of Discipline → Operational Patterns → **Runbooks-as-Code**
- **Placeholder:** `https://blog.heroku.com/incident-response`
- **Capturing:** "Runbook PR is the artifact of every learning" framing — smaller-org runbook discipline as contrast to Google SRE-book scale.
- **Recommendation:** Search Heroku's engineering blog for "incident response" by Eckhardt or Neva. If gone, **Increment's "On-Call" issue** (see A4 below) has overlapping material from similar voices.

### A3. Code Review from the Command Line (Dan Slimmon)

- **Where:** Section 3, Patterns of Discipline → Review & Verification → **Review-as-Conversation vs. Review-as-Gate**
- **Placeholder:** `https://blog.danslimmon.com/2022/10/27/code-review-from-the-command-line/`
- **Capturing:** "Review's primary product is the reviewer's understanding, not the author's correction."
- **Recommendation:** Check `blog.danslimmon.com` index. Slimmon's writing on this lives somewhere on his blog. If you can't find it, his "On Being a Senior Engineer" piece touches the same theme — could substitute.

### A4. An Engineer's Guide to a Good On-Call Rotation (Increment)

- **Where:** Section 3, Patterns of Discipline → Operational Patterns → **On-Call as Pattern**
- **Placeholder:** `https://increment.com/on-call/an-engineers-guide-to-a-good-on-call-rotation/`
- **Capturing:** Plural working-engineer voices on on-call from Increment's "On-Call" themed issue (Lorin Hochstein, Cindy Sridharan, others).
- **Recommendation:** Increment's archive has reshuffled. The whole **issue** is the artifact — `increment.com/on-call/` (the index page) is what to link if you can find it; otherwise link to one specific essay from the issue (Sridharan's "Distributed Systems Observability" excerpt is a candidate).

### A5. PagerDuty Incident Command Training

- **Where:** Section 3, Patterns of Discipline → Operational Patterns → **On-Call as Pattern**
- **Placeholder:** `https://github.com/SkeltonThatcher/run-book-template` (wrong repo — see note)
- **Capturing:** IC roles, communications, handoff — the *response*-side mechanics of an on-call shift.
- **Recommendation:** The agent linked the wrong repo. The real artifact is **`response.pagerduty.com`** — PagerDuty's open-source incident response training documentation. Swap to that.

### A6. What we've learned from doing code review at Stripe

- **Where:** Section 3, Patterns of Discipline → Review & Verification → **Review-as-Conversation vs. Review-as-Gate**
- **Placeholder:** `https://stripe.com/blog/code-review`
- **Capturing:** Stripe's published reflection on tuning review for a high-trust IC-heavy org, naming the queue-time cost of gate-shaped review.
- **Recommendation:** Verify this URL works. If not, search Stripe's engineering blog for a code review post by Michael Magruder or Daniel Schauenberg. Could also substitute Will Larson's "Useful tools for working with feature flags" or Jacob Kaplan-Moss's "What I look for in code reviews."

### A7. Approval Testing with Emily Bache

- **Where:** Section 3, Patterns of Discipline → Review & Verification → **Golden/Snapshot Tests**
- **Placeholder:** `https://www.youtube.com/watch?v=4t14SVHQQNk`
- **Capturing:** Bache demonstrating approval testing as a refactoring scaffold in real time.
- **Recommendation:** Bache has many talks. **"Refactoring Legacy Code with the Gilded Rose Kata"** is the most-cited; her YouTube channel `@EmilyBache-tech-coach` has a curated playlist on approval testing.

### A8. Time, Property-Based Testing, and a Coffee Maker (Hillel Wayne)

- **Where:** Section 3, Patterns of Discipline → Review & Verification → **Property-Based Testing**
- **Placeholder:** `https://www.hillelwayne.com/post/pbt-contracts/`
- **Capturing:** PBT as design tool — the act of writing the property is design work that pays off even when no bug is caught.
- **Recommendation:** Wayne has many PBT posts. Best candidates: **"Property Testing Like AFL"** or **"What I learned writing a quickcheck library"** — both on hillelwayne.com. The substantive Wayne-on-PBT essay is real; the agent picked the wrong slug.

---

## Kind B — Vendor-y or SEO-style links flagged for replacement

The URL works but violates the editorial rule against listicle/SEO sources.

### B1. Adapter pattern (refactoring.guru)

- **Where:** Section 1, Code Patterns → Boundaries & Abstraction → **Adapter**
- **Placeholder:** `https://refactoring.guru/design-patterns/adapter`
- **Why flagged:** refactoring.guru is SEO-shaped content; we agreed to keep this directory clear of that voice.
- **Recommendation:** Replace with **Fowler's "Adapter" entry** if there's one on martinfowler.com, or use the **Working Effectively with Legacy Code** chapter on adapter use already cited in the same reading path as the primary anchor (and drop the refactoring.guru entry entirely — 2 sources is fine).

### B2. James Shore "Dependency Injection Demystified" (note)

- **Where:** Section 1, Code Patterns → Composition & Construction → **Dependency Injection**
- **Placeholder:** James Shore's site (URL has moved over the years)
- **Why flagged:** The URL may 404; the artifact still exists somewhere on jamesshore.com.
- **Recommendation:** Verify the link works; if 404, search "jamesshore dependency injection demystified" — the canonical text is unchanged across moves.

### B3. An Introduction to Mutation Testing (Increment URL is wrong)

- **Where:** Section 3, Patterns of Discipline → Review & Verification → **Mutation Testing**
- **Placeholder:** `https://increment.com/testing/in-praise-of-property-based-testing/` (this is the PBT post, not mutation testing)
- **Capturing:** A working-engineer (not academic) intro to mutation testing.
- **Recommendation:** The agent admitted there's no canonical working-engineer post. Real options:
  - **Henry Coles' Pitest documentation** — `pitest.org` (most pragmatic non-academic source)
  - **Jia & Harman's "An Analysis and Survey of the Development of Mutation Testing"** (academic but the canonical survey)
  - Or drop the entry — Mutation Testing is tagged `situational` in the dossier, and the section already has 4 entries.

### B4. API Change Strategy (Keith Casey, Nordic APIs)

- **Where:** Section 2, Architecture Patterns → Evolution & Migration → **Expand/Contract**
- **Placeholder:** `https://nordicapis.com/api-change-strategy/`
- **Capturing:** Additive-versioning and deprecation windows as the API-layer analog of expand/contract.
- **Recommendation:** Verify the URL. If broken, replace with **Brandur Leach's "API Versioning at Stripe"** (`stripe.com/blog/api-versioning`) — same idea, better-known author, more durable URL.

---

## Kind C — Genuinely emerging (no canonical writeup yet)

These are the honest "the pattern is real and being lived, but nobody has written *the* essay on it yet" entries. The dossier already flags them transparently. Decision to make: leave the placeholder visible to readers (which adds editorial credibility), find a best-available substitute, or drop the reading-path entry entirely (leaving only the other 2-3 sources).

### C1. Shadow Mode / Dual Run patterns (agent-rewrite framing)

- **Where:** Section 5, Age of Agents → Verification Patterns → **Differential Testing**
- **Capturing:** "Run new code in shadow next to old code, compare, switch over once disagreement rate hits zero." Older technique, agent-rewrite application is what's new.
- **Recommendation:** No canonical agent-era piece yet. **GitHub's Scientist** is already in the reading path as a tooling reference. Either drop this fourth entry or substitute **Stripe's "Migrating from Resque to Sidekiq with zero downtime"** — same shape, classic example.

### C2. AI Workflows: Designing for safe agentic execution

- **Where:** Section 5, Age of Agents → Human-in-the-Loop → **Stage Gates**
- **Capturing:** Plan-then-execute as a workflow primitive.
- **Recommendation:** No canonical writeup. Closest substitutes: **Simon Willison's "Designing agentic loops"** or **the OpenAI "Practices for governing agentic AI systems" whitepaper** (`openai.com/research/practices-for-governing-agentic-ai-systems`). Or leave the flag visible.

### C3. Designing for human-AI collaboration in coding

- **Where:** Section 5, Age of Agents → Human-in-the-Loop → **Confidence-Tiered Autonomy**
- **Capturing:** Team-level autonomy tiering policy (as opposed to vendor mechanism docs).
- **Recommendation:** No canonical piece. Closest: **Geoffrey Litt's "Malleable Software in the Age of LLMs"** touches the design philosophy. Or **Steve Yegge's "Cheating is all you need"** for working-engineer texture. Both adjacent rather than direct.

### C4. Anatomy of an async PR review workflow (agent-PR-queue context)

- **Where:** Section 5, Age of Agents → Human-in-the-Loop → **Review Queue**
- **Capturing:** Team-process-level patterns for agent-generated PR review (vs. tooling-level docs).
- **Recommendation:** No canonical writeup at the *process* layer. **Drew Houston's "How Dropbox uses Cursor"** or **Shopify's "Building Shopify Magic"** are adjacent. Leave flagged or substitute one of those.

---

## Suggested workflow

1. **Spend the cheap fixes first:** Kind A entries (A1–A8) mostly need a wayback machine lookup or a 2-minute search. Worth fixing because each cite a real, named artifact.
2. **Apply the editorial rule:** Kind B entries (B1–B4) — decide whether to swap or just drop the entry (most sections have 4 reading-path items; dropping to 3 is fine).
3. **Leave Kind C honest:** The "URL needed — pattern is emerging" flag is itself an editorial signal — it tells readers *this is genuinely new and we're not pretending otherwise*. I'd keep at least 2-3 of these visible; they make the directory feel honest in a way the rest of the resource site can borrow from.

Once URLs are decided, the entries land in `patterns_of_practice_data.py` as `ArchResource` (or whatever the directory data type is named) — the markdown research is reference, the seeded data is the deliverable.
