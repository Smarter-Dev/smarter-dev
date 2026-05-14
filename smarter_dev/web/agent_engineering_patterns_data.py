"""Content for /resources/agent-engineering-patterns.

The Age-of-Agents layer of the resources index. Pairs with the other five
directories: /resources/agentic-coding-courses (the tools), /system-architecture
(the What), /infrastructure-hosting (the Where), /software-delivery (the
Shipping), /production-operations (the Keep-it-healthy), and
/patterns-of-practice (the timeless shapes).

Scope: patterns that emerged because LLM-driven agents now write, refactor,
test, and review code at machine speed. Three groups: how a codebase is
shaped so an agent can do useful work, how that work is verified before
merge, and how humans stay in the decision path without becoming the
bottleneck.
"""

from __future__ import annotations

from datetime import date

from smarter_dev.web.system_architecture_data import (
    ArchCategory,
    ArchResource,
    ArchTool,
    ArchToolResource,
)
from smarter_dev.web.vibe_courses_data import FAQ, Person

_INDEXED = date(2026, 5, 14)


def _r(title, url, source, key, tool_slugs, learning_type, blurb="", published_at=None):
    return ArchToolResource(
        title=title, url=url, source=source, key=key,
        tool_slugs=tuple(tool_slugs), learning_type=learning_type,
        first_indexed_at=_INDEXED, blurb=blurb, published_at=published_at,
    )


def _s(title, url, source, key, learning_type, blurb="", published_at=None):
    return ArchResource(
        title=title, url=url, source=source, key=key,
        learning_type=learning_type, first_indexed_at=_INDEXED, blurb=blurb,
        published_at=published_at,
    )


# ─── CATEGORIES ──────────────────────────────────────────────────────────────

AGENT_CATEGORIES: list[ArchCategory] = [
    ArchCategory(
        slug="spec-first",
        name="Spec-First Patterns",
        intro=(
            "How a codebase is shaped so an agent can do useful work on it. "
            "Naming discipline, module boundaries, AGENTS.md as a machine-readable "
            "contract, and specification-by-example are about giving the agent the "
            "same on-ramp a competent new hire gets. None of this is agent-specific. "
            "Agents punish the codebases that already neglected it."
        ),
        tools=(
            ArchTool("naming-discipline", "Naming Discipline",
                     "https://martinfowler.com/bliki/TwoHardThings.html",
                     "agent-patterns:tool:naming-discipline:home",
                     "Names are the agent's primary index into the codebase. Ambiguity multiplies into wrong calls and bad PRs."),
            ArchTool("module-boundaries", "Module Boundaries",
                     "https://web.eecs.umich.edu/~imarkov/10rules.pdf",
                     "agent-patterns:tool:module-boundaries:home",
                     "Modules small enough that an agent can hold the whole thing in its context window and reason about it."),
            ArchTool("agents-md", "AGENTS.md",
                     "https://agents.md/",
                     "agent-patterns:tool:agents-md:home",
                     "A README written for agents. Build steps, test commands, conventions, and guardrails the agent should respect."),
            ArchTool("specification-by-example", "Specification by Example",
                     "https://gojko.net/books/specification-by-example/",
                     "agent-patterns:tool:specification-by-example:home",
                     "Encode the spec as concrete input-output examples that double as documentation and test fixtures."),
            ArchTool("spec-driven-development", "Spec-Driven Development",
                     "https://github.com/github/spec-kit",
                     "agent-patterns:tool:spec-driven-development:home",
                     "Treat the spec as the source artifact. Plan, tasks, and code are downstream products the agent regenerates."),
        ),
    ),
    ArchCategory(
        slug="verification",
        name="Verification Patterns",
        intro=(
            "How an agent's work is verified at machine speed. Property tests, "
            "snapshot tests, contract tests, types, differential testing, and "
            "evals close the loop without lengthening the review queue. The "
            "agent's job is to clear the fence. Humans look at the diff only "
            "when the fence catches something interesting."
        ),
        tools=(
            ArchTool("property-tests-fence", "Property Tests as Agent Fence",
                     "https://hypothesis.works/articles/what-is-property-based-testing/",
                     "agent-patterns:tool:property-tests-fence:home",
                     "Use stated invariants as the safety rail the agent's diff has to clear, not just as bug-finding tools."),
            ArchTool("snapshot-golden", "Snapshot and Golden Tests",
                     "https://jestjs.io/docs/snapshot-testing",
                     "agent-patterns:tool:snapshot-golden:home",
                     "Lock current outputs as truth so a refactor either reproduces them exactly or surfaces its disagreements."),
            ArchTool("contract-tests", "Contract Tests and Types as Fences",
                     "https://martinfowler.com/bliki/ContractTest.html",
                     "agent-patterns:tool:contract-tests:home",
                     "Use type checkers and contract tests as the cheap fast layer the agent has to satisfy before a human looks."),
            ArchTool("differential-testing", "Differential Testing",
                     "https://github.com/github/scientist",
                     "agent-patterns:tool:differential-testing:home",
                     "Run the agent's rewrite next to the original against live traffic. Ship only when disagreement reaches zero."),
            ArchTool("evals", "Evals",
                     "https://www.anthropic.com/engineering",
                     "agent-patterns:tool:evals:home",
                     "Fixture sets of representative tasks. Track pass rate, regression rate, and cost across prompt and model changes."),
        ),
    ),
    ArchCategory(
        slug="human-loop",
        name="Human-in-the-Loop Patterns",
        intro=(
            "How humans stay in the decision path without becoming the bottleneck. "
            "Stage gates, confidence-tiered autonomy, review queue design, "
            "agent-as-reviewer, and tight feedback loops like the Ralph loop are "
            "about giving humans the calls only they can make and letting agents "
            "do the rest."
        ),
        tools=(
            ArchTool("stage-gates", "Stage Gates",
                     "https://simonwillison.net/2025/Sep/30/designing-agentic-loops/",
                     "agent-patterns:tool:stage-gates:home",
                     "Split agent work into plan, propose, and apply with a human checkpoint between each, not one big leap."),
            ArchTool("confidence-tiered-autonomy", "Confidence-Tiered Autonomy",
                     "https://openai.com/index/practices-for-governing-agentic-ai-systems/",
                     "agent-patterns:tool:confidence-tiered-autonomy:home",
                     "Agents act on low-risk classes, propose on medium-risk, and ask on high-risk. Risk graded per category."),
            ArchTool("review-queue", "Review Queue Design",
                     "https://google.github.io/eng-practices/review/reviewer/",
                     "agent-patterns:tool:review-queue:home",
                     "Treat the inbound stream of agent PRs as an explicit queue with rules and SLAs, not an ad-hoc reviewer pileup."),
            ArchTool("agent-as-reviewer", "Agent-as-Pair and Reviewer",
                     "https://github.blog/2024-04-29-github-copilot-workspace/",
                     "agent-patterns:tool:agent-as-reviewer:home",
                     "Put the agent on the reviewer seat for human work. The cheap pass catches the boring things humans miss."),
            ArchTool("ralph-loop", "The Ralph Loop",
                     "https://ghuntley.com/ralph/",
                     "agent-patterns:tool:ralph-loop:home",
                     "A bash while-loop feeds the same prompt to a coding agent until it converges. Brute-force, working."),
        ),
    ),
]


# ─── SPINE ───────────────────────────────────────────────────────────────────

AGENT_SPINE_RESOURCES: list[ArchResource] = [
    _s("Designing agentic loops",
       "https://simonwillison.net/2025/Sep/30/designing-agentic-loops/",
       "Simon Willison",
       "agent-patterns:spine:designing-agentic-loops", "Discussion",
       "Willison frames agent skill as designing the loop itself. Pick the right tools, scope the goal, and run safely in YOLO mode.",
       published_at=date(2025, 9, 30)),
    _s("Software Is Changing (Again)",
       "https://www.youtube.com/watch?v=LCEmiRjPEtQ",
       "Andrej Karpathy · YC AI Startup School",
       "agent-patterns:spine:karpathy-software-3", "Talk",
       "Karpathy lays out Software 3.0: LLMs as a new computer programmed in English, with humans verifying what models generate.",
       published_at=date(2025, 6, 17)),
    _s("Practices for Governing Agentic AI Systems",
       "https://openai.com/index/practices-for-governing-agentic-ai-systems/",
       "Shavit, Agarwal et al. · OpenAI",
       "agent-patterns:spine:governing-agentic-systems", "Best Practices",
       "Baseline practices for keeping agentic systems aligned with operator intent: scoping, oversight, interruptibility, accountability.",
       published_at=date(2023, 12, 14)),
    _s("Specification by Example",
       "https://gojko.net/books/specification-by-example/",
       "Gojko Adzic · Manning",
       "agent-patterns:spine:specification-by-example", "Tutorial",
       "Adzic's case studies on turning concrete examples into shared specifications that drive delivery. The pre-agent root of spec-driven coding.",
       published_at=date(2011, 5, 1)),
    _s("AGENTS.md",
       "https://agents.md/",
       "Agentic AI Foundation",
       "agent-patterns:spine:agents-md", "Best Practices",
       "A README for agents. An open Markdown convention for build steps, conventions, and guardrails that coding agents actually read.",
       published_at=None),
    _s("The Bitter Lesson",
       "http://www.incompleteideas.net/IncIdeas/BitterLesson.html",
       "Rich Sutton",
       "agent-patterns:spine:bitter-lesson", "Discussion",
       "Sutton's argument that general methods leveraging compute beat human-engineered cleverness. The intellectual backdrop for letting agents search.",
       published_at=date(2019, 3, 13)),
    _s("Spec Kit",
       "https://github.com/github/spec-kit",
       "GitHub",
       "agent-patterns:spine:spec-kit", "Tutorial",
       "GitHub's open toolkit for Spec-Driven Development. Spec, Plan, Tasks, Implement, with each phase producing an artifact the next phase consumes.",
       published_at=date(2025, 9, 2)),
    _s("The New Code",
       "https://www.youtube.com/watch?v=8rABwKRsec4",
       "Sean Grove · OpenAI (AI Engineer World's Fair 2025)",
       "agent-patterns:spine:new-code-specifications", "Talk",
       "Grove argues specifications, not code, are the durable artifact. The spec compiles to implementations. Prompts thrown away are wasted source.",
       published_at=date(2025, 6, 3)),
    _s("Ralph Wiggum as a \"software engineer\"",
       "https://ghuntley.com/ralph/",
       "Geoffrey Huntley",
       "agent-patterns:spine:ralph-loop", "Discussion",
       "Huntley's Ralph loop. A bash while-loop feeding the same prompt to a coding agent until it converges. Brute-force agentic engineering, working.",
       published_at=date(2025, 7, 14)),
    _s("Vibing a Non-Trivial Ghostty Feature",
       "https://mitchellh.com/writing/non-trivial-vibing",
       "Mitchell Hashimoto",
       "agent-patterns:spine:hashimoto-non-trivial-vibing", "Discussion",
       "Hashimoto ships a real Ghostty feature across 16 agent sessions for $15.98 and publishes the full transcripts. A concrete look at staying the architect.",
       published_at=date(2025, 10, 11)),
    _s("Exploring Generative AI",
       "https://martinfowler.com/articles/exploring-gen-ai.html",
       "Birgitta Boeckeler et al. · Thoughtworks (martinfowler.com)",
       "agent-patterns:spine:exploring-gen-ai", "Discussion",
       "A running Thoughtworks memo series on AI-assisted delivery. Context engineering, harness design, spec-driven coding, what holds up in practice.",
       published_at=date(2023, 7, 26)),
    _s("Cheating is all you need",
       "https://sourcegraph.com/blog/cheating-is-all-you-need",
       "Steve Yegge · Sourcegraph",
       "agent-patterns:spine:cheating-is-all-you-need", "Discussion",
       "Yegge's early call that LLM-augmented coding is a step change, not a parlor trick. The polemic that primed many engineers to take agents seriously.",
       published_at=date(2023, 3, 23)),
]


# ─── PER-PATTERN RESOURCES ──────────────────────────────────────────────────

AGENT_TOOL_RESOURCES: list[ArchToolResource] = [
    # ── Spec-First ──
    _r("Two Hard Things",
       "https://martinfowler.com/bliki/TwoHardThings.html",
       "Martin Fowler", "agent-patterns:res:naming-discipline:fowler",
       ["naming-discipline"], "Discussion",
       "Fowler's bliki entry on the joke that names half of every onboarding doc. Cache invalidation and naming things."),
    _r("Naming as a Process",
       "https://www.digdeeproots.com/articles/on/naming-as-a-process/",
       "Arlo Belshee", "agent-patterns:res:naming-discipline:belshee",
       ["naming-discipline"], "Tutorial",
       "Belshee's seven-step ladder from \"nonsense\" to \"domain abstraction\". A working method for renaming legacy code one move at a time."),
    _r("The Power of Ten: Rules for Developing Safety-Critical Code",
       "https://web.eecs.umich.edu/~imarkov/10rules.pdf",
       "Gerard J. Holzmann · NASA JPL",
       "agent-patterns:res:module-boundaries:power-of-ten",
       ["module-boundaries"], "Best Practices",
       "Ten rules for keeping modules readable by humans and agents. Small functions, no surprises, bounded loops."),
    _r("Modular Monolith: A Primer",
       "https://www.kamilgrzybek.com/blog/posts/modular-monolith-primer",
       "Kamil Grzybek", "agent-patterns:res:module-boundaries:grzybek",
       ["module-boundaries"], "Tutorial",
       "How to draw hard module boundaries inside one deployable. The shape agents work best inside."),
    _r("AGENTS.md spec",
       "https://agents.md/",
       "Agentic AI Foundation", "agent-patterns:res:agents-md:home",
       ["agents-md"], "Best Practices",
       "Community spec for the AGENTS.md file. Conventions, commands, and tests an agent should respect when changing this repo."),
    _r("How to write AGENTS.md",
       "https://ampcode.com/AGENTS.md",
       "Amp · Sourcegraph", "agent-patterns:res:agents-md:amp-example",
       ["agents-md"], "Tutorial",
       "Amp's own AGENTS.md, published as a worked example. Build commands, code style, testing, and PR conventions in one file."),
    _r("Specification by Example",
       "https://gojko.net/books/specification-by-example/",
       "Gojko Adzic · Manning",
       "agent-patterns:res:specification-by-example:adzic",
       ["specification-by-example"], "Tutorial",
       "Adzic's reference text. Living specs that double as tests, written in concrete examples humans and machines can read."),
    _r("Spec-Driven Development with AI: Get Started with a New Open Source Toolkit",
       "https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/",
       "GitHub blog", "agent-patterns:res:spec-driven-development:github-spec-kit-announce",
       ["spec-driven-development"], "Tutorial",
       "GitHub's introduction to Spec Kit. Spec, Plan, Tasks, Implement, each producing an artifact the next phase consumes.",
       published_at=date(2025, 9, 2)),
    _r("The New Code",
       "https://www.youtube.com/watch?v=8rABwKRsec4",
       "Sean Grove · OpenAI",
       "agent-patterns:res:spec-driven-development:grove-new-code",
       ["spec-driven-development"], "Talk",
       "Grove makes the case that specifications, not prompts or code, are the durable artifact. The spec compiles. Prompts thrown away are wasted source.",
       published_at=date(2025, 6, 3)),

    # ── Verification ──
    _r("What is Property Based Testing?",
       "https://hypothesis.works/articles/what-is-property-based-testing/",
       "Hypothesis", "agent-patterns:res:property-tests-fence:hypothesis-intro",
       ["property-tests-fence"], "Tutorial",
       "The Hypothesis project's intro essay. State invariants and let the runner generate inputs. The starting point for property testing on real code."),
    _r("Property-Based Testing in Python",
       "https://hypothesis.readthedocs.io/en/latest/quickstart.html",
       "Hypothesis docs",
       "agent-patterns:res:property-tests-fence:hypothesis-quickstart",
       ["property-tests-fence"], "Tutorial",
       "Hands-on quickstart. From decorator to passing assertion, with strategies for generating realistic inputs."),
    _r("Snapshot Testing",
       "https://jestjs.io/docs/snapshot-testing",
       "Jest docs", "agent-patterns:res:snapshot-golden:jest",
       ["snapshot-golden"], "Tutorial",
       "Jest's reference treatment. Record current output as truth, fail when it changes. Useful when the spec is \"whatever it does today.\""),
    _r("ContractTest",
       "https://martinfowler.com/bliki/ContractTest.html",
       "Martin Fowler",
       "agent-patterns:res:contract-tests:fowler-contract-test",
       ["contract-tests"], "Discussion",
       "Fowler's bliki entry on contract tests. Narrow fast tests that pin down the shape of an inter-service contract."),
    _r("Type-Driven Development with Idris",
       "https://www.manning.com/books/type-driven-development-with-idris",
       "Edwin Brady · Manning",
       "agent-patterns:res:contract-tests:brady",
       ["contract-tests"], "Tutorial",
       "Brady's book on types-as-fences taken to its conclusion. The type checker as the first and cheapest verifier."),
    _r("Scientist",
       "https://github.com/github/scientist",
       "GitHub", "agent-patterns:res:differential-testing:github-scientist",
       ["differential-testing"], "Tutorial",
       "GitHub's open-source library for differential testing. Run new code in shadow next to old, compare, switch over."),
    _r("How we rebuilt Next.js with AI in one week",
       "https://blog.cloudflare.com/vinext/",
       "Cloudflare blog",
       "agent-patterns:res:differential-testing:cloudflare-vinext",
       ["differential-testing"], "Discussion",
       "vinext at 94% of the Next.js API surface, scored against 1,700 Vitest plus 380 Playwright tests ported straight from Next.js. Differential testing at framework scale."),
    _r("A 10x Faster TypeScript",
       "https://devblogs.microsoft.com/typescript/typescript-native-port/",
       "Anders Hejlsberg · Microsoft",
       "agent-patterns:res:differential-testing:typescript-native-port",
       ["differential-testing"], "Discussion",
       "Hejlsberg on porting tsc to Go for semantic parity. The reason it's a port and not a rewrite is so the existing test suite stays the source of truth."),
    _r("Anthropic's Bun team trials port from Zig to Rust",
       "https://www.theregister.com/2026/05/05/bun_rust_port/",
       "The Register",
       "agent-patterns:res:differential-testing:bun-rust",
       ["differential-testing"], "Discussion",
       "Bun's AI-assisted Zig-to-Rust port at 99.8% of the pre-existing test suite. The rewrite stays in shadow until disagreement hits zero, exactly the pattern."),
    _r("Building effective agents",
       "https://www.anthropic.com/engineering/building-effective-agents",
       "Anthropic", "agent-patterns:res:evals:anthropic-building-effective",
       ["evals", "confidence-tiered-autonomy"], "Best Practices",
       "Anthropic's reference on agent design. Workflows vs. agents, tool design, evals, and where the autonomy band actually pays off."),
    _r("Demystifying evals for AI agents",
       "https://www.anthropic.com/engineering",
       "Anthropic Engineering",
       "agent-patterns:res:evals:anthropic-demystifying",
       ["evals"], "Best Practices",
       "Anthropic's working notes on building eval suites for agents. Treat the agent like untrusted input. Run a fixture set on every change."),

    # ── Human-in-the-Loop ──
    _r("Designing agentic loops",
       "https://simonwillison.net/2025/Sep/30/designing-agentic-loops/",
       "Simon Willison",
       "agent-patterns:res:stage-gates:willison-loops",
       ["stage-gates"], "Discussion",
       "Willison's essay on splitting agent runs into plan, propose, and apply phases. Where the human checkpoints actually sit.",
       published_at=date(2025, 9, 30)),
    _r("Vibing a Non-Trivial Ghostty Feature",
       "https://mitchellh.com/writing/non-trivial-vibing",
       "Mitchell Hashimoto",
       "agent-patterns:res:stage-gates:hashimoto-vibing",
       ["stage-gates"], "Discussion",
       "Sixteen agent sessions to ship one Ghostty feature, all transcripts published. A working example of stage gates as a real-world workflow.",
       published_at=date(2025, 10, 11)),
    _r("Practices for Governing Agentic AI Systems",
       "https://openai.com/index/practices-for-governing-agentic-ai-systems/",
       "Shavit, Agarwal et al. · OpenAI",
       "agent-patterns:res:confidence-tiered-autonomy:openai-practices",
       ["confidence-tiered-autonomy"], "Best Practices",
       "OpenAI's framework for tiering agent autonomy by risk class. The underpinnings of confidence-tiered policies.",
       published_at=date(2023, 12, 14)),
    _r("Software Is Changing (Again)",
       "https://www.youtube.com/watch?v=LCEmiRjPEtQ",
       "Andrej Karpathy · YC AI Startup School",
       "agent-patterns:res:confidence-tiered-autonomy:karpathy-software-3",
       ["confidence-tiered-autonomy"], "Talk",
       "Karpathy on the autonomy slider. The Tesla Autopilot analogy for handing tasks to an agent gradually instead of full autonomy on day one.",
       published_at=date(2025, 6, 17)),
    _r("How to do a code review (Google)",
       "https://google.github.io/eng-practices/review/reviewer/",
       "Google Engineering Practices",
       "agent-patterns:res:review-queue:google-eng-practices",
       ["review-queue"], "Best Practices",
       "Google's public reviewer guide. The rules and SLAs that scale a review queue without sinking individual reviewers."),
    _r("Pull Request Reviews with Copilot",
       "https://docs.github.com/en/copilot/using-github-copilot/code-review/using-copilot-code-review",
       "GitHub docs",
       "agent-patterns:res:review-queue:copilot-review-docs",
       ["review-queue", "agent-as-reviewer"], "Tutorial",
       "GitHub's docs on letting Copilot review PRs. The cheap pass that catches the boring things humans miss."),
    _r("GitHub Copilot Workspace",
       "https://github.blog/2024-04-29-github-copilot-workspace/",
       "GitHub blog",
       "agent-patterns:res:agent-as-reviewer:copilot-workspace",
       ["agent-as-reviewer"], "Discussion",
       "GitHub's announcement for Copilot Workspace. The working sketch of the agent-as-reviewer interaction.",
       published_at=date(2024, 4, 29)),
    _r("Ralph Wiggum as a \"software engineer\"",
       "https://ghuntley.com/ralph/",
       "Geoffrey Huntley",
       "agent-patterns:res:ralph-loop:huntley",
       ["ralph-loop"], "Discussion",
       "Huntley's original Ralph loop post. A bash while-loop feeding the same prompt to a coding agent until it converges.",
       published_at=date(2025, 7, 14)),
]


# ─── CREATORS ────────────────────────────────────────────────────────────────

AGENT_PEOPLE: list[Person] = [
    Person(
        "Simon Willison", "simonw", "blog", "https://simonwillison.net/",
        "agent-patterns:person:blog:simon-willison",
        "Daily field notes on LLM tooling, agentic coding, and the practical limits of AI-assisted development.",
    ),
    Person(
        "Andrej Karpathy", "karpathy", "YouTube",
        "https://www.youtube.com/@AndrejKarpathy",
        "agent-patterns:person:youtube:andrej-karpathy",
        "Long-form lectures on neural networks, LLMs, and how agents actually learn. Also posts at karpathy.ai and on X.",
    ),
    Person(
        "Birgitta Boeckeler", "bboeckel", "martinfowler.com",
        "https://martinfowler.com/articles/exploring-gen-ai.html",
        "agent-patterns:person:martinfowler:birgitta-boeckeler",
        "Thoughtworks lead writing field memos on agentic coding, harness engineering, and what changes when AI joins the team.",
    ),
    Person(
        "Geoff Huntley", "ghuntley", "blog", "https://ghuntley.com/",
        "agent-patterns:person:blog:geoff-huntley",
        "Sharp writeups on the Ralph loop, autonomous coding agents, and what it looks like to run Claude in production.",
    ),
    Person(
        "Sean Grove", "sgrove", "YouTube",
        "https://www.youtube.com/watch?v=8rABwKRsec4",
        "agent-patterns:person:youtube:sean-grove",
        "OpenAI engineer making the case that specifications, not prompts or code, are the new unit of programming.",
    ),
    Person(
        "Anthropic Engineering", "anthropic", "engineering blog",
        "https://www.anthropic.com/engineering",
        "agent-patterns:person:engineering-blog:anthropic",
        "Practical writeups on building effective agents, designing tools, and evaluating agent output in production.",
    ),
    Person(
        "Mitchell Hashimoto", "mitchellh", "blog",
        "https://mitchellh.com/writing",
        "agent-patterns:person:blog:mitchell-hashimoto",
        "HashiCorp founder and Ghostty author writing on how agentic tools fit into serious systems work.",
    ),
    Person(
        "Chip Huyen", "chiphuyen", "blog", "https://huyenchip.com/",
        "agent-patterns:person:blog:chip-huyen",
        "Author of AI Engineering and Designing ML Systems. Writes on shipping AI to production without the magic.",
    ),
]


# ─── FAQs ────────────────────────────────────────────────────────────────────

AGENT_FAQS: list[FAQ] = [
    FAQ(
        "How do I structure a codebase so an agent can be useful in it?",
        "Give the agent the same on-ramp you would give a new hire. Add an "
        "AGENTS.md at the repo root with build, test, and convention notes. "
        "Keep modules small and well-named. Make sure the test suite runs "
        "cleanly from a fresh clone. Agents do their best work in codebases "
        "where humans already do their best work.",
        source_label="AGENTS.md",
        source_url="https://agents.md/",
        source_key="agent-patterns:faq:agents-md",
    ),
    FAQ(
        "What's the difference between spec-driven development and just writing good tests?",
        "Tests check behavior after you decide what to build. A spec captures "
        "intent before the code exists: goals, constraints, success criteria, "
        "and example inputs and outputs. Sean Grove's framing is that the "
        "spec is the new source code and tests are one of several artifacts "
        "generated from it. Good tests are necessary but not sufficient.",
        source_label="The New Code (Sean Grove, OpenAI)",
        source_url="https://www.youtube.com/watch?v=8rABwKRsec4",
        source_key="agent-patterns:faq:sean-grove-new-code",
    ),
    FAQ(
        "When should an agent merge without human review?",
        "Only when the blast radius is small, the tests are trustworthy, and "
        "the change is reversible. Think dependency bumps, formatting passes, "
        "or scoped refactors behind a feature flag. Anything that touches "
        "auth, data migrations, billing, or public APIs still wants a human "
        "signoff. Start narrow and widen the autonomy band as your evals "
        "catch real regressions.",
        source_label="Anthropic: Building effective agents",
        source_url="https://www.anthropic.com/engineering/building-effective-agents",
        source_key="agent-patterns:faq:anthropic-building-effective-agents",
    ),
    FAQ(
        "How do I keep agent-generated code reviewable at scale?",
        "Make the agent write small, single-purpose PRs with a clear summary "
        "of intent and the spec or task it was working from. Require the "
        "same conventions you require of humans: meaningful commit messages, "
        "passing tests, no drive-by changes. Birgitta Boeckeler's memos on "
        "harness engineering are a good model. Invest in the scaffolding "
        "that makes the output legible.",
        source_label="Exploring Gen AI (Birgitta Boeckeler)",
        source_url="https://martinfowler.com/articles/exploring-gen-ai.html",
        source_key="agent-patterns:faq:bboeckel-exploring-gen-ai",
    ),
    FAQ(
        "What's AGENTS.md, and is it worth adopting now?",
        "AGENTS.md is an open format for telling coding agents how your "
        "project works: build commands, test commands, conventions, gotchas. "
        "It is stewarded by the Agentic AI Foundation and read natively by "
        "major agents including Claude Code, Codex, and Gemini CLI. Adoption "
        "is cheap, the file is just Markdown, and you get immediate leverage. "
        "Worth adding today.",
        source_label="AGENTS.md spec",
        source_url="https://agents.md/",
        source_key="agent-patterns:faq:agents-md-spec",
    ),
    FAQ(
        "How do I evaluate an agent's output without reading every diff?",
        "Treat the agent like any other untrusted input: write evals. Build "
        "a fixture set of representative tasks, run the agent against them "
        "on every prompt or model change, and track pass rate, regression "
        "rate, and cost per task. Anthropic's writeups on demystifying evals "
        "are a solid starting point. Spot-check diffs only when the eval "
        "surfaces something interesting.",
        source_label="Anthropic Engineering",
        source_url="https://www.anthropic.com/engineering",
        source_key="agent-patterns:faq:anthropic-evals",
    ),
    FAQ(
        "Where does the human stay in the loop, and where does the agent take over?",
        "Humans own intent, constraints, and the merge button. Agents own "
        "the mechanical work in between: drafting, refactoring, running "
        "tests, proposing changes. The GitHub Spec Kit workflow makes this "
        "concrete. Humans write and approve the spec, plan, and tasks. The "
        "agent implements against them. Keep the handoff points explicit and "
        "you keep your judgment where it matters.",
        source_label="GitHub Spec Kit",
        source_url="https://github.com/github/spec-kit",
        source_key="agent-patterns:faq:github-spec-kit",
    ),
]
