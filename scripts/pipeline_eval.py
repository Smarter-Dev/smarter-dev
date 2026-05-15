"""Local researcher × author pipeline evaluation harness.

Pipeline shape:

    prompt
       │
       ▼
    [Researcher] — searches `resource_sources` / `resource_tools` for
                   curated entries relevant to the prompt, outputs a
                   compact "research findings" block.
       │
       ▼
    [Author]     — receives prompt + research, produces the final
                   answer markdown.

For each prompt the harness runs every (researcher, author) pair and
captures:
- Each researcher's raw research output.
- Each author's final answer for each research source.
- Per-call usage (input/output tokens) and dollar cost using the
  pricing table baked in below.

Output: a single self-contained HTML report with every transcript
inline plus a per-prompt and grand-total cost table. Each prompt
section is a `<details>` so the matrix (3 researchers × 2 authors per
prompt) doesn't drown in scroll. Researcher + author bodies are
markdown-rendered so citations are clickable.

Usage:

    uv run python scripts/pipeline_eval.py \
        --prompts scripts/eval_prompts.txt \
        --output reports/eval_$(date +%Y%m%d_%H%M%S).html

Prompts file: one prompt per line. Lines starting with `#` are skipped,
blank lines separate runs. Multi-line prompts are also supported via a
`---` separator on its own line.

The DB connection comes from `smarter_dev.shared.config`'s effective
database URL — point it at the same PG you'd run the prod agent
against (the catalog is what we're testing against). Requires
`GEMINI_API_KEY` (Google) and `OPENAI_API_KEY` to be set.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.database import convert_postgres_url_for_asyncpg

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pricing (per 1M tokens). Update the table here as Google/OpenAI publish
# new tiers. Costs are USD.
# ---------------------------------------------------------------------------

PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-5.4-nano":           {"input": 0.20, "output": 1.25},
    # Google
    "gemini-3.1-flash-lite":  {"input": 0.25, "output": 1.50},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    # alias the prod resource-agent model uses
    "gemini-3-flash":         {"input": 0.50, "output": 3.00},
}


def cost_for(model_id: str, input_tokens: int, output_tokens: int) -> float:
    rates = PRICING.get(model_id)
    if rates is None:
        return 0.0
    return (
        (input_tokens / 1_000_000.0) * rates["input"]
        + (output_tokens / 1_000_000.0) * rates["output"]
    )


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------

def _google_model(model_id: str) -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


def _openai_model(model_id: str) -> OpenAIChatModel:
    api_key = os.getenv("OPENAI_API_KEY") or ""
    return OpenAIChatModel(model_id, provider=OpenAIProvider(api_key=api_key))


def build_model(model_id: str):
    if model_id.startswith("gpt-") or model_id.startswith("openai/"):
        return _openai_model(model_id.removeprefix("openai/"))
    return _google_model(model_id)


# ---------------------------------------------------------------------------
# Curated catalog search — same query the production resource agent uses,
# inlined so the harness doesn't import the production agent (which would
# pull in Skrift's worker runtime).
# ---------------------------------------------------------------------------

_SEARCH_SQL_SOURCES = text(
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

_SEARCH_SQL_TOOLS = text(
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


async def search_curated(query: str, limit: int = 8) -> list[dict]:
    if not query or not query.strip():
        return []
    limit = max(1, min(limit, 20))
    settings = get_settings()
    engine = create_async_engine(
        convert_postgres_url_for_asyncpg(settings.effective_database_url),
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET search_path TO skrift, public"))
            src_rows = (
                await conn.execute(
                    _SEARCH_SQL_SOURCES, {"q": query, "limit": limit}
                )
            ).mappings().all()
            tool_rows = (
                await conn.execute(
                    _SEARCH_SQL_TOOLS,
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
        hits.append({
            "kind": "source", "title": row["title"], "url": row["url"],
            "byline": row["byline"] or "", "blurb": row["blurb"] or "",
            "learning_type": row["learning_type"],
            "directory": row["directory"], "category": row["category"] or "",
        })
    for row in tool_rows:
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        hits.append({
            "kind": "tool", "title": row["title"], "url": row["url"],
            "byline": "", "blurb": row["blurb"] or "",
            "learning_type": "Tool",
            "directory": row["directory"], "category": row["category"] or "",
        })
    return hits[:limit]


# ---------------------------------------------------------------------------
# Researcher & author system prompts
# ---------------------------------------------------------------------------

_RESEARCHER_PROMPT = """\
You are the RESEARCHER step in a two-stage retrieval pipeline. The user
will give you a prompt. Your job is to find the relevant entries in the
Smarter Dev curated catalog by calling the `search_curated(query: str,
limit: int = 8)` tool. Run 1-3 well-chosen queries (different phrasings
of the same intent are fine).

You will not write the final answer — that's the author's job. Your
output is a compact "research findings" document the author can use.

Output ONLY a markdown bullet list of the entries you'd cite, in the
order you'd reach for them. Each bullet:

  - **<Title>** — <one-line blurb on what this resource covers and why
    it's relevant to the prompt>. URL: <url>

Aim for 3-8 bullets. Drop entries that aren't a genuine fit, even if
they came back from search. No preamble, no closing, no headings —
just the bullet list."""

_AUTHOR_PROMPT = """\
You are the AUTHOR step in a two-stage pipeline. A researcher has
already searched the Smarter Dev curated catalog and given you a
compact list of relevant entries. Your job is to write the final
answer for the user.

Style: friendly, direct, opinionated. Use contractions. Lead with the
answer in the first sentence. Use inline markdown links to cite the
researcher's entries — never invent URLs, only use URLs from the
research block. Keep the answer to 1-3 short paragraphs. If the
research is thin, say so plainly rather than padding.

You will see the user prompt and the research findings in the user
turn that follows. Output JUST the answer markdown — no preamble."""


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

RESEARCHER_MODELS = [
    "gpt-5.4-nano",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
]

AUTHOR_MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
]


@dataclasses.dataclass
class CallResult:
    model: str
    output: str
    input_tokens: int
    output_tokens: int
    elapsed_s: float

    @property
    def cost_usd(self) -> float:
        return cost_for(self.model, self.input_tokens, self.output_tokens)


def _coerce_usage(usage) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    if callable(usage):
        try:
            usage = usage()
        except Exception:  # noqa: BLE001
            return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


async def _run_researcher(model_id: str, prompt: str) -> CallResult:
    agent = Agent(build_model(model_id), system_prompt=_RESEARCHER_PROMPT)

    @agent.tool_plain
    async def search_curated_tool(query: str, limit: int = 8) -> list[dict]:
        """Search the curated Smarter Dev catalog for entries relevant
        to a query. Returns up to `limit` hits with title, URL, blurb,
        directory, learning_type."""
        return await search_curated(query, limit=limit)

    t0 = time.monotonic()
    result = await agent.run(prompt)
    elapsed = time.monotonic() - t0
    output = str(getattr(result, "output", None) or "")
    inp, out = _coerce_usage(getattr(result, "usage", None))
    return CallResult(
        model=model_id, output=output,
        input_tokens=inp, output_tokens=out, elapsed_s=elapsed,
    )


async def _run_author(
    model_id: str, prompt: str, research: str
) -> CallResult:
    agent = Agent(build_model(model_id), system_prompt=_AUTHOR_PROMPT)
    user_turn = (
        "# User prompt\n\n"
        f"{prompt}\n\n"
        "# Researcher findings\n\n"
        f"{research}"
    )
    t0 = time.monotonic()
    result = await agent.run(user_turn)
    elapsed = time.monotonic() - t0
    output = str(getattr(result, "output", None) or "")
    inp, out = _coerce_usage(getattr(result, "usage", None))
    return CallResult(
        model=model_id, output=output,
        input_tokens=inp, output_tokens=out, elapsed_s=elapsed,
    )


@dataclasses.dataclass
class PromptReport:
    prompt: str
    researchers: list[CallResult]
    authors: list[tuple[str, str, CallResult]]  # (researcher, author, result)

    def total_cost(self) -> float:
        return (
            sum(r.cost_usd for r in self.researchers)
            + sum(a.cost_usd for _, _, a in self.authors)
        )

    def total_tokens(self) -> tuple[int, int]:
        inp = sum(r.input_tokens for r in self.researchers) + sum(
            a.input_tokens for _, _, a in self.authors
        )
        out = sum(r.output_tokens for r in self.researchers) + sum(
            a.output_tokens for _, _, a in self.authors
        )
        return inp, out


async def run_one_prompt(prompt: str) -> PromptReport:
    print(f"\n=== prompt: {prompt[:80]}...", file=sys.stderr)
    researchers: list[CallResult] = []
    for model_id in RESEARCHER_MODELS:
        print(f"  researcher: {model_id} …", file=sys.stderr, end="")
        try:
            r = await _run_researcher(model_id, prompt)
            researchers.append(r)
            print(
                f" ok ({r.elapsed_s:.1f}s, "
                f"{r.input_tokens}+{r.output_tokens} tok, "
                f"${r.cost_usd:.4f})",
                file=sys.stderr,
            )
        except Exception as exc:  # noqa: BLE001
            print(f" FAIL: {exc}", file=sys.stderr)
            researchers.append(CallResult(
                model=model_id,
                output=f"[error] {exc}",
                input_tokens=0, output_tokens=0, elapsed_s=0.0,
            ))

    authors: list[tuple[str, str, CallResult]] = []
    for researcher in researchers:
        if researcher.output.startswith("[error]"):
            continue
        for model_id in AUTHOR_MODELS:
            print(
                f"  author: {model_id} on {researcher.model} …",
                file=sys.stderr, end="",
            )
            try:
                a = await _run_author(model_id, prompt, researcher.output)
                authors.append((researcher.model, model_id, a))
                print(
                    f" ok ({a.elapsed_s:.1f}s, "
                    f"{a.input_tokens}+{a.output_tokens} tok, "
                    f"${a.cost_usd:.4f})",
                    file=sys.stderr,
                )
            except Exception as exc:  # noqa: BLE001
                print(f" FAIL: {exc}", file=sys.stderr)
                authors.append((researcher.model, model_id, CallResult(
                    model=model_id, output=f"[error] {exc}",
                    input_tokens=0, output_tokens=0, elapsed_s=0.0,
                )))
    return PromptReport(prompt=prompt, researchers=researchers, authors=authors)


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def _fmt_meta(c: CallResult) -> str:
    return (
        f"{c.input_tokens:,} in / {c.output_tokens:,} out tok · "
        f"${c.cost_usd:.4f} · {c.elapsed_s:.1f}s"
    )


def _md_to_html(md: str) -> str:
    """Render markdown via Skrift's renderer (the same one /ai/answer
    uses) so links + lists + code fences come through."""
    try:
        from skrift.lib.markdown import render_markdown
        return render_markdown(md or "")
    except Exception:  # noqa: BLE001
        # Fallback: <pre> the raw text.
        import html as _html
        return f"<pre>{_html.escape(md or '')}</pre>"


_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pipeline eval — {timestamp}</title>
<style>
  :root {{
    --bg: #0e1115;
    --fg: #d8e0ea;
    --muted: #8d97a6;
    --cyan: #4fb8ff;
    --green: #6ce6a0;
    --amber: #ffba6a;
    --border: rgba(255,255,255,.08);
    --card: #161a21;
    --code: #1e242d;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px;
    background: var(--bg); color: var(--fg);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ margin: 0 0 .25rem; font-size: 1.5rem; }}
  h2 {{ font-size: 1.05rem; margin: 1.25rem 0 .5rem; }}
  h3 {{ font-size: .82rem; margin: 1rem 0 .35rem;
        text-transform: uppercase; letter-spacing: .12em;
        color: var(--muted); }}
  .sub {{ color: var(--muted); font-size: .85rem; margin-bottom: 1rem; }}
  table {{ width: 100%; border-collapse: collapse; margin: .5rem 0 1.25rem;
           font-size: .85rem; }}
  th, td {{ padding: .35rem .55rem; text-align: left;
            border-bottom: 1px solid var(--border); }}
  th {{ font-weight: 600; color: var(--muted); font-size: .72rem;
        text-transform: uppercase; letter-spacing: .12em; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr.total td {{ font-weight: 700; border-top: 2px solid var(--border); }}
  code {{ background: var(--code); padding: .1em .35em; border-radius: 3px;
          font-size: .9em; }}
  pre {{ background: var(--code); padding: .75rem .85rem; border-radius: 4px;
         overflow-x: auto; line-height: 1.45; font-size: .85rem; }}
  blockquote {{ border-left: 3px solid var(--cyan); margin: .5rem 0;
                padding: .25rem 0 .25rem .8rem;
                color: var(--muted); font-style: italic; }}
  a {{ color: var(--cyan); }}
  .prompt-card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 6px; padding: 1rem 1.25rem; margin-bottom: 1rem;
  }}
  details {{ border: 1px solid var(--border); border-radius: 6px;
             margin: .35rem 0; background: var(--card); }}
  details > summary {{
    cursor: pointer; padding: .55rem .8rem;
    font: 600 .82rem/1 -apple-system, sans-serif;
    list-style: none;
    display: flex; gap: .5rem; align-items: center; flex-wrap: wrap;
  }}
  details > summary::-webkit-details-marker {{ display: none; }}
  details > summary::before {{
    content: "▸"; color: var(--muted); transition: transform .15s;
    font-size: .75rem;
  }}
  details[open] > summary::before {{ transform: rotate(90deg);
    display: inline-block; }}
  .pill {{
    display: inline-block; padding: .12rem .45rem;
    font: 600 .7rem/1.4 ui-monospace, "SF Mono", monospace;
    border-radius: 3px;
    background: rgba(79,184,255,.15); color: var(--cyan);
  }}
  .pill.author {{ background: rgba(108,230,160,.15); color: var(--green); }}
  .pill.cost {{ background: transparent; color: var(--muted);
                font-weight: 400; }}
  details > .body {{ padding: 0 .8rem .9rem;
                     border-top: 1px solid var(--border); }}
  details > .body > .md {{ padding-top: .8rem; }}
  .md p:first-child {{ margin-top: 0; }}
  .md p:last-child  {{ margin-bottom: 0; }}
  .md ul, .md ol {{ margin: .35rem 0 .6rem; padding-left: 1.4rem; }}
  .md li {{ margin: .15rem 0; }}
  .empty {{ color: var(--muted); font-style: italic; }}
  .row-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; }}
  @media (max-width: 800px) {{ .row-grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Researcher × Author pipeline eval</h1>
  <p class="sub">Generated {timestamp} · {n_prompts} prompt(s) · {n_runs} model calls</p>

  <h2>Cost totals</h2>
  <table>
    <thead><tr>
      <th>Model</th><th class="num">Input tok</th>
      <th class="num">Output tok</th><th class="num">Cost (USD)</th>
    </tr></thead>
    <tbody>
{cost_rows}
      <tr class="total">
        <td>TOTAL</td>
        <td class="num">{total_in:,}</td>
        <td class="num">{total_out:,}</td>
        <td class="num">${grand_cost:.4f}</td>
      </tr>
    </tbody>
  </table>

{prompt_blocks}

</div>
</body>
</html>
"""


def _html_escape(s: str) -> str:
    import html as _html
    return _html.escape(s)


def render_html(reports: list[PromptReport]) -> str:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # Aggregate per-model totals.
    model_in: dict[str, int] = {}
    model_out: dict[str, int] = {}
    n_runs = 0
    for r in reports:
        for c in r.researchers:
            model_in[c.model] = model_in.get(c.model, 0) + c.input_tokens
            model_out[c.model] = model_out.get(c.model, 0) + c.output_tokens
            n_runs += 1
        for _, _, c in r.authors:
            model_in[c.model] = model_in.get(c.model, 0) + c.input_tokens
            model_out[c.model] = model_out.get(c.model, 0) + c.output_tokens
            n_runs += 1

    grand_cost = 0.0
    total_in = 0
    total_out = 0
    cost_rows: list[str] = []
    for m in sorted(set(list(model_in.keys()) + list(model_out.keys()))):
        ci = model_in.get(m, 0)
        co = model_out.get(m, 0)
        cost = cost_for(m, ci, co)
        grand_cost += cost
        total_in += ci
        total_out += co
        cost_rows.append(
            f"      <tr><td><code>{_html_escape(m)}</code></td>"
            f"<td class='num'>{ci:,}</td><td class='num'>{co:,}</td>"
            f"<td class='num'>${cost:.4f}</td></tr>"
        )

    # Per-prompt blocks.
    blocks: list[str] = []
    for i, r in enumerate(reports, 1):
        researcher_html: list[str] = []
        for c in r.researchers:
            body = (
                _md_to_html(c.output)
                if c.output and not c.output.startswith("[error]")
                else f"<pre class='empty'>{_html_escape(c.output) or '(empty)'}</pre>"
            )
            researcher_html.append(
                "<details open>"
                f"<summary><span class='pill'>{_html_escape(c.model)}</span> "
                f"<span class='pill cost'>{_html_escape(_fmt_meta(c))}</span>"
                "</summary>"
                f"<div class='body'><div class='md'>{body}</div></div>"
                "</details>"
            )

        author_html: list[str] = []
        for researcher, author_model, a in r.authors:
            body = (
                _md_to_html(a.output)
                if a.output and not a.output.startswith("[error]")
                else f"<pre class='empty'>{_html_escape(a.output) or '(empty)'}</pre>"
            )
            author_html.append(
                "<details open>"
                "<summary>"
                f"<span class='pill author'>{_html_escape(author_model)}</span>"
                " ← research from "
                f"<span class='pill'>{_html_escape(researcher)}</span>"
                f"<span class='pill cost'>{_html_escape(_fmt_meta(a))}</span>"
                "</summary>"
                f"<div class='body'><div class='md'>{body}</div></div>"
                "</details>"
            )

        per_in, per_out = r.total_tokens()
        blocks.append(
            "<div class='prompt-card'>"
            f"<h2>Prompt {i}</h2>"
            f"<blockquote>{_html_escape(r.prompt)}</blockquote>"
            f"<p class='sub'>${r.total_cost():.4f} · "
            f"{per_in:,} in / {per_out:,} out tok · "
            f"{len(r.researchers)} researchers × {len(AUTHOR_MODELS)} authors"
            "</p>"
            "<h3>Researcher outputs</h3>"
            + "".join(researcher_html)
            + "<h3>Author outputs (per researcher source)</h3>"
            + "".join(author_html)
            + "</div>"
        )

    return _HTML_TEMPLATE.format(
        timestamp=timestamp,
        n_prompts=len(reports),
        n_runs=n_runs,
        cost_rows="\n".join(cost_rows),
        total_in=total_in,
        total_out=total_out,
        grand_cost=grand_cost,
        prompt_blocks="\n".join(blocks),
    )


# ---------------------------------------------------------------------------
# Prompt file parsing
# ---------------------------------------------------------------------------

def parse_prompts(text_blob: str) -> list[str]:
    """One prompt per line, OR sections separated by lines of `---`.
    Lines starting with `#` are skipped. Adjacent blank lines collapse."""
    blocks: list[str] = []
    current: list[str] = []
    for raw in text_blob.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("#"):
            continue
        if line.strip() == "---":
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        if not line.strip() and not current:
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    # If no `---` separators were used, treat each non-empty line as its own
    # prompt — the common case.
    if len(blocks) == 1 and "\n" in blocks[0]:
        lines = [l.strip() for l in blocks[0].splitlines() if l.strip()]
        if len(lines) > 1 and all(len(l) < 240 for l in lines):
            return lines
    return [b for b in blocks if b]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--prompts", type=Path, required=True,
        help="Path to a text file containing prompts (see module docstring).",
    )
    ap.add_argument(
        "--output", type=Path, required=True,
        help="Path to write the Markdown report.",
    )
    ap.add_argument(
        "--json", type=Path, default=None,
        help="Optional path to also write a JSON sidecar with raw data.",
    )
    args = ap.parse_args()

    text_blob = args.prompts.read_text()
    prompts = parse_prompts(text_blob)
    if not prompts:
        print("No prompts found.", file=sys.stderr)
        return 1
    print(f"Running {len(prompts)} prompt(s)…", file=sys.stderr)

    reports: list[PromptReport] = []
    for prompt in prompts:
        reports.append(await run_one_prompt(prompt))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(reports))
    print(f"Wrote {args.output}", file=sys.stderr)

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = []
        for r in reports:
            payload.append({
                "prompt": r.prompt,
                "researchers": [dataclasses.asdict(c) for c in r.researchers],
                "authors": [
                    {"researcher": rh, "author": ah,
                     **dataclasses.asdict(a)}
                    for rh, ah, a in r.authors
                ],
                "total_cost_usd": r.total_cost(),
            })
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"Wrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
