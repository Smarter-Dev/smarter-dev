#!/usr/bin/env python
"""Search-query eval: ask a model for web search queries for each request.

Runs every request in requests.yaml through a Pydantic AI agent that returns
exactly five web search queries, and writes the request next to the queries
for human review (no judge model).

Usage:
    uv run python scripts/search_query_eval/run.py
    uv run python scripts/search_query_eval/run.py --model openai/gpt-6-luna --only vague-slow-website
    uv run python scripts/search_query_eval/run.py --reasoning low

Reads LITE_LLM_API_KEY and LITE_LLM_PROXY_HOST from .env. Writes
scripts/search_query_eval/reports/<model>-<reasoning>-<prompt>-<timestamp>.{json,md}, each with
the model settings, the system prompt and the cost at list price.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai import ModelRetry
from pydantic_ai import NativeOutput
from pydantic_ai import RunContext
from pydantic_ai import ToolOutput
from genai_prices import calc_price
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

HERE = Path(__file__).parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
import eval_prices  # noqa: E402  — registers prices for models newer than the snapshot

DEFAULT_MODEL = "openai/gpt-6-luna"
# gpt-6-luna's own default is medium; always send the level so reports never
# depend on a provider default changing underneath them.
DEFAULT_REASONING = "medium"
REASONING_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh"]
# tool: the queries come back as a function-call argument (Pydantic AI's default).
# native: the provider's JSON-schema structured output.
OUTPUT_MODES = {"tool": ToolOutput, "native": NativeOutput}
DEFAULT_OUTPUT_MODE = "tool"

DEFAULT_PROMPT = "v4"
# Proxy model name -> (genai-prices provider, model ref) for models whose proxy
# name doesn't match the price table. openai/* names resolve on their own.
PRICE_REFS = {"glm-5.3-flash": ("openrouter", "z-ai/glm-5.3-flash")}
# How many times a run may send junk back to the model before it counts as failed.
OUTPUT_RETRIES = 2


class SearchQuery(BaseModel):
    angle: str = Field(
        description="What this query looks for, in a few words of plain language."
    )
    query: str = Field(description="The search query itself, 3 to 7 words.")


class SearchQueries(BaseModel):
    queries: list[SearchQuery] = Field(min_length=5, max_length=5)


def _script(char: str) -> str | None:
    """Unicode script of a letter or wide punctuation mark, from its name
    ("CYRILLIC SMALL LETTER A" -> "CYRILLIC"); None for anything else."""
    category = unicodedata.category(char)
    if category.startswith("L"):
        return unicodedata.name(char, "UNKNOWN").split()[0]
    if category.startswith("P") and unicodedata.east_asian_width(char) in "WF":
        return "CJK"
    return None


def stray_characters(query: str, request: str) -> str:
    """Characters in a query from a writing system the request doesn't use.

    Luna sometimes ends a query with junk like `】【。` or a stray Russian word.
    Latin is always allowed: English terms are fine in any language."""
    allowed = {"LATIN"} | {_script(c) for c in request}
    return "".join(c for c in query if _script(c) not in allowed | {None})


def reject_stray_characters(ctx: RunContext[None], output: SearchQueries) -> SearchQueries:
    for item in output.queries:
        if stray := stray_characters(item.query, ctx.prompt or ""):
            raise ModelRetry(
                f"The query {item.query!r} contains stray characters {stray!r} "
                "that don't belong to the request's language. Rewrite it."
            )
    return output


def system_prompt(name: str) -> str:
    """Render prompts/<name>.md; {today} becomes today's date."""
    template = (HERE / "prompts" / f"{name}.md").read_text().strip()
    return template.replace("{today}", datetime.now(UTC).date().isoformat())


def build_agent(
    model_id: str, reasoning: str, prompt: str, output_mode: str = DEFAULT_OUTPUT_MODE
) -> Agent[None, SearchQueries]:
    load_dotenv(REPO / ".env")
    host = os.environ["LITE_LLM_PROXY_HOST"].rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    provider = OpenAIProvider(
        base_url=f"{host}/v1", api_key=os.environ["LITE_LLM_API_KEY"]
    )
    if model_id.startswith("openai/"):
        # Responses API: Chat Completions refuses tool-based output with reasoning on.
        model = OpenAIResponsesModel(model_id, provider=provider)
        settings = OpenAIResponsesModelSettings(openai_reasoning_effort=reasoning)
    else:
        # Everything else (e.g. OpenRouter models) goes through the proxy's
        # Chat Completions route, the same way the bot reaches LiteLLM.
        model = OpenAIChatModel(
            model_id,
            provider=provider,
            profile=OpenAIModelProfile(openai_supports_tool_choice_required=False),
        )
        settings = OpenAIChatModelSettings(openai_reasoning_effort=reasoning)
    agent = Agent(
        model,
        output_type=OUTPUT_MODES[output_mode](SearchQueries),
        system_prompt=prompt,
        model_settings=settings,
        retries=OUTPUT_RETRIES,
    )
    agent.output_validator(reject_stray_characters)
    return agent


def price_usd(model_id: str, usage) -> float | None:
    """List-price cost of one run, or None when the model has no known price."""
    provider, _, name = model_id.rpartition("/")
    provider, name = PRICE_REFS.get(model_id, (provider, name))
    try:
        return float(
            calc_price(usage, model_ref=name, provider_id=provider or "openai").total_price
        )
    except LookupError:
        return None


async def run_one(
    agent: Agent[None, SearchQueries], model_id: str, case: dict, sem: asyncio.Semaphore
) -> dict:
    async with sem:
        started = time.monotonic()
        try:
            result = await agent.run(case["request"])
        except Exception as exc:  # keep the rest of the run going
            return {**case, "queries": None, "error": repr(exc)}
        usage = result.usage
        return {
            **case,
            "queries": [q.model_dump() for q in result.output.queries],
            # More than 1 means the output was rejected and retried.
            "model_requests": usage.requests,
            "seconds": round(time.monotonic() - started, 2),
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cache_read_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.details.get("reasoning_tokens", 0),
            "cost_usd": price_usd(model_id, usage),
        }


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("error")]
    costs = [r["cost_usd"] for r in ok]
    total = None if not ok or None in costs else round(sum(costs), 6)
    words = [len(q["query"].split()) for r in ok for q in r["queries"]]
    return {
        "requests": len(rows),
        "succeeded": len(ok),
        "input_tokens": sum(r["input_tokens"] for r in ok),
        "output_tokens": sum(r["output_tokens"] for r in ok),
        "reasoning_tokens": sum(r["reasoning_tokens"] for r in ok),
        "total_cost_usd": total,
        "mean_cost_usd": None if total is None else round(total / len(ok), 6),
        "retried": sum(1 for r in ok if r["model_requests"] > 1),
        "mean_query_words": round(sum(words) / len(words), 1) if words else None,
        "max_query_words": max(words, default=None),
    }


def usd(value: float | None) -> str:
    return "unpriced" if value is None else f"${value:.6f}"


def render_markdown(report: dict) -> str:
    settings, summary = report["settings"], report["summary"]
    lines = [
        f"# Search queries: {settings['model']}",
        "",
        f"- **Model:** `{settings['model']}`",
        f"- **Reasoning:** {settings['reasoning_effort']}",
        f"- **Prompt:** `{settings['prompt']}`",
        f"- **Output mode:** {settings['output_mode']}",
        f"- **Run at:** {report['run_at']}",
        f"- **Succeeded:** {summary['succeeded']}/{summary['requests']}",
        f"- **Cost:** {usd(summary['total_cost_usd'])} total, "
        f"{usd(summary['mean_cost_usd'])} per request (list price)",
        f"- **Tokens:** {summary['input_tokens']} in, {summary['output_tokens']} out "
        f"({summary['reasoning_tokens']} reasoning)",
        f"- **Query length:** {summary['mean_query_words']} words on average, "
        f"{summary['max_query_words']} at most",
        f"- **Retried for stray characters:** {summary['retried']}",
        "",
        "## System prompt",
        "",
        "```text",
        report["system_prompt"],
        "```",
        "",
        "## Results",
        "",
    ]
    for row in report["results"]:
        lines += [f"### {row['id']}", "", f"> {row['request']}", ""]
        if row.get("error"):
            lines += [f"**Error:** `{row['error']}`", ""]
            continue
        lines += [
            f"{i}. `{q['query']}` — {q['angle']}"
            for i, q in enumerate(row["queries"], 1)
        ]
        retries = row["model_requests"] - 1
        lines += [
            "",
            f"_{usd(row['cost_usd'])} · {row['input_tokens']} in / "
            f"{row['output_tokens']} out ({row['reasoning_tokens']} reasoning) · "
            f"{row['seconds']} s"
            + (f" · retried {retries}×" if retries else "")
            + "_",
            "",
        ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--reasoning", choices=REASONING_LEVELS, default=DEFAULT_REASONING
    )
    parser.add_argument(
        "--prompt", default=DEFAULT_PROMPT, help="system prompt name in prompts/"
    )
    parser.add_argument(
        "--output-mode", choices=OUTPUT_MODES, default=DEFAULT_OUTPUT_MODE
    )
    parser.add_argument("--requests", type=Path, default=HERE / "requests.yaml")
    parser.add_argument("--only", nargs="*", help="request ids to run")
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()

    cases = yaml.safe_load(args.requests.read_text())
    if args.only:
        cases = [c for c in cases if c["id"] in set(args.only)]

    eval_prices.install()
    prompt = system_prompt(args.prompt)
    agent = build_agent(args.model, args.reasoning, prompt, args.output_mode)
    sem = asyncio.Semaphore(args.concurrency)
    run_at = datetime.now(UTC)
    rows = await asyncio.gather(*(run_one(agent, args.model, c, sem) for c in cases))

    report = {
        "run_at": run_at.isoformat(timespec="seconds"),
        "settings": {
            "model": args.model,
            "reasoning_effort": args.reasoning,
            "prompt": args.prompt,
            "output_mode": args.output_mode,
            "api": "openai-responses",
            "concurrency": args.concurrency,
        },
        "system_prompt": prompt,
        "summary": summarize(rows),
        "results": rows,
    }
    stamp = run_at.strftime("%Y%m%d-%H%M%S")
    reports = HERE / "reports"
    reports.mkdir(exist_ok=True)
    model = args.model.replace("/", "_")
    stem = reports / f"{model}-{args.reasoning}-{args.prompt}-{stamp}"
    stem.parent.joinpath(f"{stem.name}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False)
    )
    markdown = render_markdown(report)
    stem.parent.joinpath(f"{stem.name}.md").write_text(markdown)
    print(markdown)
    summary = report["summary"]
    print(
        f"\n{summary['succeeded']}/{summary['requests']} ok, "
        f"{usd(summary['total_cost_usd'])} -> {stem}.{{json,md}}"
    )


if __name__ == "__main__":
    asyncio.run(main())
