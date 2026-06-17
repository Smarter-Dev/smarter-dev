"""Capability probe: Gemini 3.1 Flash Lite vs Gemini 3 Flash on coding
questions across a difficulty gradient. Tools OFF (pure model knowledge,
no paid web search). Dumps answers to scripts/.capability_results.json for
manual evaluation.

Usage: uv run python scripts/chat_capability.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat_eval_matrix import build_agent  # noqa: E402

from chat_eval import _StubBot  # noqa: E402
from smarter_dev.bot.agents.chat_input_format import build_agent_call  # noqa: E402
from smarter_dev.bot.agents.chat_models import (  # noqa: E402
    Author,
    ChannelInfo,
    InitialAgentInput,
    Me,
    Message,
)
from smarter_dev.bot.agents.chat_tools import ChatDeps  # noqa: E402

MODELS = {
    "Gemini 3.1 Flash Lite": ("google", "gemini-3.1-flash-lite"),
    "Gemini 3 Flash": ("google", "gemini-3-flash-preview"),
}

# (id, difficulty, question)
QUESTIONS = [
    ("easy_is_vs_eq", "easy",
     "quick q — what's the actual difference between `is` and `==` in python? "
     "i keep seeing people say don't use `==` for None"),
    ("easy_list_vs_tuple", "easy",
     "besides tuples being immutable, is there any real reason to pick a tuple "
     "over a list?"),
    ("med_asyncio_cores", "medium",
     "if i move my CPU-heavy loop into asyncio with a bunch of tasks, will it "
     "actually run across my cores and go faster?"),
    ("med_mutable_default", "medium",
     "got a weird bug — my function `def add(item, bucket=[])` keeps "
     "remembering items from previous calls even though i never pass bucket. "
     "what's going on?"),
    ("med_static_vs_class", "medium",
     "when would i actually reach for @classmethod vs @staticmethod? feels like "
     "they do the same thing"),
    ("hard_lru_cache_leak", "hard",
     "i slapped @lru_cache on a method to speed it up and now my process memory "
     "just climbs forever and the instances never get freed. why would caching "
     "a method cause a leak?"),
    ("hard_float_eq", "hard",
     "why does 0.1 + 0.2 == 0.3 come back False in python, and what's the "
     "correct way to compare them?"),
    ("hard_baseexception", "hard",
     "i wrapped my main loop in `try/except Exception` to catch everything but "
     "ctrl+c still kills it instantly and so does sys.exit(). how is it getting "
     "past my except?"),
    ("vhard_lost_task_exc", "very_hard",
     "i create background work with asyncio.create_task() and don't await it. "
     "every so often a task hits an error but nothing is logged and the program "
     "just carries on like nothing happened. what's actually happening and how "
     "do i stop losing those exceptions?"),
]

ME = Me(user_id="999", username="smarterbot")
CH = ChannelInfo(channel_id="100", name="dev-help", description="coding help")
USER = Author(user_id="1", username="dev")


async def ask(agent, qid: str, body: str) -> dict:
    msg = Message(
        message_id="500",
        author_id="1",
        body=f"@smarterbot {body}",
        mentions_bot=True,
        sent_at=datetime.now(UTC),
    )
    agent_input = InitialAgentInput(
        me=ME, channel_history=[], activation_message=msg,
        authors=[USER], channel=CH, now_utc=datetime.now(UTC),
    )
    user_prompt, history = build_agent_call(agent_input, prior_history=[])
    deps = ChatDeps(bot=_StubBot(), channel_id=100, guild_id=0)
    result = await agent.run(
        user_prompt=user_prompt, message_history=history, deps=deps
    )
    out = result.output
    usage = result.usage()
    return {
        "responded": out.response is not None,
        "message": out.response.message if out.response else None,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
    }


async def main() -> int:
    results: dict = {}
    for friendly, (prov, mid) in MODELS.items():
        agent = build_agent(prov, mid)
        for qid, diff, body in QUESTIONS:
            r = await ask(agent, qid, body)
            results.setdefault(qid, {"difficulty": diff, "question": body, "answers": {}})
            results[qid]["answers"][friendly] = r
            print(f"[{friendly}] {qid} ({diff}): {r['output_tokens']} out tok", flush=True)
    Path("scripts/.capability_results.json").write_text(json.dumps(results, indent=2))
    print("wrote scripts/.capability_results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
