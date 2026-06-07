"""Content for /vibe-coding-courses.

Hand-maintained: edit and redeploy. Each outbound link carries a stable
``track_key`` so the click counter survives URL changes.

Sort order on the page:
- Featured strip default: ``COURSES`` declared order (Python's sorted() is
  stable; with equal ``first_indexed_at`` values it preserves this order).
- Featured strip flips to "Most Popular" once the 3rd-ranked link has \u226510
  clicks (logic lives in ``vibe_courses_controller``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class Tool:
    slug: str
    name: str
    description: str
    url: str
    home_key: str


CATEGORIES: tuple[str, ...] = (
    "Tutorial",
    "Course",
    "Discussion",
    "Best Practices",
    "Talk",
)


@dataclass(frozen=True)
class Course:
    title: str
    url: str
    source: str
    key: str
    tools: tuple[str, ...]
    first_indexed_at: date
    published_at: date | None = None
    blurb: str = ""
    category: str = "Tutorial"

    @property
    def sort_date(self) -> date:
        return self.published_at or self.first_indexed_at

    @property
    def category_slug(self) -> str:
        """URL-safe form of category for the filter chip's data attribute."""
        return self.category.lower().replace(" ", "-")


@dataclass(frozen=True)
class Person:
    name: str
    handle: str
    platform: str
    url: str
    key: str
    blurb: str


_INDEXED = date(2026, 5, 12)


# ─── TOOLS ────────────────────────────────────────────────────────────────────

TOOLS: list[Tool] = [
    Tool("claude-code", "Claude Code",
         "Terminal-native agentic coder from Anthropic that reads codebases, edits files, runs tests, and coordinates parallel agent teams.",
         "https://www.anthropic.com/product/claude-code",
         "vibe:tool:claude-code:home"),
    Tool("codex", "OpenAI Codex",
         "OpenAI's Rust-based terminal agent plus IDE extensions and a cloud agent, powered by the GPT-5.x Codex models.",
         "https://openai.com/codex/",
         "vibe:tool:codex:home"),
    Tool("cursor", "Cursor",
         "VS Code fork with the Composer model and a unified local-plus-cloud agent workspace spanning desktop, web, and mobile.",
         "https://cursor.com/",
         "vibe:tool:cursor:home"),
    Tool("windsurf", "Windsurf",
         "Cognition-owned agentic IDE with Cascade and the SWE-1.5 model; the Codeium editor reborn as an AI-native IDE.",
         "https://windsurf.com/",
         "vibe:tool:windsurf:home"),
    Tool("lovable", "Lovable",
         "Prompt-to-app builder that generates full-stack React + Supabase projects with auth, database, and one-click deploy.",
         "https://lovable.dev/",
         "vibe:tool:lovable:home"),
    Tool("bolt-new", "Bolt.new",
         "StackBlitz in-browser agent that prompts, builds, and hosts full-stack web apps through Bolt Cloud.",
         "https://bolt.new/",
         "vibe:tool:bolt-new:home"),
    Tool("v0", "v0",
         "Vercel's generative app tool that produces production React UIs, multi-page apps, and end-to-end agent workflows.",
         "https://v0.app/",
         "vibe:tool:v0:home"),
    Tool("replit-agent", "Replit Agent",
         "Cloud agent that builds, deploys, and collaborates on full apps inside Replit's browser workspace and Design Canvas.",
         "https://replit.com/products/agent",
         "vibe:tool:replit-agent:home"),
    Tool("devin", "Devin",
         "Cognition's autonomous AI software engineer that plans, codes, tests, and opens PRs on long-horizon engineering tasks.",
         "https://devin.ai/",
         "vibe:tool:devin:home"),
    Tool("antigravity", "Google Antigravity",
         "Google's agent-first VS Code fork with a Manager view for orchestrating parallel Gemini coding agents.",
         "https://antigravity.google/",
         "vibe:tool:antigravity:home"),
    Tool("jules", "Jules",
         "Google's async coding agent that clones repos into a cloud VM and ships PRs while you're offline.",
         "https://jules.google/",
         "vibe:tool:jules:home"),
    Tool("github-copilot", "GitHub Copilot",
         "GitHub's IDE assistant with an autonomous coding agent that takes issues to PRs across VS Code, JetBrains, and Visual Studio.",
         "https://github.com/features/copilot",
         "vibe:tool:github-copilot:home"),
    Tool("aider", "Aider",
         "Open-source terminal pair programmer that edits any Git repo and commits each change atomically, architect/editor split.",
         "https://aider.chat/",
         "vibe:tool:aider:home"),
    Tool("cline", "Cline",
         "Open-source autonomous coding agent for VS Code with Plan and Act modes, browser control, and 30+ model providers.",
         "https://cline.bot/",
         "vibe:tool:cline:home"),
    Tool("continue", "Continue",
         "Open-source VS Code and JetBrains agent with source-controlled AI rules enforceable in CI, BYO-model.",
         "https://www.continue.dev/",
         "vibe:tool:continue:home"),
    Tool("roo-code", "Roo Code",
         "Open-source VS Code agent with role-specific modes (Architect, Code, Debug, Test) acting as a whole AI dev team.",
         "https://roocode.com/",
         "vibe:tool:roo-code:home"),
    Tool("opencode", "OpenCode",
         "Open-source Go-based terminal agent by SST with a Bubble Tea TUI and 75+ model providers, no lock-in.",
         "https://opencode.ai/",
         "vibe:tool:opencode:home"),
    Tool("goose", "Goose",
         "Block's open-source extensible agent with desktop, CLI, and MCP-based extensions for any LLM provider.",
         "https://block.github.io/goose/",
         "vibe:tool:goose:home"),
    Tool("warp", "Warp",
         "Open-source Rust terminal with Agent Mode that runs multi-step plans in your shell and orchestrates cloud agents.",
         "https://www.warp.dev/",
         "vibe:tool:warp:home"),
    Tool("zed", "Zed",
         "Rust-native editor with parallel agents and Agent Client Protocol for Claude, Gemini, and Codex CLIs.",
         "https://zed.dev/",
         "vibe:tool:zed:home"),
    Tool("kiro", "Kiro",
         "AWS's spec-driven agentic IDE and CLI built around Specs, Hooks, and Steering files.",
         "https://kiro.dev/",
         "vibe:tool:kiro:home"),
    Tool("factory", "Factory",
         "Agent-native platform whose Droids run Missions across the full SDLC \u2014 planning, coding, reviewing, and deploying in parallel.",
         "https://factory.ai/",
         "vibe:tool:factory:home"),
    Tool("gemini-cli", "Gemini CLI",
         "Google's open-source terminal agent (Apache 2.0) using Gemini with a 1M-token context and MCP tools.",
         "https://github.com/google-gemini/gemini-cli",
         "vibe:tool:gemini-cli:home"),
]


# ─── COURSES ──────────────────────────────────────────────────────────────────

COURSES: list[Course] = [
    # Cross-cutting "use agents to ship software" workflow content. Front of
    # the list so it leads the "Latest" strip until click counts promote
    # tool-specific items. Note: deliberately excludes content about *building*
    # agents or LLM theory; only practical workflow material lives here.
    Course(
        "Vibe coding (the original tweet)",
        "https://x.com/karpathy/status/1886192184808149383",
        "X · Andrej Karpathy",
        "vibe:course:karpathy-vibe-coding-tweet",
        tools=(),
        first_indexed_at=_INDEXED,
        published_at=date(2025, 2, 2),
        blurb="The Feb 2025 post that named the practice: give in to the LLM, forget the code.",
        category="Discussion",
    ),
    Course(
        "Spec-Driven Development with Coding Agents",
        "https://www.deeplearning.ai/courses/spec-driven-development-with-coding-agents",
        "DeepLearning.AI (with JetBrains)",
        "vibe:course:dlai-spec-driven-development",
        tools=(),
        first_indexed_at=_INDEXED,
        published_at=date(2026, 4, 15),
        blurb="Plan-implement-verify loop: write a constitution and specs your coding agent can ship from.",
        category="Course",
    ),
    Course(
        "Spec-driven development with AI: Get started with the open-source toolkit",
        "https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/",
        "GitHub blog",
        "vibe:course:gh-spec-kit",
        tools=(),
        first_indexed_at=_INDEXED,
        published_at=date(2025, 9, 2),
        blurb="Walkthrough of Spec Kit: turn markdown specs into agent-built features across Copilot, Claude, Gemini.",
    ),
    Course(
        "Vibe Coding Essentials: Build Apps with AI",
        "https://www.coursera.org/specializations/vibe-coding",
        "Coursera (Scrimba)",
        "vibe:course:coursera-vibe-coding-essentials",
        tools=(),
        first_indexed_at=_INDEXED,
        blurb="Beginner specialization on shipping real apps with Copilot, Cursor, Claude Code, and ChatGPT.",
        category="Course",
    ),
    Course(
        "Generative AI Software Engineering Specialization",
        "https://www.coursera.org/specializations/generative-ai-software-engineering",
        "Coursera (Vanderbilt)",
        "vibe:course:coursera-vanderbilt-genai-se",
        tools=(),
        first_indexed_at=_INDEXED,
        blurb="Multi-course path on orchestrating coding agents to build, ship, and maintain real applications.",
        category="Course",
    ),
    Course(
        "Practical Prompt Engineering",
        "https://frontendmasters.com/courses/prompt-engineering/",
        "Frontend Masters",
        "vibe:course:fem-practical-prompt-engineering",
        tools=(),
        first_indexed_at=_INDEXED,
        blurb="Sabrina Goldfarb teaches prompting strategies that transfer across Cursor, Copilot, and Claude.",
        category="Course",
    ),
    Course(
        "Vibe Coding for Developers",
        "https://www.coursera.org/specializations/vibe-coding-for-developers",
        "Coursera (Edureka)",
        "vibe:course:coursera-vibe-coding-for-developers",
        tools=(),
        first_indexed_at=_INDEXED,
        blurb="Multi-tool specialization on prompt engineering, AI-assisted editing, full-stack agents, and shipping production code.",
        category="Course",
    ),
    Course(
        "Structured Vibe Coding with AI Coding Agents",
        "https://www.linkedin.com/learning/structured-vibe-coding-with-ai-coding-agents",
        "LinkedIn Learning",
        "vibe:course:linkedin-structured-vibe-coding",
        tools=(),
        first_indexed_at=_INDEXED,
        blurb="Ray Villalobos on driver/navigator pair programming with agents \u2014 agentic modes, system prompts, responsible practice.",
        category="Course",
    ),
    Course(
        "AI Development Tools",
        "https://www.codecademy.com/learn/ai-development-tools",
        "Codecademy",
        "vibe:course:codecademy-ai-dev-tools",
        tools=(),
        first_indexed_at=_INDEXED,
        blurb="AI-assisted dev with Codex CLI, spec-driven development, TDD, and context engineering \u2014 the practice past basic vibe coding.",
        category="Course",
    ),
    Course(
        "Cursor & Claude Code: Professional AI Setup",
        "https://frontendmasters.com/courses/pro-ai/",
        "Frontend Masters",
        "vibe:course:fem-pro-ai",
        tools=("cursor", "claude-code"),
        first_indexed_at=_INDEXED,
        blurb="Cross-tool workflow course: inline edits, markdown plans, background agents, MCP servers.",
        category="Course",
    ),
    Course(
        "Agentic Coding Recommendations",
        "https://lucumr.pocoo.org/2025/6/12/agentic-coding/",
        "Armin Ronacher",
        "vibe:course:ronacher-agentic-coding",
        tools=(),
        first_indexed_at=_INDEXED,
        published_at=date(2025, 6, 12),
        blurb="Practitioner playbook: hand the agent a job, pick boring stacks, keep tools fast and observable.",
        category="Best Practices",
    ),
    Course(
        "Embracing the parallel coding agent lifestyle",
        "https://simonwillison.net/2025/Oct/5/parallel-coding-agents/",
        "Simon Willison",
        "vibe:course:simonw-parallel-agents",
        tools=(),
        first_indexed_at=_INDEXED,
        published_at=date(2025, 10, 5),
        blurb="How to actually fire off multiple agents at once across worktrees without losing the plot.",
        category="Best Practices",
    ),
    Course(
        "Code research projects with async coding agents",
        "https://simonwillison.net/2025/Nov/6/async-code-research/",
        "Simon Willison",
        "vibe:course:simonw-async-research",
        tools=(),
        first_indexed_at=_INDEXED,
        published_at=date(2025, 11, 6),
        blurb="Use coding agents as throwaway research workers to answer library and POC questions fast.",
        category="Best Practices",
    ),
    Course(
        "Ralph Wiggum as a software engineer",
        "https://ghuntley.com/ralph/",
        "Geoffrey Huntley",
        "vibe:course:ghuntley-ralph",
        tools=(),
        first_indexed_at=_INDEXED,
        published_at=date(2025, 9, 8),
        blurb="The Ralph loop: a bash while-loop with fresh context per iteration that ships specs into code overnight.",
        category="Best Practices",
    ),
    Course(
        "Revenge of the junior developer",
        "https://sourcegraph.com/blog/revenge-of-the-junior-developer",
        "Steve Yegge · Sourcegraph",
        "vibe:course:yegge-revenge-junior-dev",
        tools=(),
        first_indexed_at=_INDEXED,
        published_at=date(2025, 3, 22),
        blurb="Yegge's map of the six waves from autocomplete to agent fleets and how to ride them as a working dev.",
        category="Discussion",
    ),
    Course(
        "The rise of the professional vibe coder",
        "https://www.lennysnewsletter.com/p/getting-paid-to-vibe-code",
        "Lenny's Newsletter",
        "vibe:course:lennys-professional-vibe-coder",
        tools=(),
        first_indexed_at=_INDEXED,
        published_at=date(2026, 2, 8),
        blurb="Lovable's first pro vibe coder on PRD-driven workflows, parallel prototypes, and the 4×4 debugging method.",
        category="Discussion",
    ),
    Course(
        "How to Use Claude Code, Codex, and Cursor for Multi-Agent Vibe Coding",
        "https://www.youtube.com/watch?v=8OX_ZjQTu34",
        "YouTube · Theo (t3.gg)",
        "vibe:course:theo-multi-agent-vibe-coding",
        tools=("claude-code", "codex", "cursor"),
        first_indexed_at=_INDEXED,
        blurb="Hour-long livestream wiring three coding agents into one shipping workflow.",
        category="Talk",
    ),
    Course(
        "Vibe Coding Tutorial: Cursor + 4 Projects",
        "https://www.youtube.com/playlist?list=PLrBQjB7hqzJlsP-oKSbJ0I0otzevS17xA",
        "YouTube · Riley Brown",
        "vibe:course:riley-brown-cursor-playlist",
        tools=("cursor",),
        first_indexed_at=_INDEXED,
        blurb="Long-form practitioner walkthroughs that build real apps end-to-end by voice in Cursor.",
    ),
    Course(
        "Claude Code Deep Mastery",
        "https://www.youtube.com/playlist?list=PLS_o2ayVCKvBR3jawG9JFIzJ1vXffi8fS",
        "YouTube · IndyDevDan",
        "vibe:course:indydevdan-claude-code-mastery",
        tools=("claude-code",),
        first_indexed_at=_INDEXED,
        blurb="Long-running playlist on planning, building, and agent orchestration patterns for shipping with coding agents.",
        category="Tutorial",
    ),
    Course(
        "Engineering practices that make coding agents work",
        "https://www.youtube.com/watch?v=owmJyKVu5f8",
        "YouTube · Pragmatic Engineer",
        "vibe:course:pragmatic-eng-coding-agents",
        tools=(),
        first_indexed_at=_INDEXED,
        blurb="Simon Willison on tests, scaffolding, and review habits that make coding agents actually ship.",
        category="Talk",
    ),

    # Claude Code
    Course(
        "Claude Code overview",
        "https://code.claude.com/docs/en/overview",
        "Anthropic docs",
        "vibe:course:anthropic-claude-code-overview",
        tools=("claude-code",),
        first_indexed_at=_INDEXED,
        blurb="Official intro covering install paths, supported IDEs, and where Claude Code fits in your workflow.",
    ),
    Course(
        "Best practices for Claude Code",
        "https://code.claude.com/docs/en/best-practices",
        "Anthropic docs",
        "vibe:course:anthropic-claude-code-best-practices",
        tools=("claude-code",),
        first_indexed_at=_INDEXED,
        blurb="Anthropic's distilled patterns for context, memory, subagents, and long-running tasks.",
        category="Best Practices",
    ),
    Course(
        "Claude Code: A Highly Agentic Coding Assistant",
        "https://www.deeplearning.ai/courses/claude-code-a-highly-agentic-coding-assistant",
        "DeepLearning.AI",
        "vibe:course:dlai-claude-code-agentic",
        tools=("claude-code",),
        first_indexed_at=_INDEXED,
        blurb="Free short course with Elie Schoppik covering subagents, GitHub integration, MCP, and Figma-to-app builds.",
        category="Course",
    ),
    Course(
        "How does Claude Code actually work?",
        "https://www.youtube.com/watch?v=I82j7AzMU80",
        "YouTube · Theo (t3.gg)",
        "vibe:course:theo-claude-code-internals",
        tools=("claude-code",),
        first_indexed_at=_INDEXED,
        blurb="Walkthrough of Claude Code's loop, tool calls, and what makes its agentic design different from autocomplete.",
        category="Discussion",
    ),
    Course(
        "Claude Code: Software Engineering with Generative AI Agents",
        "https://www.coursera.org/learn/claude-code",
        "Coursera (Vanderbilt)",
        "vibe:course:coursera-vanderbilt-claude-code",
        tools=("claude-code",),
        first_indexed_at=_INDEXED,
        blurb="Orchestrate Claude Code like a tech lead: Best-of-N, CLAUDE.md, parallel git branch workflows.",
        category="Course",
    ),
    Course(
        "Claude Code 4: Agentic Coding for Professional Developers",
        "https://www.linkedin.com/learning/claude-code-4-agentic-coding-for-professional-developers",
        "LinkedIn Learning",
        "vibe:course:linkedin-claude-code-4",
        tools=("claude-code",),
        first_indexed_at=_INDEXED,
        published_at=date(2025, 6, 25),
        blurb="Ray Villalobos walks pro devs through Claude Code setup, slash commands, and GitHub PR flows.",
        category="Course",
    ),

    # OpenAI Codex
    Course(
        "Codex Quickstart",
        "https://developers.openai.com/codex/quickstart",
        "OpenAI docs",
        "vibe:course:openai-codex-quickstart",
        tools=("codex",),
        first_indexed_at=_INDEXED,
        blurb="Install Codex CLI, sign in with ChatGPT or API key, and run your first repo-aware task.",
    ),
    Course(
        "Codex CLI reference",
        "https://developers.openai.com/codex/cli",
        "OpenAI docs",
        "vibe:course:openai-codex-cli-reference",
        tools=("codex",),
        first_indexed_at=_INDEXED,
        blurb="Full CLI surface: approval modes, /model switching, screenshot input, and sandbox controls.",
    ),

    # Cursor
    Course(
        "Cursor Quickstart",
        "https://cursor.com/docs/get-started/quickstart",
        "Cursor docs",
        "vibe:course:cursor-quickstart",
        tools=("cursor",),
        first_indexed_at=_INDEXED,
        blurb="From install to first useful edit: codebase Q&A, inline edits, and reviewing agent output.",
    ),
    Course(
        "Learn Cursor: Official Tutorials",
        "https://cursor.com/learn",
        "Cursor docs",
        "vibe:course:cursor-learn",
        tools=("cursor",),
        first_indexed_at=_INDEXED,
        blurb="Bite-size lessons on AI foundations, coding with agents, and reviewing & testing agent diffs.",
    ),
    Course(
        "Cursor 2.0: Full Tutorial for Beginners",
        "https://www.youtube.com/watch?v=l30Eb76Tk5s",
        "YouTube",
        "vibe:course:cursor-2-beginner-tutorial",
        tools=("cursor",),
        first_indexed_at=_INDEXED,
        blurb="End-to-end beginner walkthrough of Cursor 2.0's Composer, Agent mode, and parallel-agents window.",
    ),
    Course(
        "Cursor AI: A Guide With 10 Practical Examples",
        "https://www.datacamp.com/tutorial/cursor-ai-code-editor",
        "DataCamp",
        "vibe:course:datacamp-cursor-10-examples",
        tools=("cursor",),
        first_indexed_at=_INDEXED,
        blurb="Ten concrete prompts spanning refactors, tests, and migrations that show Cursor's strengths.",
    ),

    # Windsurf
    Course(
        "Windsurf Editor: official site",
        "https://windsurf.com/",
        "Windsurf",
        "vibe:course:windsurf-official",
        tools=("windsurf",),
        first_indexed_at=_INDEXED,
        blurb="Download Windsurf and learn how Cascade plans multi-step edits across your project.",
    ),
    Course(
        "Windsurf Tutorial for Beginners (AI Code Editor)",
        "https://www.youtube.com/watch?v=8TcWGk1DJVs",
        "YouTube",
        "vibe:course:windsurf-beginner-yt",
        tools=("windsurf",),
        first_indexed_at=_INDEXED,
        blurb="Beginner tour of Cascade, Supercomplete, and how Windsurf differs from Cursor on multi-file edits.",
    ),

    # Lovable
    Course(
        "Lovable Quick start",
        "https://docs.lovable.dev/introduction/getting-started",
        "Lovable docs",
        "vibe:course:lovable-quickstart",
        tools=("lovable",),
        first_indexed_at=_INDEXED,
        blurb="Spin up your first Lovable project, connect Supabase, and ship a working full-stack app.",
    ),
    Course(
        "Lovable Tutorials: Build Apps with AI",
        "https://lovable.dev/videos/tutorial",
        "Lovable",
        "vibe:course:lovable-tutorials",
        tools=("lovable",),
        first_indexed_at=_INDEXED,
        blurb="A video library walking through auth, databases, payments, and deploys in Lovable.",
    ),
    Course(
        "Lovable AI: How to Build an App & Upload to App Store",
        "https://www.youtube.com/watch?v=N7NsveOiG-g",
        "YouTube",
        "vibe:course:lovable-app-store-yt",
        tools=("lovable",),
        first_indexed_at=_INDEXED,
        blurb="End-to-end mobile build with Lovable, including App Store submission and review prep.",
    ),

    # Bolt
    Course(
        "Introduction to Bolt",
        "https://support.bolt.new/building/intro-bolt",
        "Bolt.new docs",
        "vibe:course:bolt-intro",
        tools=("bolt-new",),
        first_indexed_at=_INDEXED,
        blurb="How Bolt's in-browser WebContainers run your prompted project end to end, no setup required.",
    ),
    Course(
        "Bolt.new AI Tutorial for Beginners",
        "https://www.youtube.com/watch?v=5zfOitaKfmM",
        "YouTube",
        "vibe:course:bolt-beginner-yt",
        tools=("bolt-new",),
        first_indexed_at=_INDEXED,
        blurb="Prompt-to-deployment walkthrough of a small full-stack app inside Bolt's browser environment.",
    ),
    Course(
        "Bolt.new: Build a Full-Stack App in Minutes",
        "https://www.codecademy.com/article/build-an-app-with-bolt-new",
        "Codecademy",
        "vibe:course:codecademy-bolt-fullstack",
        tools=("bolt-new",),
        first_indexed_at=_INDEXED,
        blurb="Build a workout tracker with Bolt: UI prompts, Supabase wiring, and deploy to production.",
    ),

    # v0
    Course(
        "v0 Quickstart",
        "https://v0.app/docs/quickstart",
        "Vercel docs",
        "vibe:course:v0-quickstart",
        tools=("v0",),
        first_indexed_at=_INDEXED,
        blurb="Describe a UI, iterate with chat, then push the generated components into your Next.js repo.",
    ),
    Course(
        "UI with v0",
        "https://vercel.com/academy/ai-sdk/ui-with-v0",
        "Vercel Academy",
        "vibe:course:vercel-academy-ui-v0",
        tools=("v0",),
        first_indexed_at=_INDEXED,
        blurb="Free Vercel lesson on prompting v0 to produce production-grade shadcn/ui components.",
        category="Course",
    ),

    # Replit Agent
    Course(
        "Replit Agent docs",
        "https://docs.replit.com/core-concepts/agent",
        "Replit docs",
        "vibe:course:replit-agent-docs",
        tools=("replit-agent",),
        first_indexed_at=_INDEXED,
        blurb="Reference for Plan mode, browser-based self-testing, and connecting Agent to external data.",
    ),
    Course(
        "Replit Agent: A Guide With Practical Examples",
        "https://www.datacamp.com/tutorial/replit-agent-ai-code-editor",
        "DataCamp",
        "vibe:course:datacamp-replit-agent",
        tools=("replit-agent",),
        first_indexed_at=_INDEXED,
        blurb="Hands-on examples of prompting Replit Agent to build, test, and deploy a small web app.",
    ),
    Course(
        "Building AI-powered apps with Replit Agent",
        "https://neon.com/guides/replit-neon",
        "Neon guides",
        "vibe:course:neon-replit-agent",
        tools=("replit-agent",),
        first_indexed_at=_INDEXED,
        blurb="Wire Replit Agent to a Neon Postgres database and ship a data-driven app from one prompt.",
    ),

    # Devin
    Course(
        "Devin: Get started",
        "https://docs.devin.ai/get-started/devin-intro",
        "Devin docs",
        "vibe:course:devin-intro",
        tools=("devin",),
        first_indexed_at=_INDEXED,
        blurb="Onboard Devin, scope your first task well, and understand when to use it vs. an IDE agent.",
    ),
    Course(
        "Best practices for delegating to Devin",
        "https://docs.devin.ai/get-started/best-practices",
        "Devin docs",
        "vibe:course:devin-best-practices",
        tools=("devin",),
        first_indexed_at=_INDEXED,
        blurb="How to brief Devin so it ships: task scoping, repo setup, knowledge files, and review habits.",
        category="Best Practices",
    ),
    Course(
        "Software Development With Devin: Setup and First Pull Request",
        "https://www.datacamp.com/tutorial/devin-ai",
        "DataCamp",
        "vibe:course:datacamp-devin-first-pr",
        tools=("devin",),
        first_indexed_at=_INDEXED,
        published_at=date(2025, 6, 26),
        blurb="Set up Devin, connect GitHub, prompt it to modernize a repo, monitor execution, and ship the PR.",
    ),

    # Antigravity
    Course(
        "Google Antigravity Documentation",
        "https://antigravity.google/docs/home",
        "Google docs",
        "vibe:course:antigravity-docs",
        tools=("antigravity",),
        first_indexed_at=_INDEXED,
        blurb="Reference for Editor and Manager views, agent orchestration, and switching between Gemini and Claude.",
    ),
    Course(
        "Getting Started with Google Antigravity",
        "https://codelabs.developers.google.com/getting-started-google-antigravity",
        "Google Codelabs",
        "vibe:course:antigravity-codelab",
        tools=("antigravity",),
        first_indexed_at=_INDEXED,
        blurb="Step-by-step codelab: install Antigravity, set up Agent Manager, and ship a first agentic project.",
    ),

    # Jules
    Course(
        "Jules: Getting started",
        "https://jules.google/docs/",
        "Google docs",
        "vibe:course:jules-getting-started",
        tools=("jules",),
        first_indexed_at=_INDEXED,
        blurb="Connect Jules to a GitHub repo, submit your first async task, and review its PR.",
    ),
    Course(
        "Practical Agentic Coding with Google Jules",
        "https://machinelearningmastery.com/practical-agentic-coding-with-google-jules/",
        "Machine Learning Mastery",
        "vibe:course:mlm-jules-walkthrough",
        tools=("jules",),
        first_indexed_at=_INDEXED,
        blurb="Connect Jules to a repo, prompt refactors and unit tests, review the plan, publish branch, open PR.",
    ),

    # GitHub Copilot
    Course(
        "Quickstart for GitHub Copilot",
        "https://docs.github.com/en/copilot/get-started/quickstart",
        "GitHub docs",
        "vibe:course:gh-copilot-quickstart",
        tools=("github-copilot",),
        first_indexed_at=_INDEXED,
        blurb="Enable Copilot, choose an IDE, and try chat, inline suggestions, and the in-repo agent.",
    ),
    Course(
        "Best practices for using GitHub Copilot",
        "https://docs.github.com/en/copilot/get-started/best-practices",
        "GitHub docs",
        "vibe:course:gh-copilot-best-practices",
        tools=("github-copilot",),
        first_indexed_at=_INDEXED,
        blurb="Prompt patterns, repo setup, and review habits that make Copilot's output trustworthy.",
        category="Best Practices",
    ),
    Course(
        "GitHub Copilot CLI for Beginners",
        "https://github.blog/ai-and-ml/github-copilot/github-copilot-cli-for-beginners-getting-started-with-github-copilot-cli/",
        "GitHub blog",
        "vibe:course:gh-copilot-cli-beginners",
        tools=("github-copilot",),
        first_indexed_at=_INDEXED,
        blurb="Install Copilot CLI, authenticate, and run your first terminal-driven prompts and code edits.",
    ),
    Course(
        "Get started with GitHub Copilot in VS Code",
        "https://code.visualstudio.com/docs/copilot/getting-started",
        "VS Code docs",
        "vibe:course:vscode-copilot-getting-started",
        tools=("github-copilot",),
        first_indexed_at=_INDEXED,
        blurb="Sign in, configure chat participants, and try Copilot's edits and agent modes inside VS Code.",
    ),
    Course(
        "Copilot ask, edit, and agent modes: what they do and when to use them",
        "https://github.blog/ai-and-ml/github-copilot/copilot-ask-edit-and-agent-modes-what-they-do-and-when-to-use-them/",
        "GitHub blog",
        "vibe:course:gh-copilot-modes",
        tools=("github-copilot",),
        first_indexed_at=_INDEXED,
        blurb="Practical walkthrough of Copilot's three chat modes with concrete \"reach for this when\" examples.",
        category="Best Practices",
    ),
    Course(
        "Generative AI for Software Developers Specialization",
        "https://www.coursera.org/specializations/microsoft-copilot-for-software-development",
        "Coursera (Microsoft)",
        "vibe:course:coursera-microsoft-copilot-spec",
        tools=("github-copilot",),
        first_indexed_at=_INDEXED,
        blurb="Four-course Microsoft path on shipping code with GitHub Copilot across VS Code workflows.",
        category="Course",
    ),
    Course(
        "GitHub Copilot in Practice",
        "https://www.pluralsight.com/paths/github-copilot-in-practice",
        "Pluralsight",
        "vibe:course:pluralsight-copilot-in-practice",
        tools=("github-copilot",),
        first_indexed_at=_INDEXED,
        blurb="Eight-course path on prompting Copilot for reviews, tests, refactors, and secure development.",
        category="Course",
    ),
    Course(
        "Building applications with GitHub Copilot agent mode",
        "https://learn.microsoft.com/en-us/training/modules/github-copilot-agent-mode/",
        "Microsoft Learn",
        "vibe:course:ms-learn-copilot-agent-mode",
        tools=("github-copilot",),
        first_indexed_at=_INDEXED,
        blurb="Free six-unit module on driving Copilot Agent Mode to build, refactor, and fix codebases.",
        category="Course",
    ),

    # Aider
    Course(
        "Aider: Installation",
        "https://aider.chat/docs/install.html",
        "Aider docs",
        "vibe:course:aider-install",
        tools=("aider",),
        first_indexed_at=_INDEXED,
        blurb="Install aider with pipx, point it at Claude or GPT, and start pairing in your terminal.",
    ),
    Course(
        "Aider Documentation",
        "https://aider.chat/docs/",
        "Aider docs",
        "vibe:course:aider-docs",
        tools=("aider",),
        first_indexed_at=_INDEXED,
        blurb="Full reference for repo maps, model selection, edit formats, and Git-backed commit flow.",
    ),
    Course(
        "Aider tutorial videos",
        "https://aider.chat/docs/usage/tutorials.html",
        "Aider docs",
        "vibe:course:aider-tutorial-videos",
        tools=("aider",),
        first_indexed_at=_INDEXED,
        blurb="Community videos showing real refactors, test-writing, and multi-file edits with Aider.",
    ),

    # Cline
    Course(
        "Cline Documentation",
        "https://docs.cline.bot/home",
        "Cline docs",
        "vibe:course:cline-docs",
        tools=("cline",),
        first_indexed_at=_INDEXED,
        blurb="Install Cline in VS Code, pick a provider, and use Plan/Act mode to control agentic edits.",
    ),
    Course(
        "Getting Started with Cline: The Best VS Code AI Plugin",
        "https://www.youtube.com/watch?v=f33Fw6NiPpw",
        "YouTube",
        "vibe:course:cline-getting-started-yt",
        tools=("cline",),
        first_indexed_at=_INDEXED,
        blurb="Walkthrough of Cline's Plan/Act split, browser tool use, and approval-gated terminal commands.",
    ),
    Course(
        "Cline AI: A Guide With Nine Practical Examples",
        "https://www.datacamp.com/tutorial/cline-ai",
        "DataCamp",
        "vibe:course:datacamp-cline",
        tools=("cline",),
        first_indexed_at=_INDEXED,
        blurb="Nine concrete tasks, from bugfix to refactor, showing Cline's tool-use patterns in practice.",
    ),

    # Continue
    Course(
        "What is Continue?",
        "https://docs.continue.dev/",
        "Continue docs",
        "vibe:course:continue-overview",
        tools=("continue",),
        first_indexed_at=_INDEXED,
        blurb="Overview of Continue's Agent mode, context providers, and source-controlled config model.",
    ),
    Course(
        "Continue Quick Start",
        "https://docs.continue.dev/ide-extensions/quick-start",
        "Continue docs",
        "vibe:course:continue-quickstart",
        tools=("continue",),
        first_indexed_at=_INDEXED,
        blurb="Install the VS Code or JetBrains extension, pick a model, and run your first Agent task.",
    ),
    Course(
        "Continue.dev: The Complete Local AI Coding Assistant Setup",
        "https://www.sitepoint.com/continuedev-for-developers-the-complete-local-ai-coding-assistant-setup/",
        "SitePoint",
        "vibe:course:sitepoint-continue-local",
        tools=("continue",),
        first_indexed_at=_INDEXED,
        blurb="Configure Continue to run fully local with Ollama, including autocomplete and chat models.",
    ),

    # Roo Code
    Course(
        "Roo Code Docs",
        "https://docs.roocode.com/",
        "Roo Code docs",
        "vibe:course:roo-code-docs",
        tools=("roo-code",),
        first_indexed_at=_INDEXED,
        blurb="Reference for Roo's multi-mode agents, custom instructions, and per-mode model routing.",
    ),
    Course(
        "Installing Roo Code",
        "https://docs.roocode.com/getting-started/installing/",
        "Roo Code docs",
        "vibe:course:roo-code-install",
        tools=("roo-code",),
        first_indexed_at=_INDEXED,
        blurb="Install Roo Code from VS Marketplace or Open VSX and pick a provider for your first task.",
    ),
    Course(
        "Roo Code: A Guide With 7 Practical Examples",
        "https://www.datacamp.com/tutorial/roo-code",
        "DataCamp",
        "vibe:course:datacamp-roo-code",
        tools=("roo-code",),
        first_indexed_at=_INDEXED,
        blurb="Seven hands-on tasks showing Roo's Architect, Code, and Ask modes on a real project.",
    ),

    # OpenCode
    Course(
        "OpenCode docs",
        "https://opencode.ai/docs/",
        "OpenCode docs",
        "vibe:course:opencode-docs",
        tools=("opencode",),
        first_indexed_at=_INDEXED,
        blurb="Install the terminal agent, connect a provider, and run build vs. plan agents on your repo.",
    ),
    Course(
        "sst/opencode on GitHub",
        "https://github.com/sst/opencode",
        "GitHub · sst/opencode",
        "vibe:course:opencode-github",
        tools=("opencode",),
        first_indexed_at=_INDEXED,
        blurb="Source repo for OpenCode with config examples, LSP setup notes, and provider matrix.",
    ),
    Course(
        "OpenCode Quickstart: Install, Configure, and Use the Terminal AI Coding Agent",
        "https://dev.to/rosgluk/opencode-quickstart-install-configure-and-use-the-terminal-ai-coding-agent-4kcb",
        "DEV.to",
        "vibe:course:devto-opencode-quickstart",
        tools=("opencode",),
        first_indexed_at=_INDEXED,
        blurb="Step-by-step setup from install to AGENTS.md, with model switching and first real tasks.",
    ),

    # Goose
    Course(
        "Goose Documentation",
        "https://block.github.io/goose/",
        "Block / Goose docs",
        "vibe:course:goose-docs",
        tools=("goose",),
        first_indexed_at=_INDEXED,
        blurb="Install Goose desktop or CLI, connect a model, and add extensions for your workflow.",
    ),
    Course(
        "Free Agentic Coding with Goose",
        "https://www.kdnuggets.com/free-agentic-coding-with-goose",
        "KDnuggets",
        "vibe:course:kdnuggets-goose",
        tools=("goose",),
        first_indexed_at=_INDEXED,
        blurb="Practical tour of Goose extensions for shell, Git, and browser automation, fully local.",
    ),

    # Warp
    Course(
        "Using Agents in Warp",
        "https://docs.warp.dev/agents/using-agents",
        "Warp docs",
        "vibe:course:warp-using-agents",
        tools=("warp",),
        first_indexed_at=_INDEXED,
        blurb="Toggle Agent Mode, hand off multi-step shell tasks, and inspect Warp's auto-correcting plans.",
    ),
    Course(
        "Warp AI Terminal: Agent Mode",
        "https://www.warp.dev/ai",
        "Warp",
        "vibe:course:warp-ai-overview",
        tools=("warp",),
        first_indexed_at=_INDEXED,
        blurb="Overview of Warp's natural-language CLI: chained commands, output reading, and model choice.",
    ),
    Course(
        "Warp Terminal Tutorial: AI-Powered Features",
        "https://www.datacamp.com/tutorial/warp-terminal-tutorial",
        "DataCamp",
        "vibe:course:datacamp-warp",
        tools=("warp",),
        first_indexed_at=_INDEXED,
        blurb="Hands-on walk through Agent Mode, workflows, and shell suggestions on real day-to-day tasks.",
    ),

    # Zed
    Course(
        "Zed Agent Panel",
        "https://zed.dev/docs/ai/agent-panel",
        "Zed docs",
        "vibe:course:zed-agent-panel",
        tools=("zed",),
        first_indexed_at=_INDEXED,
        blurb="Open Zed's agent panel, add @-context, and follow the agent's edits in real time.",
    ),
    Course(
        "Zed AI overview",
        "https://zed.dev/docs/ai/overview",
        "Zed docs",
        "vibe:course:zed-ai-overview",
        tools=("zed",),
        first_indexed_at=_INDEXED,
        blurb="How Zed wires up providers, edit prediction, inline assistant, and external agents.",
    ),
    Course(
        "External Agents in Zed",
        "https://zed.dev/docs/ai/external-agents",
        "Zed docs",
        "vibe:course:zed-external-agents",
        tools=("zed",),
        first_indexed_at=_INDEXED,
        blurb="Run Claude Agent, Gemini CLI, or Codex inside Zed's agent panel via the ACP adapter.",
    ),
    Course(
        "How to use the Agent Panel in Zed",
        "https://www.youtube.com/watch?v=K1K84PSgp5g",
        "YouTube · Zed Industries",
        "vibe:course:zed-agent-panel-yt",
        tools=("zed",),
        first_indexed_at=_INDEXED,
        blurb="Video walkthrough of Zed's Agent Panel: configuring providers, prompting edits, and reviewing change hunks.",
    ),

    # Kiro
    Course(
        "Kiro: Get started",
        "https://kiro.dev/docs/",
        "Kiro docs",
        "vibe:course:kiro-docs",
        tools=("kiro",),
        first_indexed_at=_INDEXED,
        blurb="Install Kiro, sign in, and learn its spec-first flow: requirements, design, and tasks files.",
    ),
    Course(
        "Your first Kiro project",
        "https://kiro.dev/docs/getting-started/first-project/",
        "Kiro docs",
        "vibe:course:kiro-first-project",
        tools=("kiro",),
        first_indexed_at=_INDEXED,
        blurb="Build a feature end-to-end in Kiro: generate the spec triad and let the agent execute tasks.",
    ),

    # Factory
    Course(
        "Factory: Quickstart",
        "https://docs.factory.ai/cli/getting-started/quickstart",
        "Factory docs",
        "vibe:course:factory-quickstart",
        tools=("factory",),
        first_indexed_at=_INDEXED,
        blurb="Install the droid CLI, sign in, and run your first specialized droid against a repo.",
    ),
    Course(
        "Factory IDE: AI Coding Agents",
        "https://factory.ai/product/ide",
        "Factory",
        "vibe:course:factory-ide",
        tools=("factory",),
        first_indexed_at=_INDEXED,
        blurb="Overview of Factory's droid IDE: parallel droids, review flow, and SDLC coverage.",
    ),
    Course(
        "How to Code with Droids",
        "https://buckhouse.medium.com/how-to-code-with-droids-e1f1c52f9482",
        "Medium · James Buckhouse",
        "vibe:course:buckhouse-droids",
        tools=("factory",),
        first_indexed_at=_INDEXED,
        blurb="Mental model for delegating to droids: scoping, briefing, and accepting work products.",
        category="Best Practices",
    ),

    # Gemini CLI
    Course(
        "Gemini CLI on GitHub",
        "https://github.com/google-gemini/gemini-cli",
        "GitHub · google-gemini/gemini-cli",
        "vibe:course:gemini-cli-github",
        tools=("gemini-cli",),
        first_indexed_at=_INDEXED,
        blurb="Official open-source repo for the Gemini terminal agent \u2014 install, configure, and contribute.",
    ),
    Course(
        "Gemini CLI: Code & Create with an Open-Source Agent",
        "https://learn.deeplearning.ai/courses/gemini-cli-code-and-create-with-an-open-source-agent/information",
        "DeepLearning.AI",
        "vibe:course:dlai-gemini-cli",
        tools=("gemini-cli",),
        first_indexed_at=_INDEXED,
        blurb="Jack Wotherspoon (Google) on context management, MCP servers, and multi-step workflows with Gemini CLI.",
        category="Course",
    ),
    Course(
        "Mastering Gemini CLI: From Installation to Advanced Use-Cases",
        "https://cloud.google.com/blog/topics/developers-practitioners/mastering-gemini-cli-your-complete-guide-from-installation-to-advanced-use-cases",
        "Google Cloud Blog",
        "vibe:course:google-cloud-gemini-cli-guide",
        tools=("gemini-cli",),
        first_indexed_at=_INDEXED,
        blurb="Google Cloud's installation-through-advanced walkthrough of Gemini CLI for real coding workflows.",
    ),
]


# ─── PEOPLE ───────────────────────────────────────────────────────────────────

PEOPLE: list[Person] = [
    Person(
        "Andrej Karpathy", "karpathy", "x", "https://x.com/karpathy",
        "vibe:person:x:karpathy",
        "Coined \"vibe coding\"; posts the canonical hands-on takes on building real software with LLMs.",
    ),
    Person(
        "Simon Willison", "simonwillison", "blog", "https://simonwillison.net/",
        "vibe:person:blog:simonwillison",
        "Near-daily deep notes on every coding-agent release, model, and CLI workflow.",
    ),
    Person(
        "Theo Browne", "t3dotgg", "youtube", "https://www.youtube.com/@t3dotgg",
        "vibe:person:youtube:t3dotgg",
        "Same-week reviews of Cursor, Claude Code, and Codex with opinionated full-stack TypeScript context.",
    ),
    Person(
        "Fireship", "fireship", "youtube", "https://www.youtube.com/@Fireship",
        "vibe:person:youtube:fireship",
        "Fast, technically tight breakdowns of every new agentic-coding tool the day it ships.",
    ),
    Person(
        "ThePrimeagen", "ThePrimeagen", "youtube", "https://www.youtube.com/@ThePrimeagen",
        "vibe:person:youtube:theprimeagen",
        "Skeptical, terminal-pilled takes on AI coding tools and his \"99\" pattern as a vibe-coding alternative.",
    ),
    Person(
        "IndyDevDan", "indydevdan", "youtube", "https://www.youtube.com/@indydevdan",
        "vibe:person:youtube:indydevdan",
        "Anti-hype, tactical Claude Code playbooks \u2014 hooks, sub-agents, multi-agent observability.",
    ),
    Person(
        "Michael Truell", "mntruell", "x", "https://x.com/mntruell",
        "vibe:person:x:mntruell",
        "Cursor's CEO; shipping notes and product philosophy from the company defining the AI-IDE category.",
    ),
    Person(
        "Guillermo Rauch", "rauchg", "x", "https://x.com/rauchg",
        "vibe:person:x:rauchg",
        "Vercel CEO showing how v0 is used in real Git workflows \u2014 prompt-to-PR patterns at production scale.",
    ),
    Person(
        "Birgitta Böckeler", "birgittabockeler", "blog", "https://birgitta.info/",
        "vibe:person:blog:birgittabockeler",
        "Thoughtworks' lead for AI-assisted software delivery; long-form memos on context engineering and agent workflows.",
    ),
    Person(
        "Mckay Wrigley", "mckaywrigley", "x", "https://x.com/mckaywrigley",
        "vibe:person:x:mckaywrigley",
        "Practical workflows for shipping with Claude Code, Codex, Cursor, and Gemini CLI \u2014 prompts, recipes, parallel agents.",
    ),
    Person(
        "Dan Shipper", "danshipper", "newsletter", "https://every.to/@danshipper",
        "vibe:person:newsletter:danshipper",
        "Every's CEO chronicling how a team of 15 ships multiple AI products with engineers writing almost no code by hand.",
    ),
    Person(
        "Boris Cherny", "bcherny", "x", "https://x.com/bcherny",
        "vibe:person:x:bcherny",
        "Creator of Claude Code; posts setup tips, internals, and how the team actually uses it day-to-day.",
    ),
    Person(
        "Geoffrey Huntley", "ghuntley", "blog", "https://ghuntley.com/",
        "vibe:person:blog:ghuntley",
        "Originated the \"Ralph loop\" autonomous-agent pattern; field notes from running coding agents at the edge.",
    ),
    Person(
        "Steve Yegge", "Steve_Yegge", "blog", "https://steve-yegge.medium.com/",
        "vibe:person:blog:steveyegge",
        "Sourcegraph Amp lead writes the long-form essays mapping where coding agents and agent fleets are headed.",
    ),
    Person(
        "Armin Ronacher", "mitsuhiko", "blog", "https://lucumr.pocoo.org/",
        "vibe:person:blog:mitsuhiko",
        "Flask creator's sharp, skeptical essays on agent design, tool ergonomics, and AI-PR review bottlenecks.",
    ),
    Person(
        "swyx", "swyx", "newsletter", "https://www.latent.space/",
        "vibe:person:newsletter:swyx",
        "Latent Space podcast / newsletter; interviews the people actually building coding agents and harnesses.",
    ),
    Person(
        "Gergely Orosz", "pragmaticengineer", "newsletter",
        "https://newsletter.pragmaticengineer.com/",
        "vibe:person:newsletter:pragmaticengineer",
        "The Pragmatic Engineer; deep-dives on AI tooling, agent workflows, and what AI is doing to the craft.",
    ),
    Person(
        "Syntax", "syntaxfm", "podcast", "https://syntax.fm/",
        "vibe:person:podcast:syntaxfm",
        "Wes Bos and Scott Tolinski's web-dev podcast; regular Claude Code, Cursor, and agent-mode episodes.",
    ),
    Person(
        "The Changelog", "changelog", "podcast", "https://changelog.com/podcast",
        "vibe:person:podcast:changelog",
        "Long-running software interview podcast with frequent episodes on coding agents and how teams actually use them.",
    ),
    Person(
        "TLDR AI", "tldrnewsletter", "newsletter", "https://tldr.tech/ai",
        "vibe:person:newsletter:tldr-ai",
        "Daily 5-minute roundup of AI news, papers, and tooling \u2014 a fast scan to stay on top of the field.",
    ),
]


# Platforms we treat as "Stay current" (subscribable feeds) vs "Creators to
# follow" (people you'd open the profile of). Keeps each section coherent.
STAY_CURRENT_PLATFORMS: frozenset[str] = frozenset({"newsletter", "podcast"})


@dataclass(frozen=True)
class FAQ:
    question: str
    answer: str           # plain text; rendered as a paragraph
    source_label: str     # e.g. "Andrej Karpathy, The New Stack"
    source_url: str       # canonical URL backing the claim
    source_key: str       # stable click-track key


# Question wording chosen to match real search queries (covered terms:
# "what is vibe coding", "how to get better at vibe coding", "agentic coding",
# "AI pair programming"). Answers stay short and concrete.
FAQS: list[FAQ] = [
    FAQ(
        "What is agentic engineering?",
        "It's the practice of using AI coding agents to ship software while "
        "staying the engineer. You decide what gets built. You write the "
        "briefs. You read the diffs. The agent handles the typing and most "
        "of the rote work. The thinking part stays yours. Karpathy framed it "
        "as orchestrating agents and acting as oversight: you're not writing "
        "the code directly 99% of the time, but you're still responsible for it.",
        source_label="Andrej Karpathy, via The New Stack",
        source_url="https://thenewstack.io/vibe-coding-is-passe/",
        source_key="vibe:faq:karpathy-agentic-engineering",
    ),
    FAQ(
        "How is it different from AI pair programming?",
        "AI pair programming in its classic Copilot-autocomplete sense is "
        "real-time and granular: you're at the keyboard, the AI suggests "
        "the next line or block, you accept or reject. The human stays the "
        "primary author. Agentic engineering is delegation. You write a "
        "brief, the agent does the work end-to-end (often asynchronously), "
        "and you review the result. The agent's the author. You're the "
        "director who decided what gets built and verifies it did. Modern "
        "Copilot ships both modes now, so the line lives in how you use it.",
        source_label="GitHub Copilot documentation",
        source_url="https://docs.github.com/en/copilot/get-started/what-is-github-copilot",
        source_key="vibe:faq:github-copilot-docs",
    ),
    FAQ(
        "How is it different from vibe coding?",
        "Same tools, different relationship to the code. Vibe coding is "
        "Karpathy's casual mode: describe what you want, let the agent run, "
        "don't really read the diff. Fine for low-stakes work where being "
        "wrong is cheap. Agentic engineering is what you do when the code "
        "matters: when it handles secrets, money, or other people's "
        "decisions. Same tools. Different stakes.",
        source_label="Simon Willison",
        source_url="https://simonwillison.net/2025/Mar/19/vibe-coding/",
        source_key="vibe:faq:simonw-vibe-coding-distinction",
    ),
    FAQ(
        "Where do I start?",
        "Pick one tool and stay with it for a week. The picks worth "
        "starting with: Claude Code or Codex if you live in the terminal, "
        "Cursor for a dedicated AI-native editor, or VS Code with the "
        "Claude Code, Codex, or GitHub Copilot extensions if you want to "
        "stay in your current setup. Pick a small task you already know "
        "how to do. Watch the agent attempt it. Read every line of the "
        "diff. Anthropic's recommendation: explore first, then plan, then "
        "code. Start with scope you can verify quickly before expanding "
        "what you delegate.",
        source_label="Anthropic: Claude Code best practices",
        source_url="https://code.claude.com/docs/en/best-practices",
        source_key="vibe:faq:anthropic-best-practices-start",
    ),
    FAQ(
        "How do I get better at it?",
        "Three habits matter most. Write briefs the agent can actually use: "
        "what you want, what context it can use, what done looks like. Keep "
        "tasks small and feedback loops fast (Anthropic's own team puts "
        "2–3× quality on tight feedback loops alone). Read every diff. The "
        "agent's worst failures don't look like errors. They look like "
        "plausible code that does the wrong thing.",
        source_label="Anthropic: Claude Code best practices",
        source_url="https://code.claude.com/docs/en/best-practices",
        source_key="vibe:faq:anthropic-best-practices-improve",
    ),
    FAQ(
        "What kinds of work should I not hand to an agent?",
        "Anything where being subtly wrong is expensive. Security-critical "
        "changes. Migrations. Cross-cutting refactors in code you don't "
        "fully understand. Work where \"looks right\" isn't sufficient. "
        "Scoped tasks inside well-tested boundaries are where agents pay "
        "off the most. Anthropic's 2026 trends report saw the same pattern "
        "across teams: engineers delegate work that's easily verifiable or "
        "low-stakes and keep the conceptually difficult work for themselves.",
        source_label="Birgitta Böckeler: To vibe or not to vibe",
        source_url="https://martinfowler.com/articles/exploring-gen-ai/to-vibe-or-not-vibe.html",
        source_key="vibe:faq:bockeler-to-vibe-or-not",
    ),
    FAQ(
        "Should I let an agent merge its own PRs?",
        "Almost never on production. The point of agentic engineering is "
        "that someone (you) understood what was supposed to happen. If you "
        "didn't read the PR, no one did. Auto-merge is fine on scratch "
        "repos, generated config, throwaway prototypes. The line is whether "
        "anyone else's data, time, or trust depends on the code. GitHub's "
        "own guidance for the Coding Agent era keeps a human review gate "
        "on anything touching production.",
        source_label="GitHub Engineering",
        source_url="https://github.blog/ai-and-ml/generative-ai/agent-pull-requests-are-everywhere-heres-how-to-review-them/",
        source_key="vibe:faq:github-agent-prs",
    ),
    FAQ(
        "Which tool should I pick?",
        "It's converging within categories. Among terminal agents (Claude "
        "Code, Codex, OpenCode), among editor agents (Cursor, Windsurf), "
        "and among async cloud agents (Devin, Jules), the tools are doing "
        "very similar things. Pick whatever fits your workflow. Try a "
        "second one a week later. The bigger lever is on the human side: "
        "how well you brief, how carefully you review, how small you keep "
        "the tasks.",
        source_label="Armin Ronacher: A Year of Vibes",
        source_url="https://lucumr.pocoo.org/2025/12/22/a-year-of-vibes/",
        source_key="vibe:faq:ronacher-year-of-vibes",
    ),
]
