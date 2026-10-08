"""The dashboard's web search: query checks, the pipeline's stages and events,
and the request validation the controller applies.

The pipeline runs against the sqlite test session with Luna, Brave and Jev
replaced, so these tests pin what is saved and announced at each step, not
what the models say."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from litestar.exceptions import HTTPException
from sqlalchemy import select

from smarter_dev.web import dashboard_controller
from smarter_dev.web.llm_pricing import calc_cost
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.usage_invoice import monthly_invoice
from smarter_dev.web.web_search import answer
from smarter_dev.web.web_search import brave
from smarter_dev.web.web_search import metering
from smarter_dev.web.web_search import pipeline
from smarter_dev.web.web_search import queries
from smarter_dev.web.web_search import ranking
from smarter_dev.web.web_search.snapshot import snapshot


def test_malformed_site_operators_are_named():
    assert queries.malformed_site("site: example.com foo")
    assert "path" in queries.malformed_site("site:example.com/docs foo")
    assert queries.malformed_site("site:postgresql.org count") is None


def test_stray_characters_allow_latin_and_the_requests_script():
    assert queries.stray_characters("docker size】【", "docker image") == "】【"
    assert queries.stray_characters("tamaño imagen docker", "¿por qué?") == ""


def test_brave_snippets_lose_markup_and_www():
    assert brave.clean_snippet("<strong>COUNT</strong> is &amp; slow") == "COUNT is & slow"
    assert brave.domain_of("https://www.postgresql.org/docs/") == "postgresql.org"


async def test_httpx_request_lines_for_brave_are_dropped(caplog, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    monkeypatch.setattr(brave, "MIN_INTERVAL_SECONDS", 0)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(transport=transport) as client:
        await brave.search(client, "secret user words")
        await client.get("https://example.org/other")
    lines = [record.getMessage() for record in caplog.records if record.name == "httpx"]
    assert len(lines) == 1 and "example.org" in lines[0]
    assert "secret" not in caplog.text


def test_ranking_material_numbers_results_under_the_guide():
    text = ranking.material(
        "slow count",
        [{"domain": "a.org", "snippet": "one"}, {"domain": "b.org", "snippet": "two"}],
    )
    assert text.startswith("GUIDE:\nYou judge web search results")
    assert "REQUEST:\nslow count" in text
    assert text.endswith("[1] a.org: one\n[2] b.org: two")


def test_ranking_schema_has_a_best_pick_and_a_rubric_per_result():
    model = ranking.judgment_model([{"domain": "a.org"}, {"domain": "b.org"}])
    schema = model.model_json_schema()
    assert set(model.model_fields) == {
        "best",
        "result_1_relevance",
        "result_1_quality",
        "result_1_local",
        "result_2_relevance",
        "result_2_quality",
        "result_2_local",
    }
    levels = schema["properties"]["result_1_relevance"]["anyOf"]
    assert [level["const"] for level in levels] == [0, 1, 2, 3]


def _hit(url: str, snippet: str = "text") -> dict:
    return {
        "title": url,
        "url": url,
        "domain": brave.domain_of(url),
        "snippet": snippet,
        "favicon": "",
        "age": "",
    }


@pytest.fixture
def search_env(db_session, monkeypatch):
    """Route the pipeline at the test session and record every event."""

    @asynccontextmanager
    async def session_context():
        yield db_session

    monkeypatch.setattr(pipeline, "get_db_session_context", session_context)
    monkeypatch.setattr(metering, "get_db_session_context", session_context)
    events: list[dict] = []

    async def notify(owner, state):
        events.append(state)

    async def plan(request):
        return [{"query": f"query {n}", "angle": f"angle {n}"} for n in range(5)], False, {
            "model": "gpt-6-luna",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_tokens": 0,
        }

    hits = {
        "query 0": [_hit("https://a.org"), _hit("https://b.org")],
        "query 1": [_hit("https://b.org", "second snippet"), _hit("https://c.org")],
        "query 2": [],
        "query 3": [_hit("https://d.org")],
        "query 4": [_hit("https://e.org")],
    }

    async def search(client, query):
        return hits[query]

    async def rank(request, results):
        # Scores follow the reverse of Brave's order, so ranking must reorder.
        judgments = [
            {
                "score": float(n),
                "level": min(3, n),
                "relevant": n >= 1,
                "quality": False,
                "best_probability": 0.0,
                "best": n == len(results) - 1,
            }
            for n in range(len(results))
        ]
        return judgments, {"model": "jev-1.13.0", "input_tokens": 10_000, "cost_usd": 0.00042}

    monkeypatch.setattr(queries, "plan_queries", plan)
    monkeypatch.setattr(brave, "search", search)
    monkeypatch.setattr(ranking, "rank", rank)
    return {"session": db_session, "events": events, "notify": notify, "hits": hits}


async def _new_run(session) -> WebSearchRun:
    run = WebSearchRun(
        owner_user_id=uuid4(),
        submission_key=uuid4().hex,
        request="why is my thing slow",
        status="queued",
        queries=[],
        results=[],
        ranked=False,
        usage={},
        version=0,
        attempt_count=0,
    )
    session.add(run)
    await session.commit()
    return run


async def test_search_saves_and_announces_every_stage_in_order(search_env):
    run = await _new_run(search_env["session"])

    assert await pipeline.run_search(run.id, search_env["notify"]) == "complete"

    events = search_env["events"]
    statuses = [event["status"] for event in events]
    assert statuses[0] == "planning"
    assert statuses[-2:] == ["ranking", "complete"]
    # Each query is announced as it starts and again when it finishes.
    assert statuses.count("searching") == 1 + 2 * 5
    assert [event["version"] for event in events] == list(range(1, len(events) + 1))
    assert all("hits" not in query for event in events for query in event["queries"])

    final = events[-1]
    assert final == snapshot(await search_env["session"].get(WebSearchRun, run.id))
    assert final["ranked"] is True and final["active"] is False
    assert [q["count"] for q in final["queries"]] == [2, 2, 0, 1, 1]
    assert final["queries"][0]["domains"] == ["a.org", "b.org"]


async def test_duplicate_pages_merge_and_results_sort_by_score(search_env):
    run = await _new_run(search_env["session"])
    await pipeline.run_search(run.id, search_env["notify"])

    final = search_env["events"][-1]
    urls = [result["url"] for result in final["results"]]
    assert urls == ["https://e.org", "https://d.org", "https://c.org", "https://b.org", "https://a.org"]
    b_org = next(result for result in final["results"] if result["url"] == "https://b.org")
    assert b_org["queries"] == [0, 1]
    assert b_org["snippet"] == "text"
    assert final["results"][0]["best"] is True
    # Before Jev answers, the browser gets the merged results in search order.
    assert [r["url"] for r in search_env["events"][-2]["results"]][:2] == ["https://a.org", "https://b.org"]


async def test_the_top_pick_leads_then_the_rest_by_score(search_env, monkeypatch):
    async def rank(request, results):
        # Jev picks the third-best score.
        judgments = [
            {
                "score": float(n),
                "level": min(3, n),
                "relevant": n >= 1,
                "quality": False,
                "best_probability": 0.0,
                "best": n == 2,
            }
            for n in range(len(results))
        ]
        return judgments, {"model": "jev-1.13.0", "input_tokens": 10_000, "cost_usd": 0.00042}

    monkeypatch.setattr(ranking, "rank", rank)
    run = await _new_run(search_env["session"])
    await pipeline.run_search(run.id, search_env["notify"])

    final = search_env["events"][-1]
    assert [r["score"] for r in final["results"]] == [2.0, 4.0, 3.0, 1.0, 0.0]
    assert final["results"][0]["best"] is True


async def test_a_failed_ranking_still_completes_in_search_order(search_env, monkeypatch):
    async def broken(request, results):
        raise TimeoutError

    monkeypatch.setattr(ranking, "rank", broken)
    run = await _new_run(search_env["session"])

    assert await pipeline.run_search(run.id, search_env["notify"]) == "complete"
    final = search_env["events"][-1]
    assert final["ranked"] is False
    assert final["results"][0]["url"] == "https://a.org"


async def test_no_results_ends_the_search_with_an_error(search_env, monkeypatch):
    async def nothing(client, query):
        raise brave.BraveError("Brave answered 500")

    monkeypatch.setattr(brave, "search", nothing)
    run = await _new_run(search_env["session"])

    assert await pipeline.run_search(run.id, search_env["notify"]) == "error"
    final = search_env["events"][-1]
    assert final["status"] == "error" and final["error"] == "The searches found nothing."
    assert {q["status"] for q in final["queries"]} == {"failed"}


async def test_a_luna_failure_ends_the_search_with_an_error(search_env, monkeypatch):
    async def broken(request):
        raise RuntimeError("proxy down")

    monkeypatch.setattr(queries, "plan_queries", broken)
    run = await _new_run(search_env["session"])

    assert await pipeline.run_search(run.id, search_env["notify"]) == "error"
    assert search_env["events"][-1]["error"] == "Couldn't plan the searches (RuntimeError)."


async def test_a_retried_search_starts_over(search_env):
    run = await _new_run(search_env["session"])
    await pipeline.run_search(run.id, search_env["notify"])
    await pipeline.run_search(run.id, search_env["notify"])

    saved = await search_env["session"].get(WebSearchRun, run.id)
    assert saved.attempt_count == 2
    assert len(saved.results) == 5


async def _ledger(session) -> dict[str, UsageCostRow]:
    rows = (await session.scalars(select(UsageCostRow))).all()
    return {row.operation_type: row for row in rows}


async def test_each_paid_step_reaches_the_invoice(search_env):
    run = await _new_run(search_env["session"])
    await pipeline.run_search(run.id, search_env["notify"])

    ledger = await _ledger(search_env["session"])
    assert set(ledger) == {"web_search_queries", "web_search_brave", "web_search_ranking"}
    assert {row.product_mode for row in ledger.values()} == {"search"}
    assert {row.user_id for row in ledger.values()} == {run.owner_user_id}
    # GPT-6 Luna: $0.10 in and $0.50 out per million tokens.
    assert ledger["web_search_queries"].cost_usd == Decimal("0.6")
    # Every query was answered, including query 2, which found nothing.
    assert ledger["web_search_brave"].details["requests"] == 5
    assert ledger["web_search_brave"].cost_usd == 5 * Decimal(brave.PRICE_PER_REQUEST_USD)
    assert ledger["web_search_ranking"].provider_key == "typesafe"
    assert ledger["web_search_ranking"].cost_usd == Decimal("0.00042")

    lines = await monthly_invoice(search_env["session"], datetime.now(UTC).strftime("%Y-%m"))
    by_source = {line.source: line for line in lines}
    assert by_source["search:web_search_brave"].provider_label == "Brave"
    assert by_source["search:web_search_ranking"].provider_label == "TypeSafe"
    assert by_source["search:web_search_queries"].provider_label == "OpenAI"


async def test_a_retry_bills_again_but_a_repeat_write_does_not(search_env):
    run = await _new_run(search_env["session"])
    await pipeline.run_search(run.id, search_env["notify"])
    await pipeline.run_search(run.id, search_env["notify"])
    rows = (await search_env["session"].scalars(select(UsageCostRow))).all()
    assert len(rows) == 6

    saved = await search_env["session"].get(WebSearchRun, run.id)
    await metering.record(run.id, lambda row: [metering.brave_row(row, 5)])
    rows = (await search_env["session"].scalars(select(UsageCostRow))).all()
    assert len(rows) == 6 and saved.attempt_count == 2


async def test_failed_brave_queries_and_a_failed_ranking_cost_nothing(search_env, monkeypatch):
    async def flaky(client, query):
        if query == "query 0":
            raise brave.BraveError("Brave answered 500")
        return search_env["hits"][query]

    async def broken(request, results):
        raise TimeoutError

    monkeypatch.setattr(brave, "search", flaky)
    monkeypatch.setattr(ranking, "rank", broken)
    run = await _new_run(search_env["session"])
    await pipeline.run_search(run.id, search_env["notify"])

    ledger = await _ledger(search_env["session"])
    assert set(ledger) == {"web_search_queries", "web_search_brave"}
    assert ledger["web_search_brave"].details["requests"] == 4


def test_requests_are_trimmed_and_bounded():
    assert dashboard_controller._validate_request("  slow   docker\nbuild ") == "slow docker build"
    with pytest.raises(HTTPException):
        dashboard_controller._validate_request("hi")
    with pytest.raises(HTTPException):
        dashboard_controller._validate_request("x" * 501)


def test_submission_keys_must_look_like_keys():
    key = str(uuid4())
    assert dashboard_controller._validate_submission_key(key) == key
    for bad in ("short", "x" * 65, "has spaces in it", "<script>alert</script>"):
        with pytest.raises(HTTPException):
            dashboard_controller._validate_submission_key(bad)


def test_the_client_build_changes_with_the_dashboard_script(tmp_path, monkeypatch):
    build = dashboard_controller.CLIENT_BUILD
    assert len(build) == 12
    for name in dashboard_controller.CLIENT_FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes((dashboard_controller.THEME_DIR / name).read_bytes())
    monkeypatch.setattr(dashboard_controller, "THEME_DIR", tmp_path)
    assert dashboard_controller._client_build() == build
    (tmp_path / "static/js/dashboard.js").write_text("// next deploy")
    assert dashboard_controller._client_build() not in {"", build}


async def test_a_user_gets_six_searches_a_minute(db_session):
    user_id = uuid4()
    for _ in range(dashboard_controller.PER_MINUTE_LIMIT):
        await dashboard_controller._enforce_limits(db_session, user_id)
    with pytest.raises(HTTPException) as caught:
        await dashboard_controller._enforce_limits(db_session, user_id)
    assert caught.value.status_code == 429


def _verdicts(levels, local, best):
    verdicts = {"best": type("Pick", (), {"value": f"RESULT [{best}]"})()}
    for n, (level, is_local) in enumerate(zip(levels, local, strict=True), 1):
        verdicts[f"result_{n}_relevance"] = level
        verdicts[f"result_{n}_quality"] = False
        verdicts[f"result_{n}_local"] = is_local
    return verdicts


def test_a_local_page_drops_a_level_and_loses_the_top_pick():
    details = {
        "scores": {"result_1_relevance": 2.4, "result_2_relevance": 2.6, "result_3_relevance": 1.2},
        "probabilities": {"best": {"RESULT [1]": 0.2, "RESULT [2]": 0.7, "RESULT [3]": 0.1}},
    }
    judged = ranking.judge(_verdicts([2, 3, 1], [False, True, False], best=2), details, 3)
    assert judged[1]["score"] == pytest.approx(1.6) and judged[1]["local"] is True
    # Jev picked the Tampa page; the pick moves to the likeliest non-local one.
    assert [j["best"] for j in judged] == [True, False, False]


def test_when_every_result_is_local_jevs_pick_stands():
    details = {"scores": {}, "probabilities": {"best": {"RESULT [2]": 0.9}}}
    judged = ranking.judge(_verdicts([1, 2], [True, True], best=2), details, 2)
    assert [j["best"] for j in judged] == [False, True]
    assert judged[1]["score"] == 1.0


def test_citations_become_links_to_the_cited_results():
    results = [
        {"url": "https://a.org/x", "domain": "a.org"},
        {"url": "https://en.wikipedia.org/wiki/Foo_(bar)", "domain": "wikipedia.org"},
    ]
    html = answer.render("Use `git revert` [1][2]. Also [2, 1]. Not [9].\n\n[1]: https://evil.org", results)
    assert html.count('class="ud-cite"') == 5
    assert 'href="https://en.wikipedia.org/wiki/Foo_(bar)" title="wikipedia.org">2</a>' in html
    assert "[9]" in html and "evil.org" in html and 'href="https://evil.org"' not in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_answer_html_never_carries_raw_markup_or_script_links():
    html = answer.render("<img src=x onerror=alert(1)> [x](javascript:alert(1))", [])
    assert "<img" not in html and 'href="javascript' not in html


@pytest.fixture
def answering_env(search_env, monkeypatch):
    async def plan(request):
        return [{"query": f"query {n}", "angle": f"angle {n}"} for n in range(5)], True, {
            "model": "gpt-6-luna",
            "input_tokens": 1_000,
            "output_tokens": 100,
        }

    async def write(request, results, on_progress):
        reads = [answer.Read(number=1, domain=results[0]["domain"], status="done", tokens=2_000_000)]
        await on_progress(reads, "")
        await on_progress(reads, "Partly written [1]")
        return "The answer [1].", reads, {
            "model": "gpt-6-luna",
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "reader_tokens": 2_000_000,
        }

    monkeypatch.setattr(queries, "plan_queries", plan)
    monkeypatch.setattr(answer, "write_answer", write)
    return search_env


async def test_the_answer_follows_the_ranked_results(answering_env):
    run = await _new_run(answering_env["session"])
    assert await pipeline.run_search(run.id, answering_env["notify"]) == "complete"

    events = answering_env["events"]
    statuses = [event["status"] for event in events]
    first_answering = statuses.index("answering")
    # The ranked results go out before any of the answer.
    assert events[first_answering]["ranked"] is True
    assert events[first_answering]["answer"] == {"status": "reading", "reads": [], "html": "", "error": None}
    assert any(event["answer"] and event["answer"]["status"] == "writing" for event in events)

    final = events[-1]
    assert final["status"] == "complete" and final["active"] is False
    assert final["answer"]["status"] == "done"
    assert final["answer"]["reads"] == [{"number": 1, "domain": "e.org", "status": "done"}]
    assert 'href="https://e.org"' in final["answer"]["html"]
    assert "markdown" not in final["answer"]

    ledger = await _ledger(answering_env["session"])
    assert ledger["web_search_answer"].cost_usd == Decimal("0.1")
    assert ledger["web_search_reader"].provider_key == "jina"
    assert ledger["web_search_reader"].cost_usd == 2 * Decimal(answer.JINA_PRICE_PER_MILLION_TOKENS_USD)


async def test_a_failed_answer_keeps_the_results(answering_env, monkeypatch):
    async def broken(request, results, on_progress):
        raise TimeoutError

    monkeypatch.setattr(answer, "write_answer", broken)
    run = await _new_run(answering_env["session"])
    assert await pipeline.run_search(run.id, answering_env["notify"]) == "complete"

    final = answering_env["events"][-1]
    assert final["answer"]["status"] == "failed"
    assert final["answer"]["error"].startswith("Couldn't write an answer")
    assert len(final["results"]) == 5 and final["ranked"] is True
    assert "web_search_answer" not in await _ledger(answering_env["session"])


async def test_searches_that_need_no_answer_never_start_one(search_env):
    run = await _new_run(search_env["session"])
    await pipeline.run_search(run.id, search_env["notify"])
    assert "answering" not in [event["status"] for event in search_env["events"]]
    assert search_env["events"][-1]["answer"] is None


@pytest.mark.parametrize("input_tokens", [0, 1, 999, 10_000, 924_157, 3_141_593])
async def test_ranking_cost_is_unchanged_by_reading_the_rate_from_llm_pricing(
    monkeypatch, input_tokens
):
    # Jev's rate moved into llm_pricing (#101); the ledger figure must not move.
    class FakeAgent:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            verdicts = _verdicts([2], [False], best=1)
            return SimpleNamespace(
                output=SimpleNamespace(model_dump=lambda: verdicts),
                response=SimpleNamespace(provider_details={}, model_name="jev-1.13.0"),
                usage=SimpleNamespace(input_tokens=input_tokens),
            )

    monkeypatch.setattr(ranking, "Agent", FakeAgent)
    monkeypatch.setattr(ranking, "build_model", lambda: None)
    _, usage = await ranking.rank("q", [{"domain": "a.org", "snippet": "s"}])
    assert usage["cost_usd"] == round(input_tokens * 0.042 / 1e6, 6)
    assert usage["cost_usd"] == pytest.approx(
        float(calc_cost(input_tokens, 0, "typesafe:jev-1.13.0")), abs=5e-7
    )
