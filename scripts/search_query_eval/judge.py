#!/usr/bin/env python
"""Result-judging eval: how well Jev rates search results against a request.

Takes a run.py report, searches Brave for each of its queries (5 results
each, so 25 per request), and asks Jev, TypeSafe's classifier, two yes/no
questions about every result: is it relevant to the user's request, and is it
a good-quality source. Jev only sees each result's domain and snippet. One Jev
call per request covers all 25 results.

Brave results are cached in cache/brave/, so reruns judge exactly the same
results and cost no searches.

Usage:
    uv run python scripts/search_query_eval/judge.py
    uv run python scripts/search_query_eval/judge.py --queries reports/<run>.json --only event-today

Reads BRAVE_SEARCH_API_KEY and TYPESAFE_API_KEY from .env. Writes
scripts/search_query_eval/reports/judge-<model>-<prompt>-<timestamp>.{json,md}.
Jev's wording lives in judge_prompts/<prompt>.yaml.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
import httpx2
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, create_model
from pydantic_ai import Agent
from pydantic_ai.models.typesafe import TypeSafeModel
from pydantic_ai.providers.typesafe import TypeSafeProvider

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

HERE = Path(__file__).parent
REPO = HERE.parent.parent

DEFAULT_QUERIES = HERE / "reports" / "openai_gpt-6-luna-medium-v6-20260930-183448.json"
DEFAULT_MODEL = "typesafe:jev-1.13.0"
RESULTS_PER_QUERY = 5
CACHE = HERE / "cache" / "brave"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
# Brave's base plan allows 1 request a second.
BRAVE_INTERVAL_SECONDS = 1.1
# pydantic_ai's TypeSafe default, as in the message gate.
BOOLEAN_THRESHOLD = 0.5
# Jev list price (.env.example, 2026-09-15); output tokens are free.
INPUT_PRICE_PER_MILLION_USD = 0.042

DEFAULT_PROMPT = "v5"


def load_prompt(name: str) -> dict:
    """judge_prompts/<name>.yaml: instructions plus the two per-result questions."""
    prompt = yaml.safe_load((HERE / "judge_prompts" / f"{name}.yaml").read_text())
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    for placeholder, value in {
        "{today}": today.isoformat(),
        "{weekday}": f"{today:%A}",
        "{this_week}": f"Monday {monday.isoformat()} to Sunday {sunday.isoformat()}",
    }.items():
        prompt["instructions"] = prompt["instructions"].replace(placeholder, value)
    return prompt


def brave_cache_path(query: str, count: int) -> Path:
    digest = hashlib.sha256(f"{count}\n{query}".encode()).hexdigest()[:16]
    return CACHE / f"{digest}.json"


def clean_snippet(text: str) -> str:
    """Brave snippets carry <strong> highlight tags and HTML entities."""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def domain_of(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.")


class Brave:
    """Brave web search with an on-disk cache keyed by query and count."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.last_call = 0.0
        self.searches = 0
        self.lock = asyncio.Lock()

    async def search(self, query: str, count: int = RESULTS_PER_QUERY) -> list[dict]:
        path = brave_cache_path(query, count)
        if path.exists():
            return json.loads(path.read_text())["results"]
        async with self.lock:
            wait = self.last_call + BRAVE_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            response = await self.client.get(
                BRAVE_URL,
                params={"q": query, "count": count},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"],
                },
            )
            self.last_call = time.monotonic()
        response.raise_for_status()
        self.searches += 1
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "domain": domain_of(item.get("url", "")),
                "snippet": clean_snippet(item.get("description", "")),
            }
            for item in response.json().get("web", {}).get("results", [])[:count]
        ]
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "query": query,
                    "count": count,
                    "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "results": results,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return results


def material(request: str, results: list[dict]) -> str:
    """The text Jev judges: the request, then every numbered result."""
    lines = [f"REQUEST:\n{request.strip()}", "", "RESULTS:"]
    for n, result in enumerate(results, 1):
        lines.append(f"[{n}] {result['domain']}: {result['snippet']}")
    return "\n".join(lines)


class _JudgmentsBase(BaseModel):
    """Which search RESULTS are relevant to the REQUEST and of good quality."""


def judgment_model(results: list[dict], prompt: dict) -> type[BaseModel]:
    """Two bools per result; each description is the question about it."""
    fields = {}
    for n, result in enumerate(results, 1):
        where = f"RESULT [{n}] ({result['domain']})"
        fields[f"result_{n}_relevant"] = (
            bool,
            Field(description=prompt["relevant"].format(where=where)),
        )
        fields[f"result_{n}_quality"] = (
            bool,
            Field(description=prompt["quality"].format(where=where)),
        )
    return create_model("ResultJudgments", __base__=_JudgmentsBase, **fields)


def build_model(model_id: str) -> TypeSafeModel:
    # Same as smarter_dev.bot.proactive.models.build_twopass_model, which can't
    # be imported without the Discord stack: stay on gzip, since httpx2 can't
    # decode TypeSafe's brotli responses.
    http_client = httpx2.AsyncClient(headers={"Accept-Encoding": "gzip, deflate"})
    return TypeSafeModel(
        model_id.removeprefix("typesafe:"),
        provider=TypeSafeProvider(http_client=http_client),
    )


async def judge_request(model, prompt: dict, case: dict, brave: Brave) -> dict:
    results = []
    for index, item in enumerate(case["queries"], 1):
        for rank, hit in enumerate(await brave.search(item["query"]), 1):
            results.append({"query_index": index, "rank": rank, **hit})
    record = {"id": case["id"], "request": case["request"], "results": results}
    if not results:
        record["error"] = "Brave returned no results"
        return record
    agent = Agent(model, output_type=judgment_model(results, prompt), retries=0)
    started = time.perf_counter()
    try:
        run = await agent.run(
            material(case["request"], results),
            instructions=prompt["instructions"],
            model_settings={"timeout": 60, "typesafe_boolean_threshold": BOOLEAN_THRESHOLD},
        )
    except Exception as error:  # noqa: BLE001 - report the failure, keep going
        record["error"] = f"{type(error).__name__}: {error}"
        return record
    record["seconds"] = round(time.perf_counter() - started, 2)
    verdicts = run.output.model_dump()
    details = run.response.provider_details or {}
    confidence = details.get("confidence") or {}
    probabilities = details.get("probabilities") or {}
    for n, result in enumerate(results, 1):
        for question in ("relevant", "quality"):
            name = f"result_{n}_{question}"
            result[question] = verdicts[name]
            result[f"{question}_confidence"] = confidence.get(name)
            result[f"{question}_probability"] = probabilities.get(name)
    record["model_name"] = run.response.model_name
    record["input_tokens"] = run.usage.input_tokens or 0
    record["output_tokens"] = run.usage.output_tokens or 0
    record["cost_usd"] = round(record["input_tokens"] * INPUT_PRICE_PER_MILLION_USD / 1e6, 6)
    return record


def summarize(records: list[dict]) -> dict:
    ok = [r for r in records if "error" not in r]
    judged = [res for r in ok for res in r["results"]]
    seconds = sorted(r["seconds"] for r in ok)
    return {
        "requests": len(records),
        "succeeded": len(ok),
        "failed": len(records) - len(ok),
        "results_judged": len(judged),
        "relevant": sum(res["relevant"] for res in judged),
        "good_quality": sum(res["quality"] for res in judged),
        "duplicate_urls": sum(
            len(r["results"]) - len({res["url"] for res in r["results"]}) for r in ok
        ),
        "input_tokens": sum(r["input_tokens"] for r in ok),
        "cost_usd": round(sum(r["cost_usd"] for r in ok), 6),
        "median_seconds": seconds[len(seconds) // 2] if seconds else None,
        "max_seconds": seconds[-1] if seconds else None,
    }


def mark(value: bool, conf: float | None) -> str:
    text = "yes" if value else "no"
    return f"{text} {conf:.2f}" if conf is not None else text


def render_markdown(report: dict) -> str:
    s, settings = report["summary"], report["settings"]
    lines = [
        "# Search result judging eval",
        "",
        f"- **Judge:** `{settings['model']}` (threshold {settings['boolean_threshold']})",
        f"- **Prompt:** `{settings.get('prompt', 'v1')}`",
        f"- **Queries from:** `{settings['queries_report']}`",
        f"- **Search:** Brave, {settings['results_per_query']} results per query",
        f"- **Run at:** {report['run_at']}",
        f"- **Requests:** {s['succeeded']}/{s['requests']} judged, {s['results_judged']} results",
        f"- **Jev says relevant:** {s['relevant']} · **good quality:** {s['good_quality']}",
        f"- **Duplicate URLs within a request:** {s['duplicate_urls']}",
        f"- **Cost:** ${s['cost_usd']:.6f} ({s['input_tokens']} input tokens, list price)",
        f"- **Jev time per request:** median {s['median_seconds']} s, slowest {s['max_seconds']} s",
        "",
        "Each cell is Jev's answer and its confidence (0 undecided, 1 certain).",
        "",
        "## Instructions",
        "",
        "```text",
        report["instructions"],
        "```",
        "",
        "Per-result questions:",
        "",
        *(f"- {name}: `{text}`" for name, text in report.get("questions", {}).items()),
        "",
        "## Results",
    ]
    for record in report["results"]:
        lines += ["", f"### {record['id']}", "", f"> {record['request'].strip()}", ""]
        if "error" in record:
            lines += [f"**Error:** {record['error']}", ""]
        queries = {}
        for res in record["results"]:
            queries.setdefault(res["query_index"], []).append(res)
        seen = set()
        for index, group in queries.items():
            lines += ["", f"**Q{index}: `{group[0].get('query', '')}`**", ""]
            lines += ["| # | Domain | Snippet | Relevant | Quality |", "|---|---|---|---|---|"]
            for res in group:
                snippet = res["snippet"].replace("|", "\\|").replace("\n", " ")
                if len(snippet) > 160:
                    snippet = snippet[:157] + "…"
                dup = " (dup)" if res["url"] in seen else ""
                seen.add(res["url"])
                if "relevant" in res:
                    rel = mark(res["relevant"], res["relevant_confidence"])
                    qual = mark(res["quality"], res["quality_confidence"])
                else:
                    rel = qual = "—"
                lines.append(
                    f"| {res['rank']} | [{res['domain']}]({res['url']}){dup} | {snippet} | {rel} | {qual} |"
                )
        if "cost_usd" in record:
            lines += [
                "",
                f"_${record['cost_usd']:.6f} · {record['input_tokens']} in · {record['seconds']} s_",
            ]
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES, help="run.py report JSON")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="wording in judge_prompts/")
    parser.add_argument("--only", nargs="*", help="request ids to judge")
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args()

    load_dotenv(REPO / ".env")
    source = json.loads(args.queries.read_text())
    requests = {r["id"]: r["request"] for r in yaml.safe_load((HERE / "requests.yaml").read_text())}
    cases = [
        {"id": r["id"], "request": requests[r["id"]], "queries": r["queries"]}
        for r in source["results"]
        if r.get("queries") and (not args.only or r["id"] in args.only)
    ]
    model = build_model(args.model)
    prompt = load_prompt(args.prompt)
    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=30) as client:
        brave = Brave(client)

        async def one(case: dict) -> dict:
            async with semaphore:
                record = await judge_request(model, prompt, case, brave)
            for res in record["results"]:
                res["query"] = case["queries"][res["query_index"] - 1]["query"]
            print(f"{case['id']}: {record.get('error', 'ok')}", file=sys.stderr)
            return record

        records = await asyncio.gather(*(one(case) for case in cases))
        searches = brave.searches

    try:
        queries_report = str(args.queries.resolve().relative_to(HERE))
    except ValueError:
        queries_report = str(args.queries)
    report = {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "settings": {
            "model": args.model,
            "prompt": args.prompt,
            "boolean_threshold": BOOLEAN_THRESHOLD,
            "queries_report": queries_report,
            "results_per_query": RESULTS_PER_QUERY,
            "judged_fields": "domain and snippet",
            "brave_searches_this_run": searches,
        },
        "instructions": prompt["instructions"],
        "questions": {"relevant": prompt["relevant"], "quality": prompt["quality"]},
        "summary": summarize(records),
        "results": records,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem = HERE / "reports" / f"judge-{args.model.replace(':', '_')}-{args.prompt}-{stamp}"
    stem.parent.joinpath(f"{stem.name}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False)
    )
    stem.parent.joinpath(f"{stem.name}.md").write_text(render_markdown(report))
    s = report["summary"]
    print(
        f"{s['succeeded']}/{s['requests']} judged, {s['relevant']}/{s['results_judged']} relevant, "
        f"{s['good_quality']} good quality, {searches} Brave searches, ${s['cost_usd']:.6f} -> {stem}.{{json,md}}"
    )


if __name__ == "__main__":
    asyncio.run(main())
