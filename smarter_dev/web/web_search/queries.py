"""Luna turns a search request into five web search queries.

The prompt and both output checks are the search eval's v6
(``scripts/search_query_eval/prompts/v6.md`` and ``run.py``).
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import UTC
from datetime import datetime

from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent
from pydantic_ai import ModelRetry
from pydantic_ai import RunContext

QUERY_MODEL = os.getenv("WEB_SEARCH_QUERY_MODEL", "gpt-6-luna")
REASONING_EFFORT = "medium"
# How many times a run may send junk back to Luna before the search fails.
OUTPUT_RETRIES = 2

SYSTEM_PROMPT = """\
You write web search queries for a user's request.

Return 5 queries. Each one is what an experienced engineer would actually type into Google: usually 3 to 5 words, never more than 7 (an exact error message in quotes may be longer). Search engines do not reward extra keywords; every added word narrows or muddies the results.

Write each query as a phrase that the answering page would contain, such as an error message, a page title, a setting name or a short question. Do not string related terms together.

Use the precise vocabulary of the field. When the request involves a specific technical term, error code, API, command or setting name, or you know the one the answering page will use, at least 2 queries must contain it.

The 5 queries must cover different ground. The test: if two of your queries would bring up mostly the same results, replace one. Queries must differ in the question their page answers, not just in which words they use. When the request is narrow, such as a single error message, widen to a different sub-question (what it means, why it happens, how to fix it, a trade-off, a known pitfall) rather than rephrase; when few sub-questions fit (for example "what's happening this week"), split by subject area (AI, security, developer tools, consumer hardware), audience or region instead.

Rules:
- Aiming a query at the authoritative source or at a Q&A site is often the best way to cover new ground. Use `site:` for that in at most 2 queries, and only with a bare domain (`site:postgresql.org`), never a path.
- No filler words such as "official", "docs", "guide", "explanation", "best practices", "troubleshooting".
- Do not put guesses or answers in a query. Search for what the user needs to learn, not what you think the answer is.
- Add a year or dates only when the request is about recent or current things. Today's date is {today}.
- Write in the user's language.

Bad (a pile of terms): `nginx 502 bad gateway upstream reverse proxy error causes troubleshooting fix configuration`
Bad (terms strung together): `nginx upstream timeout proxy buffer keepalive 502`
Bad (the same query reworded): `nginx 502 error`, `nginx 502 bad gateway fix`, `why nginx returns 502`
Good: `nginx 502 bad gateway upstream`, `nginx proxy_read_timeout`, `nginx 502 only under load`, `site:nginx.org upstream keepalive`

Also decide needs_answer. After the searches, an assistant can read the pages and write the user an answer above the results. Set it to true when the user wants something answered, explained, solved or compared, such as how to do something, why something happens, which option to choose, or whether something is true or happening. Set it to false when they want pages to open rather than an answer: a particular site, a download, the docs or product page for a named thing, or a list to browse."""


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

    Luna sometimes ends a query with junk like `】【。` or a stray Russian word.
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


def model_for(name: str):
    """An OpenAI model on the Responses API, as in production.

    Without OPENAI_API_KEY, a LiteLLM proxy (LITE_LLM_PROXY_HOST and
    LITE_LLM_API_KEY, as the search eval uses) serves it, so the search runs
    locally."""
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

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


def build_query_agent() -> Agent[None, SearchQueries]:
    from pydantic_ai.models.openai import OpenAIResponsesModelSettings

    prompt = SYSTEM_PROMPT.replace("{today}", datetime.now(UTC).date().isoformat())
    agent = Agent(
        model_for(QUERY_MODEL),
        output_type=SearchQueries,
        system_prompt=prompt,
        model_settings=OpenAIResponsesModelSettings(
            openai_reasoning_effort=REASONING_EFFORT
        ),
        retries=OUTPUT_RETRIES,
    )
    agent.output_validator(_check_queries)
    return agent


async def plan_queries(request: str) -> tuple[list[dict], bool, dict]:
    """Five ``{"query", "angle"}`` dicts for ``request``, whether it needs a
    written answer, and the run's usage."""
    result = await build_query_agent().run(request)
    usage = result.usage() if callable(result.usage) else result.usage
    return [q.model_dump() for q in result.output.queries], result.output.needs_answer, {
        "model": QUERY_MODEL,
        "requests": usage.requests,
        "input_tokens": usage.input_tokens or 0,
        "output_tokens": usage.output_tokens or 0,
        "cache_read_tokens": usage.cache_read_tokens or 0,
    }
