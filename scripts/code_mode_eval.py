"""Code-mode eval harness — can the model write *passing* Monty Python?

Tests whether a model (default Gemini 3.1 Flash Lite) can write code in
Pydantic Monty's sandboxed Python subset when given a ``run_code`` tool plus
host functions (``web_search`` / ``web_read``). This is a manual "code mode":
the model writes Python, we execute it in Monty, hand back stdout + the final
expression's value (or the error so it can self-correct), and it answers.

The harness is a DATA COLLECTOR, not an auto-grader: it records every
``run_code`` call (code + result/error), the final answer, token usage, wall
time, and the number of code runs per case, then prints a transcript for a
human (or Claude) to judge.

Usage:
    uv run python scripts/code_mode_eval.py                  # all cases
    uv run python scripts/code_mode_eval.py --only date      # cases matching substring
    uv run python scripts/code_mode_eval.py --no-web         # skip web cases (fast/offline)
    uv run python scripts/code_mode_eval.py --model gemini-3-flash-preview
    uv run python scripts/code_mode_eval.py --json reports/code_mode.json

Web cases hit the live Brave/Jina backends and can take 30s–2m each.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- load .env (same lightweight loader the matrix runner uses) -------------
for _line in Path(".env").read_text().splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _v = _line.split("=", 1)
    _v = _v.strip().strip('"').strip("'")
    if _v:
        os.environ[_k] = _v

import httpx  # noqa: E402
import pydantic_monty as monty  # noqa: E402
from pydantic_ai import Agent, RunContext  # noqa: E402
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings  # noqa: E402
from pydantic_ai.providers.google import GoogleProvider  # noqa: E402

from smarter_dev.bot.utils import web_fetch  # noqa: E402
from smarter_dev.web.scan.tools import brave_search  # noqa: E402

DEFAULT_MODEL = "gemini-3.1-flash-lite"

# Monty resource limits. We deliberately omit a duration cap and let the
# per-case asyncio timeout govern wall-clock, so a slow but legitimate web
# call isn't killed mid-flight as if it were a CPU-bound runaway.
MONTY_LIMITS: dict[str, Any] = {
    "max_memory": 256 * 1024 * 1024,
    "max_recursion_depth": 500,
}

SYSTEM_PROMPT = """\
You are a coding agent operating in CODE MODE. To answer a question you WRITE \
PYTHON CODE and execute it with the `run_code` tool, then report the result. Do \
not do arithmetic, date math, parsing, or data crunching in your head — compute \
it with code.

`run_code(reason: str, code: str)` runs your code in a secure sandbox (Pydantic \
Monty) and returns its stdout plus the value of the final expression (like a \
notebook cell). ``reason`` is a short, plain-language note (5-10 words) about \
why you're running this code — it is shown to the channel as a status message, \
so write it for a human (e.g. "Calculating the 30-day compound total", \
"Checking the latest Python version"), not as a code comment.

The sandbox is a RESTRICTED subset of Python:
- Allowed stdlib only (import if needed): sys, os, typing, asyncio, re, \
datetime, json. NOTHING else.
- NO third-party packages (no requests, numpy, pandas, etc.).
- NO `class` definitions and NO `match` statements (unsupported).
- def / async def, loops, comprehensions, f-strings, and dict/list/set/tuple \
all work.

Host functions available INSIDE the sandbox (call directly; await the async \
ones):
- async web_search(query: str) -> list[dict]  # up to 5 results; each {title, url, description}
- async web_read(url: str) -> dict            # {title, description, content, url}

The value of the LAST expression is returned to you. Use that (or print()) to \
surface results.

Workflow:
1. Write code that computes the answer; call web_search / web_read for live info.
2. Call run_code with a short human-readable `reason` and your `code`.
3. If it errors, read the error, fix the code, and run again.
4. Once you have the result, reply in plain language with the final answer.
Keep code minimal and correct."""


# --- host functions exposed inside the Monty sandbox ------------------------
async def host_web_search(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await brave_search(client, query, num_results=5)


async def host_web_read(url: str) -> dict:
    if web_fetch.is_youtube_url(url):
        meta = await web_fetch.fetch_youtube_metadata(url)
        return {
            "url": url,
            "title": meta.get("title", ""),
            "description": meta.get("description", ""),
            "content": meta.get("description", ""),
        }
    if url.lower().endswith(".pdf"):
        text = await web_fetch.fetch_pdf_text(url)
        return {"url": url, "title": "", "description": "", "content": text or ""}
    data = await web_fetch.fetch_via_jina(url)
    if data is None:
        return {"url": url, "title": "", "description": "", "content": "", "error": "fetch_failed"}
    return data


HOST_FUNCTIONS = {"web_search": host_web_search, "web_read": host_web_read}


@dataclass
class _CodeRun:
    code: str
    ok: bool
    reason: str = ""
    result_repr: str | None = None
    stdout: str = ""
    error: str | None = None


@dataclass
class Recorder:
    """Per-case capture of every run_code invocation."""

    runs: list[_CodeRun] = field(default_factory=list)


@dataclass
class CodeModeDeps:
    recorder: Recorder


async def run_code(ctx: RunContext[CodeModeDeps], reason: str, code: str) -> str:
    """Execute Python in the Monty sandbox; return stdout + final value, or the error.

    ``reason`` is a short, user-facing explanation of WHY you're running this
    code (e.g. "Calculating the 30-day compound total"). It is shown to the
    channel as a status message — in production this maps to ``_post_status``,
    the same mechanism web_search/web_read use.
    """
    # Mimic the production status post (``> -# {reason}``) so the eval shows
    # exactly what the channel would see.
    print(f"   status> -# {reason}", file=sys.stderr)

    collector = monty.CollectStreams()
    try:
        compiled = monty.Monty(code)
    except monty.MontyError as e:  # syntax / typing failure at compile time
        ctx.deps.recorder.runs.append(
            _CodeRun(code=code, ok=False, reason=reason, error=f"{type(e).__name__}: {e}")
        )
        return f"COMPILE ERROR — {type(e).__name__}: {e}"

    try:
        value = await compiled.run_async(
            external_functions=HOST_FUNCTIONS,
            limits=MONTY_LIMITS,
            print_callback=collector,
        )
    except monty.MontyError as e:
        stdout = "".join(text for stream, text in collector.output if stream == "stdout")
        ctx.deps.recorder.runs.append(
            _CodeRun(
                code=code,
                ok=False,
                reason=reason,
                stdout=stdout,
                error=f"{type(e).__name__}: {e}",
            )
        )
        tail = f"\n--- stdout before error ---\n{stdout}" if stdout else ""
        return f"RUNTIME ERROR — {type(e).__name__}: {e}{tail}"

    stdout = "".join(text for stream, text in collector.output if stream == "stdout")
    result_repr = repr(value)
    ctx.deps.recorder.runs.append(
        _CodeRun(
            code=code, ok=True, reason=reason, result_repr=result_repr, stdout=stdout
        )
    )
    parts = []
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    parts.append(f"return value: {result_repr}")
    return "\n".join(parts)


# --- eval cases -------------------------------------------------------------
# `expect` is guidance for the human/Claude judge, not an automated assertion.
CASES: list[dict[str, Any]] = [
    {
        "name": "compound_interest",
        "web": False,
        "prompt": (
            "A member starts with 1240 bytes and earns 8% interest per day, "
            "compounded daily and floored to a whole number of bytes at the end "
            "of each day. How many bytes do they have after 30 days?"
        ),
        "expect": "Deterministic integer from a 30-iteration floor-compounding loop.",
    },
    {
        "name": "date_diff",
        "web": False,
        "prompt": (
            "How many days are there from 2026-01-15 to 2026-11-03, counting the "
            "end date but not the start date?"
        ),
        "expect": "(date(2026,11,3) - date(2026,1,15)).days == 292.",
    },
    {
        "name": "hex_color_regex",
        "web": False,
        "prompt": (
            "How many of these strings are valid 6-digit hex colors (a # followed "
            "by exactly six hex digits)? "
            "['#1a2b3c', '#FFF', '123456', '#abcdez', '#000000', '#AABBCC', '#12 34 56']"
        ),
        "expect": "3 valid: #1a2b3c, #000000, #AABBCC.",
    },
    {
        "name": "fib_even_sum",
        "web": False,
        "prompt": "What is the sum of all even Fibonacci numbers strictly less than 100?",
        "expect": "2 + 8 + 34 == 44.",
    },
    {
        "name": "json_top_scorer",
        "web": False,
        "prompt": (
            "Here is JSON of squad scores: "
            '[{"squad":"Red","pts":42},{"squad":"Blue","pts":71},'
            '{"squad":"Green","pts":68},{"squad":"Gold","pts":71}]. '
            "Which squad has the highest points, and what is the value? If there "
            "is a tie, list all tied squads."
        ),
        "expect": "Tie at 71 between Blue and Gold.",
    },
    {
        "name": "longest_word",
        "web": False,
        "prompt": (
            "In the sentence \"Self documenting code needs descriptive "
            "identifiers everywhere\", what is the longest word and how many "
            "letters does it have?"
        ),
        "expect": "'documenting' (11) and 'descriptive' (11) tie at 11 letters.",
    },
    {
        "name": "web_read_title",
        "web": True,
        "prompt": (
            "Use web_read to fetch https://example.com and tell me the exact "
            "title of the page."
        ),
        "expect": "Title is 'Example Domain'.",
    },
    # NOTE: web_search (Brave) is still exposed to the sandbox as a host function,
    # but there are no search-dependent cases here: BRAVE_SEARCH_API_KEY is not
    # configured locally, so a search case can't pass deterministically. Add one
    # once a Brave key is available in the environment.
]


def build_agent(model_id: str) -> Agent[CodeModeDeps, str]:
    model = GoogleModel(
        model_id,
        provider=GoogleProvider(
            api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        ),
    )
    settings = GoogleModelSettings(google_thinking_config={"thinking_level": "MEDIUM"})
    return Agent(
        model,
        deps_type=CodeModeDeps,
        system_prompt=SYSTEM_PROMPT,
        model_settings=settings,
        tools=[run_code],
    )


async def run_case(agent: Agent, case: dict, timeout: float) -> dict[str, Any]:
    recorder = Recorder()
    deps = CodeModeDeps(recorder=recorder)
    t0 = time.monotonic()
    status = "completed"
    answer = ""
    usage_repr = ""
    inp = out = reqs = 0
    try:
        result = await asyncio.wait_for(
            agent.run(case["prompt"], deps=deps), timeout=timeout
        )
        answer = result.output
        usage = result.usage()
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        reqs = getattr(usage, "requests", 0) or 0
        usage_repr = repr(usage)
    except asyncio.TimeoutError:
        status = "TIMEOUT"
    except Exception as e:  # noqa: BLE001 — capture model/transport failures per case
        status = f"ERROR: {type(e).__name__}: {e}"
    dt = time.monotonic() - t0
    return {
        "name": case["name"],
        "web": case["web"],
        "prompt": case["prompt"],
        "expect": case["expect"],
        "status": status,
        "seconds": round(dt, 1),
        "code_runs": [
            {
                "code": r.code,
                "ok": r.ok,
                "reason": r.reason,
                "result": r.result_repr,
                "stdout": r.stdout,
                "error": r.error,
            }
            for r in recorder.runs
        ],
        "n_runs": len(recorder.runs),
        "last_run_ok": recorder.runs[-1].ok if recorder.runs else None,
        "answer": answer,
        "input_tokens": inp,
        "output_tokens": out,
        "requests": reqs,
        "usage_repr": usage_repr,
    }


def print_case(res: dict) -> None:
    bar = "=" * 78
    print(f"\n{bar}\nCASE: {res['name']}  ({'web' if res['web'] else 'pure'})  "
          f"status={res['status']}  {res['seconds']}s  runs={res['n_runs']}  "
          f"tokens={res['input_tokens']}->{res['output_tokens']}\n{bar}")
    print(f"PROMPT: {res['prompt']}")
    print(f"EXPECT (judge note): {res['expect']}")
    for i, run in enumerate(res["code_runs"], 1):
        flag = "OK" if run["ok"] else "FAIL"
        print(f"\n--- run #{i} [{flag}] ---")
        print(f"  status> -# {run.get('reason', '')}")
        print(run["code"])
        if run["stdout"]:
            print(f"  stdout> {run['stdout'].rstrip()}")
        if run["ok"]:
            print(f"  return> {run['result']}")
        else:
            print(f"  error> {run['error']}")
    print(f"\nFINAL ANSWER:\n{res['answer'] or '(none)'}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--only", default=None, help="run cases whose name contains this")
    parser.add_argument("--no-web", action="store_true", help="skip web cases")
    parser.add_argument("--web-timeout", type=float, default=120.0)
    parser.add_argument("--pure-timeout", type=float, default=45.0)
    parser.add_argument("--json", default=None, help="write full results JSON to this path")
    args = parser.parse_args()

    cases = CASES
    if args.no_web:
        cases = [c for c in cases if not c["web"]]
    if args.only:
        cases = [c for c in cases if args.only in c["name"]]
    if not cases:
        print("no cases matched", file=sys.stderr)
        sys.exit(1)

    agent = build_agent(args.model)
    print(f"model: {args.model}   cases: {len(cases)}   "
          f"(web timeout {args.web_timeout}s, pure {args.pure_timeout}s)")

    results = []
    for case in cases:
        timeout = args.web_timeout if case["web"] else args.pure_timeout
        res = await run_case(agent, case, timeout)
        print_case(res)
        results.append(res)

    # summary table
    print(f"\n{'#' * 78}\nSUMMARY ({args.model})\n{'#' * 78}")
    print(f"{'case':28} {'status':10} {'runs':>4} {'last_ok':>7} {'secs':>6}")
    for r in results:
        print(f"{r['name']:28} {r['status'][:10]:10} {r['n_runs']:>4} "
              f"{str(r['last_run_ok']):>7} {r['seconds']:>6}")

    if args.json:
        out_path = Path(args.json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"model": args.model, "results": results}, indent=2))
        print(f"\nwrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
