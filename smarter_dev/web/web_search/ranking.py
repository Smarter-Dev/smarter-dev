"""Jev, TypeSafe's classifier, ranks search results against the request.

One call judges every result: its relevance on a 0-3 rubric, whose unrounded
score orders the results, whether it is a trustworthy source, and which one
result would help most. The wording is the search eval's judge prompt v7b
(``scripts/search_query_eval/judge_prompts/v7b.yaml``); Jev sees only each
result's domain and snippet.
"""

from __future__ import annotations

import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from enum import Enum

from pydantic import BaseModel
from pydantic import Field
from pydantic import create_model
from pydantic_ai import Agent

RANK_MODEL = os.getenv("WEB_SEARCH_RANK_MODEL", "jev-1.13.0")
# A score of 0.7 or more counts as relevant: on the eval's labelled sets 0.6
# to 0.8 score the same, 0.5 lets store and job pages through and 0.9 drops
# pages that answer part of the request.
RELEVANT_FROM = 0.7
BOOLEAN_THRESHOLD = 0.5
TIMEOUT_SECONDS = 60
# Jev list price; output tokens are free.
INPUT_PRICE_PER_MILLION_USD = 0.042

INSTRUCTIONS = (
    "Follow the GUIDE. A result is at least level 1 when the snippet gives useful "
    "information for the REQUEST or any part of it, including a page about the same "
    "error or problem. It is level 0 when it only shares words, is navigation or a "
    "store or job listing, only asks for more details, or is outside a time the "
    "REQUEST names."
)
GUIDE = """\
You judge web search results for a search assistant. Today is {weekday} {today}, and this week runs {this_week}. The REQUEST is what the user asked for. Each RESULT is one search result, shown as the site's domain and the snippet the search engine returned. Judge every result on its own, from its domain and snippet only, and judge what the snippet says rather than the words it contains.

- Relevant: the page helps with the REQUEST or with any part of it. A page does not need to answer the whole REQUEST. It is relevant when it covers one step, a likely cause, an error the user is likely to hit along the way, a concept they need to understand, or one of the options they are weighing. A page that shows a function, setting or technique the user could use for part of the REQUEST counts. It is not relevant when it only shares some of the REQUEST's words while being about something else, or when the snippet is mostly menus, prices, ratings, buy buttons, sign-up text, a list of links, a job listing or an empty code editor rather than information. A reply that only asks someone for more details, or that answers a different person's unrelated problem, is not relevant either. When the REQUEST asks about a particular time, such as today, this week or the latest release, an event or release outside that time is not relevant, however close its topic.
- Good quality: the source is trustworthy and substantive for this topic, such as official documentation, a standards body, a reputable publication, an expert Q&A answer or a well-known practitioner. Content farms, thin SEO listicles, scraped copies, spam and pages selling something unrelated are not good quality."""
RELEVANCE_QUESTION = "How useful is {where} for the REQUEST?"
LEVELS = (
    "not useful: only shares words, is navigation, a store or job listing, only asks "
    "for details, or is outside the REQUEST's time",
    "gives useful information for one part of the REQUEST",
    "answers part of the REQUEST well",
    "answers the REQUEST directly",
)
BEST_QUESTION = "Which RESULT would help the user most with the REQUEST?"
QUALITY_QUESTION = "Is {where} a trustworthy, substantive source for this topic?"


def guide(today=None) -> str:
    today = today or datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return GUIDE.format(
        weekday=f"{today:%A}",
        today=today.isoformat(),
        this_week=f"Monday {monday.isoformat()} to Sunday {sunday.isoformat()}",
    )


def material(request: str, results: list[dict], today=None) -> str:
    """The text Jev judges: the guide, the request, then every numbered result.

    TypeSafe bills the instructions once per question but the material once
    per call, so the rules live here."""
    lines = [f"GUIDE:\n{guide(today)}", "", f"REQUEST:\n{request.strip()}", "", "RESULTS:"]
    for n, result in enumerate(results, 1):
        lines.append(f"[{n}] {result['domain']}: {result['snippet']}")
    return "\n".join(lines)


class _Judgments(BaseModel):
    """Which search RESULTS help with the REQUEST, and which one helps most."""


def judgment_model(results: list[dict]) -> type[BaseModel]:
    best = Enum("Best", {f"r{n}": f"RESULT [{n}]" for n in range(1, len(results) + 1)})
    fields: dict = {"best": (best, Field(description=BEST_QUESTION))}
    rubric = [{"const": level, "description": text} for level, text in enumerate(LEVELS)]
    for n, result in enumerate(results, 1):
        where = f"RESULT [{n}] ({result['domain']})"
        fields[f"result_{n}_relevance"] = (
            int,
            Field(
                description=RELEVANCE_QUESTION.format(where=where),
                json_schema_extra={"anyOf": rubric},
            ),
        )
        fields[f"result_{n}_quality"] = (
            bool,
            Field(description=QUALITY_QUESTION.format(where=where)),
        )
    return create_model("ResultJudgments", __base__=_Judgments, **fields)


def build_model():
    import httpx2
    from pydantic_ai.models.typesafe import TypeSafeModel
    from pydantic_ai.providers.typesafe import TypeSafeProvider

    # As in smarter_dev.bot.proactive.models: stay on gzip, since httpx2
    # can't decode TypeSafe's brotli responses.
    http_client = httpx2.AsyncClient(headers={"Accept-Encoding": "gzip, deflate"})
    return TypeSafeModel(RANK_MODEL, provider=TypeSafeProvider(http_client=http_client))


async def rank(request: str, results: list[dict]) -> tuple[list[dict], dict]:
    """Jev's judgment of each result, in the order given, and the call's usage.

    Each judgment is ``{"score", "level", "relevant", "quality", "best_probability"}``;
    exactly one result also has ``"best": True``."""
    agent = Agent(build_model(), output_type=judgment_model(results), retries=0)
    run = await agent.run(
        material(request, results),
        instructions=INSTRUCTIONS,
        model_settings={
            "timeout": TIMEOUT_SECONDS,
            "typesafe_boolean_threshold": BOOLEAN_THRESHOLD,
        },
    )
    verdicts = run.output.model_dump()
    details = run.response.provider_details or {}
    scores = details.get("scores") or {}
    best_probabilities = (details.get("probabilities") or {}).get("best") or {}
    best = int(verdicts["best"].value.strip("RESULT []"))
    judgments = []
    for n in range(1, len(results) + 1):
        level = verdicts[f"result_{n}_relevance"]
        score = float(scores.get(f"result_{n}_relevance", level))
        judgments.append(
            {
                "score": round(score, 3),
                "level": level,
                "relevant": score >= RELEVANT_FROM,
                "quality": verdicts[f"result_{n}_quality"],
                "best_probability": round(float(best_probabilities.get(f"RESULT [{n}]", 0.0)), 3),
                "best": n == best,
            }
        )
    usage = run.usage() if callable(run.usage) else run.usage
    input_tokens = usage.input_tokens or 0
    return judgments, {
        "model": run.response.model_name or RANK_MODEL,
        "input_tokens": input_tokens,
        "cost_usd": round(input_tokens * INPUT_PRICE_PER_MILLION_USD / 1e6, 6),
    }
