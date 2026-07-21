"""Run the chat-agent eval scenarios across multiple models, capturing
behaviour correctness + token usage/cost per model.

Tools are intentionally DISABLED so we measure the decision/response
behaviour in isolation (and never fire paid web search). Costs therefore
reflect the decision turn without tool schemas/tool calls.

Usage:
    uv run python scripts/chat_eval_matrix.py            # full matrix
    uv run python scripts/chat_eval_matrix.py --smoke    # 1 model x 1 scenario
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# --- load .env (last non-empty value wins; OPENAI_API_KEY appears twice) ---
import os

for _line in Path(".env").read_text().splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _v = _line.split("=", 1)
    _v = _v.strip().strip('"').strip("'")
    if _v:
        os.environ.setdefault(_k, _v) if _k in os.environ else os.environ.__setitem__(_k, _v)

from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_eval import _StubBot, _build_call, _parse_scenario  # noqa: E402
import eval_prices  # noqa: E402
from genai_prices import Usage, calc_price  # noqa: E402

from smarter_dev.bot.agents.chat_agent import SYSTEM_PROMPT  # noqa: E402
from smarter_dev.bot.agents.chat_models import AgentReturn  # noqa: E402
from smarter_dev.bot.agents.chat_tools import ChatDeps  # noqa: E402

# friendly name -> (provider, api model id)
MODELS = {
    "Gemini 3.1 Flash Lite": ("google", "gemini-3.1-flash-lite"),
    "Gemini 3 Flash": ("google", "gemini-3-flash-preview"),
    "GPT 5.6 Luna": ("openai", "gpt-5.6-luna"),
    "GPT 5.4 Nano": ("openai", "gpt-5.4-nano"),
    "GPT 5.4 Mini": ("openai", "gpt-5.4-mini"),
}

SCENARIO_DIR = Path("scripts/chat_eval_scenarios")

# expected decision per scenario: "respond" | "silent" | "either"
EXPECT = {
    "bystander_message": "silent",
    "reply_to_other_user": "silent",
    "reply_to_other_with_mention": "respond",
    "reply_to_bot": "respond",
    "bare_mention_summons": "respond",
    "directed_cs_followup_must_answer": "respond",
    "off_topic_brief": "either",
}


def build_agent(provider: str, model_id: str) -> Agent:
    if provider == "google":
        model = GoogleModel(
            model_id,
            provider=GoogleProvider(
                api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            ),
        )
        settings = GoogleModelSettings(
            google_thinking_config={"thinking_level": "MEDIUM"}
        )
    elif provider == "openai":
        model = OpenAIResponsesModel(
            model_id, provider=OpenAIProvider(api_key=os.getenv("OPENAI_API_KEY"))
        )
        settings = OpenAIResponsesModelSettings(openai_reasoning_effort="medium")
    else:
        raise ValueError(provider)
    # NO tools — isolate decision behaviour, never fire paid web search.
    return Agent(
        model,
        output_type=AgentReturn,
        deps_type=ChatDeps,
        system_prompt=SYSTEM_PROMPT,
        model_settings=settings,
    )


def grade(name: str, output) -> tuple[bool, str]:
    expect = EXPECT[name]
    responded = output.response is not None
    if expect == "respond":
        if not responded:
            return False, "expected a response, stayed silent"
        if name == "directed_cs_followup_must_answer" and output.response.not_cs_topic_brief_answer:
            return True, "responded (but flagged non-CS; expected a full coding answer)"
        return True, "responded"
    if expect == "silent":
        return (not responded), ("stayed silent" if not responded else "spoke when it should have stayed silent")
    # either
    if not responded:
        return True, "silent (acceptable for off-topic)"
    if output.response.not_cs_topic_brief_answer:
        return True, "brief reply (acceptable)"
    return True, "replied but not flagged brief (allowed, watch length)"


async def run_one(
    agent: Agent, scenario_path: Path, provider: str, model_id: str
) -> dict:
    scenario = _parse_scenario(scenario_path)
    user_prompt, history = _build_call(scenario)
    deps = ChatDeps(
        bot=_StubBot(),
        channel_id=int(scenario.channel.channel_id)
        if scenario.channel.channel_id.isdigit()
        else 0,
        guild_id=0,
    )
    t0 = time.monotonic()
    result = await agent.run(
        user_prompt=user_prompt, message_history=history, deps=deps
    )
    dt = time.monotonic() - t0
    usage = result.usage()
    output = result.output
    ok, note = grade(scenario_path.stem, output)
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache = getattr(usage, "cache_read_tokens", 0) or 0
    pd = calc_price(
        Usage(input_tokens=inp, cache_read_tokens=cache, output_tokens=out),
        model_ref=model_id,
        provider_id=provider,
    )
    return {
        "scenario": scenario_path.stem,
        "expect": EXPECT[scenario_path.stem],
        "responded": output.response is not None,
        "pass": ok,
        "note": note,
        "input_tokens": inp,
        "cache_read_tokens": cache,
        "output_tokens": out,
        "requests": getattr(usage, "requests", 0) or 0,
        "cost_usd": float(pd.total_price),
        "seconds": round(dt, 2),
        "usage_repr": repr(usage),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--only", default=None, help="Run just this friendly model name")
    args = ap.parse_args()

    print("price overrides installed:", eval_prices.install(), flush=True)

    scenarios = sorted(SCENARIO_DIR.glob("*.yaml"))
    models = MODELS
    if args.smoke:
        models = {"Gemini 3.1 Flash Lite": MODELS["Gemini 3.1 Flash Lite"]}
        scenarios = [SCENARIO_DIR / "directed_cs_followup_must_answer.yaml"]
    elif args.only:
        models = {args.only: MODELS[args.only]}

    out = Path("scripts/.eval_matrix_results.json")
    # merge into any existing results so per-model runs accumulate
    all_results = {}
    if out.exists():
        try:
            all_results = json.loads(out.read_text())
        except Exception:
            all_results = {}
    for friendly, (provider, model_id) in models.items():
        agent = build_agent(provider, model_id)
        rows = []
        for sc in scenarios:
            try:
                rows.append(await run_one(agent, sc, provider, model_id))
            except Exception as e:
                rows.append(
                    {
                        "scenario": sc.stem,
                        "expect": EXPECT[sc.stem],
                        "error": repr(e)[:300],
                        "pass": False,
                    }
                )
            r = rows[-1]
            status = "PASS" if r.get("pass") else "FAIL"
            print(
                f"[{friendly}] {sc.stem}: {status} "
                f"({r.get('note', r.get('error',''))}) "
                f"in/out={r.get('input_tokens')}/{r.get('output_tokens')} "
                f"${r.get('cost_usd', 0):.6f}",
                flush=True,
            )
        all_results[friendly] = {"provider": provider, "model_id": model_id, "rows": rows}
        out.write_text(json.dumps(all_results, indent=2))  # incremental save

    # summary
    print("\n" + "=" * 64)
    print(f"{'model':<22} {'pass':>6} {'tok in/out':>14} {'cost $':>10}")
    print("-" * 64)
    for friendly, data in all_results.items():
        rows = data["rows"]
        npass = sum(1 for r in rows if r.get("pass"))
        ti = sum(r.get("input_tokens", 0) for r in rows)
        to = sum(r.get("output_tokens", 0) for r in rows)
        cost = sum(r.get("cost_usd", 0) for r in rows)
        print(f"{friendly:<22} {npass:>3}/{len(rows):<2} {ti:>7}/{to:<6} {cost:>10.5f}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
