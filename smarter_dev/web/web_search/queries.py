"""Claude Haiku 5.5 turns a search request into five web search queries.

The prompt and both output checks are the search eval's v6
(``scripts/search_query_eval/prompts/v6.md`` and ``run.py``).
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import UTC
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent
from pydantic_ai import ModelRetry
from pydantic_ai import RunContext

from smarter_dev.shared.request_usage import response_request_tokens
from smarter_dev.shared.usage_meter import UsageMeter
from smarter_dev.shared.usage_meter import run_through

# Claude Haiku 5.5 since 2026-10-17 (#199), replacing GPT-6 Luna.
QUERY_MODEL = os.getenv("WEB_SEARCH_QUERY_MODEL", "claude-haiku-5-5")
REASONING_EFFORT = "medium"
# How many times a run may send junk back to the model before the search fails.
OUTPUT_RETRIES = 2

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "queries.md").read_text(
    encoding="utf-8"
)


class SearchQuery(BaseModel):
    angle: str = Field(
        description="What this query looks for, in a few words of plain language."
    )
    query: str = Field(description="The search query itself, 3 to 7 words.")


class SearchQueries(BaseModel):
    queries: list[SearchQuery] = Field(min_length=5, max_length=5)
    needs_answer: bool = Field(
        description="Whether the user needs a written answer, not just pages to open."
    )


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

    GPT-6 Luna sometimes ended a query with junk like `】【。` or a stray Russian
    word, and the check stays for whichever model writes them.
    Latin is always allowed: English terms are fine in any language."""
    allowed = {"LATIN"} | {_script(c) for c in request}
    return "".join(c for c in query if _script(c) not in allowed | {None})


# site: then whatever follows it up to the next space; empty means "site: x".
SITE_OPERATOR = re.compile(r"site:(\S*)", re.IGNORECASE)


def malformed_site(query: str) -> str | None:
    """Why a query's site: operator won't work, or None if it's fine."""
    for match in SITE_OPERATOR.finditer(query):
        target = match.group(1)
        if not target:
            return "has a space after site:, so the search engine ignores it"
        if "/" in target:
            return f"puts a path in site:{target}; use the bare domain"
    return None


def _check_queries(ctx: RunContext[None], output: SearchQueries) -> SearchQueries:
    for item in output.queries:
        if stray := stray_characters(item.query, ctx.prompt or ""):
            raise ModelRetry(
                f"The query {item.query!r} contains stray characters {stray!r} "
                "that don't belong to the request's language. Rewrite it."
            )
        if problem := malformed_site(item.query):
            raise ModelRetry(f"The query {item.query!r} {problem}. Rewrite it.")
    return output


def _routed_model(name: str):
    """The catalog entry for ``name`` when the shared model router serves it:
    any catalog or Claude id that is not OpenAI's. None for an OpenAI id."""
    from smarter_dev.shared.model_catalog import ModelProvider
    from smarter_dev.shared.model_catalog import catalog_model_for_id

    try:
        model = catalog_model_for_id(name)
    except ValueError:
        return None
    return None if model.provider is ModelProvider.OPENAI else model


def model_for(name: str):
    """The model for ``name``, as in production.

    Claude Haiku 5.5 (the default) and any other routed id go through the
    shared model router, with its key, profile and cache breakpoints. An
    OpenAI id runs on the Responses API; without OPENAI_API_KEY, a LiteLLM
    proxy (LITE_LLM_PROXY_HOST and LITE_LLM_API_KEY, as the search eval uses)
    serves it, so the search runs locally."""
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    if (routed := _routed_model(name)) is not None:
        from smarter_dev.shared.model_router import build_model_for

        return build_model_for(routed)

    host = os.getenv("LITE_LLM_PROXY_HOST", "").rstrip("/")
    if os.getenv("OPENAI_API_KEY") or not host:
        return OpenAIResponsesModel(name)
    if not host.startswith("http"):
        host = f"https://{host}"
    return OpenAIResponsesModel(
        f"openai/{name}",
        provider=OpenAIProvider(
            base_url=f"{host}/v1", api_key=os.environ["LITE_LLM_API_KEY"]
        ),
    )


def settings_for(name: str, effort: str, *, cache_messages: bool):
    """Reasoning ``effort`` in the dialect of ``name``'s provider.

    ``cache_messages`` is as in ``model_router.model_settings_for``: whether a
    Claude request keeps its message cache breakpoint."""
    if (routed := _routed_model(name)) is not None:
        from smarter_dev.shared.model_catalog import ReasoningLevel
        from smarter_dev.shared.model_router import model_settings_for

        return model_settings_for(
            routed, ReasoningLevel(effort), cache_messages=cache_messages
        )
    from pydantic_ai.models.openai import OpenAIResponsesModelSettings

    return OpenAIResponsesModelSettings(openai_reasoning_effort=effort)


def build_query_agent() -> Agent[None, SearchQueries]:
    prompt = SYSTEM_PROMPT.replace("{today}", datetime.now(UTC).date().isoformat())
    agent = Agent(
        model_for(QUERY_MODEL),
        output_type=SearchQueries,
        system_prompt=prompt,
        # One request unless a query is sent back; the system prompt, stable
        # for the day, is the cached prefix.
        model_settings=settings_for(
            QUERY_MODEL, REASONING_EFFORT, cache_messages=False
        ),
        retries=OUTPUT_RETRIES,
    )
    agent.output_validator(_check_queries)
    return agent


async def plan_queries(
    request: str, meter: UsageMeter | None = None
) -> tuple[list[dict], bool, dict]:
    """Five ``{"query", "angle"}`` dicts for ``request``, whether it needs a
    written answer, and the run's usage. *meter* keeps each response's counts
    as it arrives, for the caller to bill if the run raises."""
    agent = build_query_agent()
    result = await agent.run(request, **run_through(meter, agent))
    usage = result.usage() if callable(result.usage) else result.usage
    return [q.model_dump() for q in result.output.queries], result.output.needs_answer, {
        "model": QUERY_MODEL,
        "requests": usage.requests,
        "input_tokens": usage.input_tokens or 0,
        "output_tokens": usage.output_tokens or 0,
        "cache_read_tokens": usage.cache_read_tokens or 0,
        "cache_write_tokens": usage.cache_write_tokens or 0,
        # Each response's own counts: a long-context tier is per request.
        "request_tokens": [
            tokens.as_dict()
            for tokens in response_request_tokens(result.all_messages())
        ],
    }
