"""The dashboard's web search: query checks, the pipeline's stages and events,
and the request validation the controller applies.

The pipeline runs against the sqlite test session with Luna, Brave and Jev
replaced, so these tests pin what is saved and announced at each step, not
what the models say."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from litestar.exceptions import HTTPException

from smarter_dev.web import dashboard_controller
from smarter_dev.web.models import WebSearchRun
from smarter_dev.web.web_search import brave
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
        "result_2_relevance",
        "result_2_quality",
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
    events: list[dict] = []

    async def notify(owner, state):
        events.append(state)

    async def plan(request):
        return [{"query": f"query {n}", "angle": f"angle {n}"} for n in range(5)], {
            "model": "gpt-6-luna"
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
        return judgments, {"model": "jev-1.13.0", "input_tokens": 10, "cost_usd": 0.0}

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


async def test_a_user_gets_six_searches_a_minute(db_session):
    user_id = uuid4()
    for _ in range(dashboard_controller.PER_MINUTE_LIMIT):
        await dashboard_controller._enforce_limits(db_session, user_id)
    with pytest.raises(HTTPException) as caught:
        await dashboard_controller._enforce_limits(db_session, user_id)
    assert caught.value.status_code == 429
