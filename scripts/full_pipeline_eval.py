"""Full four-stage pipeline eval — reframer → researcher → gap-filler → author.

Runs the EXACT production model configuration end-to-end against a set of
prompts and emits a self-contained HTML + JSON report. One linear run per
prompt (not the matrix harness in ``pipeline_eval.py``).

Each run captures every stage's structured output, the tool calls the
researcher/gap-filler made, per-stage token + cost numbers, and elapsed
wall-time. The final answer is rendered as markdown so links and rich
blocks are inspectable.

Usage (inside the compose web container so `.env` + DB are wired up):

    docker compose exec web uv run python scripts/full_pipeline_eval.py \\
        --prompts scripts/eval_prompts.txt \\
        --output reports/full-eval-$(date +%Y%m%d-%H%M%S).html \\
        --json   reports/full-eval-$(date +%Y%m%d-%H%M%S).json

Prompts file follows the same format as ``pipeline_eval.py``: one prompt
per line, ``#`` lines are skipped, multi-line prompts can use ``---``
separators.

Prod model config (env-overridable, defaults match
``smarter_dev.web.resources_agent``):

    Reframer:   gemini-3-flash-preview · think=MEDIUM
    Researcher: gpt-5.4-nano           · think=medium
    Gap-filler: gemini-3-flash-preview · think=LOW
    Author:    gemini-3-flash-preview · think=LOW

The eval uses ``pydantic_ai.Agent`` directly (not ``skrift.Agent``) so it
runs standalone without the worker/notification runtime. Tool wiring and
allowlist enforcement mirror prod's ``search_resources`` / ``read_source``
/ ``web_search`` / ``read_url`` semantics.
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

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.pipeline_eval import (  # noqa: E402
    _live_jina_fetch,
    _live_jina_search,
    _model_settings,
    build_model,
    cost_for,
    parse_prompts,
    read_curated,
    search_curated,
)
from smarter_dev.web.resources_agent import (  # noqa: E402
    AUTHOR_MODEL,
    GAP_FILLER_MODEL,
    REFRAMER_MODEL,
    RESEARCHER_MODEL,
    GapFillerOutput,
    ReframerOutput,
    ResearchOutput,
    _build_author_payload,
    _build_author_user_turn,
    _build_reframer_user_turn,
    _build_researcher_user_turn,
    _format_gap_payload,
    _GAP_FILLER_PROMPT,
    _REFRAMER_PROMPT,
    _RESEARCHER_PROMPT,
    _SYSTEM_PROMPT as _AUTHOR_PROMPT,
)

logger = logging.getLogger(__name__)


# Prod model config (matches resources_agent.py defaults; env overrides
# kept consistent so this script can A/B test alternate models too).
REFRAMER_THINK = os.getenv("EVAL_REFRAMER_THINK", "medium")
RESEARCHER_THINK = os.getenv("EVAL_RESEARCHER_THINK", "medium")
GAP_FILLER_THINK = os.getenv("EVAL_GAP_FILLER_THINK", "low")
AUTHOR_THINK = os.getenv("EVAL_AUTHOR_THINK", "low")


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ToolCall:
    tool: str
    args: dict
    result: str
    elapsed_s: float


@dataclasses.dataclass
class StageResult:
    stage: str  # reframer | researcher | gap_filler | author
    model: str
    thinking: str
    output: object  # ReframerOutput | ResearchOutput | GapFillerOutput | str
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    elapsed_s: float
    error: str = ""
    # Researcher-only: per-run URL allowlist that gated read_source.
    seen_source_urls: set[str] = dataclasses.field(default_factory=set)
    # Gap-filler-only: per-run web_search URL allowlist that gated read_url.
    seen_web_urls: set[str] = dataclasses.field(default_factory=set)

    @property
    def cost_usd(self) -> float:
        return cost_for(self.model, self.input_tokens, self.output_tokens)


@dataclasses.dataclass
class PromptRun:
    prompt: str
    reframer: StageResult | None = None
    researcher: StageResult | None = None
    gap_filler: StageResult | None = None
    author: StageResult | None = None

    def stages(self) -> list[StageResult]:
        return [s for s in (
            self.reframer, self.researcher, self.gap_filler, self.author
        ) if s is not None]

    def total_cost(self) -> float:
        return sum(s.cost_usd for s in self.stages())

    def total_tokens(self) -> tuple[int, int]:
        return (
            sum(s.input_tokens for s in self.stages()),
            sum(s.output_tokens for s in self.stages()),
        )


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


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------


async def _run_reframer(prompt: str) -> StageResult:
    agent = Agent(
        build_model(REFRAMER_MODEL),
        system_prompt=_REFRAMER_PROMPT,
        output_type=ReframerOutput,
        model_settings=_model_settings(REFRAMER_MODEL, REFRAMER_THINK),
    )
    t0 = time.monotonic()
    try:
        result = await agent.run(_build_reframer_user_turn(prompt))
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage="reframer", model=REFRAMER_MODEL, thinking=REFRAMER_THINK,
            output=None, tool_calls=[],
            input_tokens=0, output_tokens=0,
            elapsed_s=time.monotonic() - t0, error=str(exc),
        )
    elapsed = time.monotonic() - t0
    output = getattr(result, "output", None)
    inp, otok = _coerce_usage(getattr(result, "usage", None))
    if not isinstance(output, ReframerOutput):
        output = ReframerOutput(
            restated_question="", reframing_instructions="",
            corpus_topics=[], web_search_topics=[],
        )
    return StageResult(
        stage="reframer", model=REFRAMER_MODEL, thinking=REFRAMER_THINK,
        output=output, tool_calls=[],
        input_tokens=inp, output_tokens=otok, elapsed_s=elapsed,
    )


async def _run_researcher(prompt: str, reframe: ReframerOutput) -> StageResult:
    """Run the researcher against the curated catalog.

    Tools mirror prod: ``search_resources`` returns ranked catalog hits
    with real URLs (no opaque IDs); ``read_source`` enforces a per-run
    allowlist that every URL passed in must have come from a prior
    ``search_resources`` hit. Matches resources_agent.py's tool surface.
    """
    agent = Agent(
        build_model(RESEARCHER_MODEL),
        system_prompt=_RESEARCHER_PROMPT,
        output_type=ResearchOutput,
        model_settings=_model_settings(RESEARCHER_MODEL, RESEARCHER_THINK),
    )
    tool_calls: list[ToolCall] = []
    seen_urls: set[str] = set()

    @agent.tool_plain
    async def search_resources(query: str, limit: int = 8) -> list[dict]:
        """Search the curated resource catalog. Returns ranked hits
        with title, url, byline, blurb, learning_type, directory,
        category. Pass the exact URL field to `read_source` to fetch
        the full body."""
        t0 = time.monotonic()
        hits = await search_curated(query, limit=limit)
        for h in hits:
            if h.get("url"):
                seen_urls.add(h["url"])
        tool_calls.append(ToolCall(
            tool="search_resources",
            args={"query": query, "limit": limit},
            result=f"{len(hits)} hit" + ("s" if len(hits) != 1 else ""),
            elapsed_s=time.monotonic() - t0,
        ))
        return hits

    @agent.tool_plain
    async def read_source(url: str) -> str:
        """Read the full body of a curated source. URL must have come
        from a `search_resources` hit in this run."""
        t0 = time.monotonic()
        if url not in seen_urls:
            tool_calls.append(ToolCall(
                tool="read_source", args={"url": url},
                result="error · url not in search results",
                elapsed_s=time.monotonic() - t0,
            ))
            return (
                "[error] URL not in search_resources results from this run. "
                "Run `search_resources` first and use a URL from the results."
            )
        raw = await read_curated(url)
        if "error" in raw:
            summary = f"error · {raw['error'][:60]}"
            body = f"[error] {raw['error']}"
        else:
            body = (
                f"{raw.get('title', '')}\n\n{raw.get('content', '')}".strip()
            )
            summary = f"ok · {len(body)} chars"
        tool_calls.append(ToolCall(
            tool="read_source", args={"url": url},
            result=summary, elapsed_s=time.monotonic() - t0,
        ))
        return body

    user_turn = _build_researcher_user_turn(
        prompt, reframe.reframing_instructions, list(reframe.corpus_topics)
    )

    t0 = time.monotonic()
    try:
        result = await agent.run(user_turn)
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage="researcher", model=RESEARCHER_MODEL,
            thinking=RESEARCHER_THINK, output=None,
            tool_calls=tool_calls,
            input_tokens=0, output_tokens=0,
            elapsed_s=time.monotonic() - t0, error=str(exc),
            seen_source_urls=seen_urls,
        )
    elapsed = time.monotonic() - t0
    research = getattr(result, "output", None)
    if not isinstance(research, ResearchOutput):
        research = ResearchOutput()
    inp, otok = _coerce_usage(getattr(result, "usage", None))
    return StageResult(
        stage="researcher", model=RESEARCHER_MODEL,
        thinking=RESEARCHER_THINK, output=research,
        tool_calls=tool_calls,
        input_tokens=inp, output_tokens=otok, elapsed_s=elapsed,
        seen_source_urls=seen_urls,
    )


async def _run_gap_filler(
    prompt: str,
    research_gaps: list[dict],
    extra_web_topics: list[str],
) -> StageResult | None:
    """Run the gap-filler against the open web. Returns None when there
    are no gaps AND no reframer-requested topics — matches prod skip
    logic."""
    if not research_gaps and not extra_web_topics:
        return None

    agent = Agent(
        build_model(GAP_FILLER_MODEL),
        system_prompt=_GAP_FILLER_PROMPT,
        output_type=GapFillerOutput,
        model_settings=_model_settings(GAP_FILLER_MODEL, GAP_FILLER_THINK),
    )
    tool_calls: list[ToolCall] = []
    seen_urls: set[str] = set()
    url_to_body: dict[str, str] = {}

    @agent.tool_plain
    async def web_search(query: str) -> list[dict]:
        """Search the open web for authoritative/primary sources."""
        t0 = time.monotonic()
        hits = await _live_jina_search(query, num_results=5)
        for h in hits:
            if h.get("url"):
                seen_urls.add(h["url"])
        tool_calls.append(ToolCall(
            tool="web_search", args={"query": query},
            result=f"{len(hits)} hit" + ("s" if len(hits) != 1 else ""),
            elapsed_s=time.monotonic() - t0,
        ))
        return hits

    @agent.tool_plain
    async def read_url(url: str) -> dict:
        """Read the full body of a URL. URL must have come from a prior
        `web_search` hit in this run."""
        t0 = time.monotonic()
        if url not in seen_urls:
            tool_calls.append(ToolCall(
                tool="read_url", args={"url": url},
                result="error · url not in search results",
                elapsed_s=time.monotonic() - t0,
            ))
            return {
                "error": (
                    f"unknown url: {url} — only URLs returned by "
                    f"`web_search` in this run are valid."
                ),
            }
        body = url_to_body.get(url)
        if body is None:
            body = await _live_jina_fetch(url, max_chars=10_000)
            if body:
                url_to_body[url] = body
        if not body:
            tool_calls.append(ToolCall(
                tool="read_url", args={"url": url},
                result="error · read failed",
                elapsed_s=time.monotonic() - t0,
            ))
            return {"error": f"jina read failed for {url}"}
        tool_calls.append(ToolCall(
            tool="read_url", args={"url": url},
            result=f"ok · {len(body)} chars",
            elapsed_s=time.monotonic() - t0,
        ))
        return {"url": url, "content": body}

    # Re-use the prod gap-payload builder so the user turn the gap-filler
    # sees in the eval matches the user turn it sees in prod, character
    # for character. `Gap` objects are reconstructed from the
    # researcher's `ResearchOutput.gaps` (already typed) — we just need
    # to pass them in.
    from smarter_dev.web.resources_agent import Gap as _Gap
    gap_objs = [_Gap(**g) for g in research_gaps]
    user_turn = _format_gap_payload(prompt, gap_objs, list(extra_web_topics))

    t0 = time.monotonic()
    try:
        result = await agent.run(user_turn)
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage="gap_filler", model=GAP_FILLER_MODEL,
            thinking=GAP_FILLER_THINK, output=None,
            tool_calls=tool_calls,
            input_tokens=0, output_tokens=0,
            elapsed_s=time.monotonic() - t0, error=str(exc),
            seen_web_urls=seen_urls,
        )
    elapsed = time.monotonic() - t0
    output = getattr(result, "output", None)
    if not isinstance(output, GapFillerOutput):
        output = GapFillerOutput()
    inp, otok = _coerce_usage(getattr(result, "usage", None))
    return StageResult(
        stage="gap_filler", model=GAP_FILLER_MODEL,
        thinking=GAP_FILLER_THINK, output=output,
        tool_calls=tool_calls,
        input_tokens=inp, output_tokens=otok, elapsed_s=elapsed,
        seen_web_urls=seen_urls,
    )


async def _run_author(
    prompt: str,
    reframe: ReframerOutput,
    research: ResearchOutput,
    web_citations: list,
) -> StageResult:
    agent = Agent(
        build_model(AUTHOR_MODEL),
        system_prompt=_AUTHOR_PROMPT,
        model_settings=_model_settings(AUTHOR_MODEL, AUTHOR_THINK),
    )
    payload = _build_author_payload(
        research, web_citations, reframe.reframing_instructions
    )
    user_turn = _build_author_user_turn(prompt, payload)
    t0 = time.monotonic()
    try:
        result = await agent.run(user_turn)
    except Exception as exc:  # noqa: BLE001
        return StageResult(
            stage="author", model=AUTHOR_MODEL, thinking=AUTHOR_THINK,
            output="", tool_calls=[],
            input_tokens=0, output_tokens=0,
            elapsed_s=time.monotonic() - t0, error=str(exc),
        )
    elapsed = time.monotonic() - t0
    output = str(getattr(result, "output", None) or "")
    inp, otok = _coerce_usage(getattr(result, "usage", None))
    return StageResult(
        stage="author", model=AUTHOR_MODEL, thinking=AUTHOR_THINK,
        output=output, tool_calls=[],
        input_tokens=inp, output_tokens=otok, elapsed_s=elapsed,
    )


async def run_pipeline(prompt: str, *, reframer_only: bool = False) -> PromptRun:
    print(f"\n=== prompt: {prompt[:80]}…", file=sys.stderr)
    run = PromptRun(prompt=prompt)

    print("  reframer …", file=sys.stderr, end="", flush=True)
    run.reframer = await _run_reframer(prompt)
    _log_stage(run.reframer)
    if run.reframer.error or reframer_only:
        return run
    reframe: ReframerOutput = run.reframer.output  # type: ignore[assignment]

    print("  researcher …", file=sys.stderr, end="", flush=True)
    run.researcher = await _run_researcher(prompt, reframe)
    _log_stage(run.researcher)
    if run.researcher.error or run.researcher.output is None:
        return run
    research: ResearchOutput = run.researcher.output  # type: ignore[assignment]

    gaps_dump = [g.model_dump() for g in research.gaps]
    print("  gap_filler …", file=sys.stderr, end="", flush=True)
    run.gap_filler = await _run_gap_filler(
        prompt, gaps_dump, list(reframe.web_search_topics)
    )
    if run.gap_filler is None:
        print(" skipped (no gaps, no extra topics)", file=sys.stderr)
        web_citations: list = []
    else:
        _log_stage(run.gap_filler)
        web_citations = (
            list(run.gap_filler.output.citations)
            if isinstance(run.gap_filler.output, GapFillerOutput) else []
        )

    print("  author …", file=sys.stderr, end="", flush=True)
    run.author = await _run_author(prompt, reframe, research, web_citations)
    _log_stage(run.author)
    return run


def _log_stage(s: StageResult) -> None:
    if s.error:
        print(f" FAIL: {s.error}", file=sys.stderr)
        return
    extras = []
    if s.tool_calls:
        extras.append(f"{len(s.tool_calls)} tools")
    if isinstance(s.output, ResearchOutput):
        extras.append(
            f"{len(s.output.excerpts)} excerpts/"
            f"{len(s.output.further_reading)} fr/"
            f"{len(s.output.gaps)} gaps"
        )
    if isinstance(s.output, GapFillerOutput):
        extras.append(f"{len(s.output.citations)} citations")
    if isinstance(s.output, ReframerOutput):
        extras.append(
            f"{len(s.output.corpus_topics)} corpus/"
            f"{len(s.output.web_search_topics)} web topics"
        )
    detail = ", ".join(extras)
    print(
        f" ok ({s.elapsed_s:.1f}s, {detail}, "
        f"{s.input_tokens}+{s.output_tokens} tok, "
        f"${s.cost_usd:.4f})",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def _html_escape(s: str) -> str:
    import html as _html

    return _html.escape(s or "")


def _md_to_html(md: str) -> str:
    try:
        from skrift.lib.markdown import render_markdown

        return render_markdown(md or "")
    except Exception:  # noqa: BLE001
        return f"<pre>{_html_escape(md)}</pre>"


def _tool_calls_html(calls: list[ToolCall]) -> str:
    if not calls:
        return ""
    rows = []
    for c in calls:
        if c.tool in ("search_resources", "web_search"):
            args = f"<code>{_html_escape(c.args.get('query', ''))}</code>"
        elif c.tool in ("read_source", "read_url"):
            url = c.args.get("url", "")
            args = (
                f"<a href='{_html_escape(url)}' target='_blank' "
                f"rel='noopener'><code>{_html_escape(url)}</code></a>"
            )
        else:
            args = f"<code>{_html_escape(str(c.args))}</code>"
        rows.append(
            "<tr>"
            f"<td><code>{_html_escape(c.tool)}</code></td>"
            f"<td>{args}</td>"
            f"<td>{_html_escape(c.result)}</td>"
            f"<td class='num'>{c.elapsed_s:.2f}s</td>"
            "</tr>"
        )
    return (
        "<h4 class='res-section'>Tool calls "
        f"<span class='res-section-count'>({len(calls)})</span></h4>"
        "<table class='tool-table'>"
        "<thead><tr><th>Tool</th><th>Args</th><th>Result</th>"
        "<th class='num'>Δ</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _reframer_body_html(s: StageResult) -> str:
    if s.error or not isinstance(s.output, ReframerOutput):
        return f"<pre class='empty'>[error] {_html_escape(s.error)}</pre>"
    o = s.output
    corpus = "".join(
        f"<li><code>{_html_escape(t)}</code></li>" for t in o.corpus_topics
    )
    web = "".join(
        f"<li><code>{_html_escape(t)}</code></li>" for t in o.web_search_topics
    ) or "<li class='empty'>(none)</li>"
    return (
        "<h4 class='res-section'>Restated question (user-visible)</h4>"
        f"<blockquote>{_html_escape(o.restated_question)}</blockquote>"
        "<h4 class='res-section'>Reframing instructions (author-facing)</h4>"
        f"<div class='md'>{_md_to_html(o.reframing_instructions)}</div>"
        "<h4 class='res-section'>Corpus topics (→ researcher)</h4>"
        f"<ul class='topic-list'>{corpus}</ul>"
        "<h4 class='res-section'>Web search topics (→ gap-filler)</h4>"
        f"<ul class='topic-list'>{web}</ul>"
    )


def _researcher_body_html(s: StageResult) -> str:
    parts: list[str] = [_tool_calls_html(s.tool_calls)]
    if s.error or not isinstance(s.output, ResearchOutput):
        parts.append(
            f"<pre class='empty'>[error] {_html_escape(s.error)}</pre>"
        )
        return "".join(parts)
    r = s.output
    if r.excerpts:
        parts.append("<h4 class='res-section'>Excerpts</h4>"
                     "<div class='excerpt-list'>")
        for ex in r.excerpts:
            parts.append(
                "<div class='excerpt'>"
                f"<div class='excerpt-point'>{_html_escape(ex.purpose)}</div>"
                f"<div class='excerpt-text'>{_html_escape(ex.excerpt)}</div>"
                f"<div class='excerpt-cite'>— "
                f"<a href='{_html_escape(ex.source_url)}' target='_blank' "
                f"rel='noopener'><code>{_html_escape(ex.source_url)}</code></a>"
                "</div></div>"
            )
        parts.append("</div>")
    if r.further_reading:
        parts.append("<h4 class='res-section'>Further reading</h4>"
                     "<ul class='further-list'>")
        for fr in r.further_reading:
            parts.append(
                "<li>"
                f"<a href='{_html_escape(fr.source_url)}' target='_blank' "
                f"rel='noopener'><code>{_html_escape(fr.source_url)}</code></a>"
                f" — <span class='further-blurb'>{_html_escape(fr.blurb)}</span>"
                "</li>"
            )
        parts.append("</ul>")
    if r.gaps:
        parts.append("<h4 class='res-section'>Gaps</h4><ul class='gap-list'>")
        for g in r.gaps:
            queries = ", ".join(
                f"<code>{_html_escape(q)}</code>" for q in g.tried_queries
            )
            parts.append(
                "<li>"
                f"<div class='gap-concept'>{_html_escape(g.concept)}</div>"
                f"<div class='gap-needed'>{_html_escape(g.needed)}</div>"
                f"<div class='gap-queries'>tried: {queries}</div>"
                "</li>"
            )
        parts.append("</ul>")
    return "".join(parts)


def _gap_filler_body_html(s: StageResult) -> str:
    parts: list[str] = [_tool_calls_html(s.tool_calls)]
    if s.error or not isinstance(s.output, GapFillerOutput):
        parts.append(
            f"<pre class='empty'>[error] {_html_escape(s.error)}</pre>"
        )
        return "".join(parts)
    o = s.output
    if not o.citations:
        parts.append("<p class='empty'>(no citations)</p>")
        return "".join(parts)
    parts.append("<h4 class='res-section'>Citations</h4>"
                 "<div class='excerpt-list'>")
    for c in o.citations:
        valid = c.source_url in s.seen_web_urls
        link = (
            f"<a href='{_html_escape(c.source_url)}' target='_blank' "
            f"rel='noopener'>{_html_escape(c.source_title or c.source_url)}</a>"
        )
        if not valid:
            link += (
                " <code class='excerpt-path excerpt-path-unresolved'>"
                "(unresolved url)</code>"
            )
        parts.append(
            "<div class='excerpt'>"
            f"<div class='excerpt-point'>{_html_escape(c.gap_concept)}</div>"
            f"<div class='excerpt-text'>{_html_escape(c.excerpt)}</div>"
            f"<div class='gap-needed'>{_html_escape(c.rationale)}</div>"
            f"<div class='excerpt-cite'>— {link}</div>"
            "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _author_body_html(s: StageResult) -> str:
    if s.error:
        return f"<pre class='empty'>[error] {_html_escape(s.error)}</pre>"
    return f"<div class='md'>{_md_to_html(str(s.output))}</div>"


def _fmt_stage_meta(s: StageResult) -> str:
    return (
        f"{s.input_tokens:,} in / {s.output_tokens:,} out tok · "
        f"${s.cost_usd:.4f} · {s.elapsed_s:.1f}s"
    )


_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Full pipeline eval — {timestamp}</title>
<style>
  :root {{
    --bg: #0e1115; --fg: #d8e0ea; --muted: #8d97a6;
    --cyan: #4fb8ff; --green: #6ce6a0; --amber: #ffba6a;
    --border: rgba(255,255,255,.08); --card: #161a21; --code: #1e242d;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
         font: 15px/1.55 -apple-system, BlinkMacSystemFont, sans-serif; }}
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
                padding: .25rem 0 .25rem .8rem; color: var(--muted);
                font-style: italic; }}
  a {{ color: var(--cyan); }}
  .prompt-card {{ background: var(--card); border: 1px solid var(--border);
                  border-radius: 6px; padding: 1rem 1.25rem;
                  margin-bottom: 1rem; }}
  details {{ border: 1px solid var(--border); border-radius: 6px;
             margin: .35rem 0; background: var(--card); }}
  details > summary {{ cursor: pointer; padding: .55rem .8rem;
                       font: 600 .82rem/1 -apple-system, sans-serif;
                       list-style: none; display: flex; gap: .5rem;
                       align-items: center; flex-wrap: wrap; }}
  details > summary::-webkit-details-marker {{ display: none; }}
  details > summary::before {{ content: "▸"; color: var(--muted);
                               transition: transform .15s; font-size: .75rem; }}
  details[open] > summary::before {{ transform: rotate(90deg);
                                     display: inline-block; }}
  .pill {{ display: inline-block; padding: .12rem .45rem;
           font: 600 .7rem/1.4 ui-monospace, "SF Mono", monospace;
           border-radius: 3px; background: rgba(79,184,255,.15);
           color: var(--cyan); }}
  .pill.reframer {{ background: rgba(255,186,106,.15); color: var(--amber); }}
  .pill.researcher {{ background: rgba(79,184,255,.15); color: var(--cyan); }}
  .pill.gap_filler {{ background: rgba(108,230,160,.15); color: var(--green); }}
  .pill.author    {{ background: rgba(216,184,90,.18); color: #e8c971; }}
  .pill.cost {{ background: transparent; color: var(--muted);
                font-weight: 400; }}
  details > .body {{ padding: 0 .8rem .9rem;
                     border-top: 1px solid var(--border); }}
  .md p:first-child {{ margin-top: .5rem; }}
  .md p:last-child {{ margin-bottom: 0; }}
  .md ul, .md ol {{ margin: .35rem 0 .6rem; padding-left: 1.4rem; }}
  .md li {{ margin: .15rem 0; }}
  .empty {{ color: var(--muted); font-style: italic; }}
  .res-section {{ font-size: .68rem; letter-spacing: .14em;
                  text-transform: uppercase; color: var(--muted);
                  margin: .85rem 0 .4rem; font-weight: 600; }}
  .excerpt-list {{ display: flex; flex-direction: column; gap: .55rem; }}
  .excerpt {{ padding: .55rem .75rem;
              background: rgba(79,184,255,.04);
              border-left: 2px solid rgba(79,184,255,.5);
              border-radius: 0 4px 4px 0; }}
  .excerpt-point {{ font-weight: 600; color: var(--cyan);
                    font-size: .82rem; margin-bottom: .25rem; }}
  .excerpt-text {{ font-size: .88rem; line-height: 1.5; margin: 0 0 .2rem; }}
  .excerpt-cite {{ font-size: .78rem; color: var(--muted); }}
  .excerpt-path-unresolved {{ color: #c54848; opacity: 1; }}
  ul.gap-list {{ list-style: none; padding: 0; margin: 0;
                 display: flex; flex-direction: column; gap: .35rem; }}
  ul.gap-list li {{ font-size: .82rem; line-height: 1.45;
                    border-left: 2px solid #c5a448; padding: .15rem .5rem;
                    background: rgba(197, 164, 72, 0.06); }}
  .gap-concept {{ font-weight: 600; color: #d8b85a; }}
  .gap-needed {{ color: var(--text); margin: .1rem 0; }}
  .gap-queries {{ color: var(--muted); font-size: .76rem; }}
  ul.further-list, ul.topic-list {{ list-style: none; padding: 0; margin: 0;
                                    display: flex; flex-direction: column;
                                    gap: .25rem; }}
  ul.further-list li, ul.topic-list li {{ font-size: .85rem; line-height: 1.45; }}
  .further-blurb {{ color: var(--muted); }}
  .table tool-table {{ font-size: .8rem; margin: 0 0 .25rem; }}
  table.tool-table th {{ font-weight: 600; }}
  table.tool-table td {{ vertical-align: top; padding: .3rem .55rem; }}
  .res-section-count {{ color: var(--muted); font-weight: 400;
                        text-transform: none; letter-spacing: 0;
                        font-size: .68rem; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Full pipeline eval — reframer + researcher + gap-filler + author</h1>
  <p class="sub">Generated {timestamp} · {n_prompts} prompt(s) · {n_runs}
    model calls · prod model config<br>
    Reframer <code>{reframer_model} · think={reframer_think}</code> →
    Researcher <code>{researcher_model} · think={researcher_think}</code> →
    Gap-filler <code>{gap_filler_model} · think={gap_filler_think}</code> →
    Author <code>{author_model} · think={author_think}</code></p>

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


def render_html(runs: list[PromptRun]) -> str:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    model_in: dict[str, int] = {}
    model_out: dict[str, int] = {}
    n_runs = 0
    for run in runs:
        for s in run.stages():
            model_in[s.model] = model_in.get(s.model, 0) + s.input_tokens
            model_out[s.model] = model_out.get(s.model, 0) + s.output_tokens
            n_runs += 1

    grand_cost = 0.0
    total_in = 0
    total_out = 0
    cost_rows = []
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

    blocks: list[str] = []
    for i, run in enumerate(runs, 1):
        stage_blocks: list[str] = []
        for s in run.stages():
            if s.stage == "reframer":
                body = _reframer_body_html(s)
                title = "Reframer"
            elif s.stage == "researcher":
                body = _researcher_body_html(s)
                title = "Researcher"
            elif s.stage == "gap_filler":
                body = _gap_filler_body_html(s)
                title = "Gap-filler"
            elif s.stage == "author":
                body = _author_body_html(s)
                title = "Author (final answer)"
            else:
                body = ""
                title = s.stage
            label = (
                f"{s.model}"
                + (f" · think={s.thinking}" if s.thinking else "")
            )
            stage_blocks.append(
                "<details open>"
                "<summary>"
                f"<span class='pill {s.stage}'>{_html_escape(title)}</span>"
                f"<span class='pill'>{_html_escape(label)}</span>"
                f"<span class='pill cost'>{_html_escape(_fmt_stage_meta(s))}</span>"
                "</summary>"
                f"<div class='body'>{body}</div>"
                "</details>"
            )

        per_in, per_out = run.total_tokens()
        blocks.append(
            "<div class='prompt-card'>"
            f"<h2>Prompt {i}</h2>"
            f"<blockquote>{_html_escape(run.prompt)}</blockquote>"
            f"<p class='sub'>${run.total_cost():.4f} · "
            f"{per_in:,} in / {per_out:,} out tok · "
            f"{len(run.stages())} stage(s)</p>"
            + "".join(stage_blocks)
            + "</div>"
        )

    return _HTML_TEMPLATE.format(
        timestamp=timestamp,
        n_prompts=len(runs),
        n_runs=n_runs,
        reframer_model=REFRAMER_MODEL, reframer_think=REFRAMER_THINK,
        researcher_model=RESEARCHER_MODEL, researcher_think=RESEARCHER_THINK,
        gap_filler_model=GAP_FILLER_MODEL, gap_filler_think=GAP_FILLER_THINK,
        author_model=AUTHOR_MODEL, author_think=AUTHOR_THINK,
        cost_rows="\n".join(cost_rows),
        total_in=total_in,
        total_out=total_out,
        grand_cost=grand_cost,
        prompt_blocks="\n".join(blocks),
    )


# ---------------------------------------------------------------------------
# JSON sidecar
# ---------------------------------------------------------------------------


def _stage_to_json(s: StageResult) -> dict:
    out: dict = {
        "stage": s.stage,
        "model": s.model,
        "thinking": s.thinking,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "elapsed_s": s.elapsed_s,
        "cost_usd": s.cost_usd,
        "error": s.error,
        "tool_calls": [dataclasses.asdict(t) for t in s.tool_calls],
    }
    if s.stage == "reframer" and isinstance(s.output, ReframerOutput):
        out["output"] = s.output.model_dump()
    elif s.stage == "researcher" and isinstance(s.output, ResearchOutput):
        out["output"] = s.output.model_dump()
        out["seen_source_urls"] = sorted(s.seen_source_urls)
    elif s.stage == "gap_filler" and isinstance(s.output, GapFillerOutput):
        out["output"] = s.output.model_dump()
        out["seen_web_urls"] = sorted(s.seen_web_urls)
    elif s.stage == "author":
        out["output"] = str(s.output)
    else:
        out["output"] = None
    return out


def runs_to_json(runs: list[PromptRun]) -> list[dict]:
    return [
        {
            "prompt": r.prompt,
            "total_cost_usd": r.total_cost(),
            "stages": [_stage_to_json(s) for s in r.stages()],
        }
        for r in runs
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", type=Path, required=True,
                    help="Path to a text file containing prompts.")
    ap.add_argument("--output", type=Path, required=True,
                    help="Path to write the HTML report.")
    ap.add_argument("--json", type=Path, default=None,
                    help="Optional path to also write a JSON sidecar.")
    ap.add_argument(
        "--reframer-only", action="store_true",
        help=(
            "Stop after the reframer stage. Report only carries the "
            "reframer's restated_question / reframing_instructions / "
            "corpus_topics / web_search_topics — no researcher, "
            "gap-filler, or author runs."
        ),
    )
    args = ap.parse_args()

    prompts = parse_prompts(args.prompts.read_text())
    if not prompts:
        print("No prompts found.", file=sys.stderr)
        return 1
    print(
        f"Running full pipeline against {len(prompts)} prompt(s)…",
        file=sys.stderr,
    )

    runs: list[PromptRun] = []
    for p in prompts:
        runs.append(await run_pipeline(p, reframer_only=args.reframer_only))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(runs))
    print(f"Wrote {args.output}", file=sys.stderr)

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(runs_to_json(runs), indent=2, ensure_ascii=False)
        )
        print(f"Wrote {args.json}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
