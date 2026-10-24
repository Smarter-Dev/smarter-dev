"""The Resources researcher's tools, old against new (#315).

``tests/web/frozen/resources_tools_main.py.frozen`` is main's tool code before the
move into ``resources_corpus``, vendored and pinned. Each case runs it and
the current tools on the same freshly seeded catalog, with the same Jina
answers, and compares everything a run can see: the return value, the tool
events (the chips), the recorded hits, the per-run URL allowlist, what was
stored in Postgres, what was kept in-process, and which URLs went to Jina.
"""

import hashlib
import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from smarter_dev.web import resources_agent as new
from smarter_dev.web import resources_corpus
from tests.web.corpus_fixture import COLD
from tests.web.corpus_fixture import LONG
from tests.web.corpus_fixture import STALE
from tests.web.corpus_fixture import TOOL
from tests.web.corpus_fixture import WARM
from tests.web.corpus_fixture import corpus  # noqa: F401 - fixture
from tests.web.corpus_fixture import drop
from tests.web.corpus_fixture import needs_postgres
from tests.web.corpus_fixture import seed
from tests.web.corpus_fixture import stored

FROZEN = Path(__file__).parent / "frozen" / "resources_tools_main.py.frozen"


def _load_frozen():
    loader = importlib.machinery.SourceFileLoader("frozen_resources_tools", str(FROZEN))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


old = _load_frozen()
FROZEN_SHA256 = "8c6da8822f2de0b3effe9cab10b294e67a4d2d0d384066934841a525ed2e29c8"
WEB = "https://web.example.test/primary-docs"


def test_the_frozen_copy_is_main_unchanged():
    assert hashlib.sha256(FROZEN.read_bytes()).hexdigest() == FROZEN_SHA256


def _jina(answers: dict[str, dict]):
    calls: list[str] = []

    async def fake(client, url, **kwargs):
        calls.append(url)
        return answers.get(url, {"title": "Fetched", "content": f"Body of {url}"})

    return fake, calls


async def _run(module, tool: str, monkeypatch, args, *, seen=(), answers=None):
    """One tool call on one implementation, and everything it left behind."""
    events: list[dict] = []

    async def notify(user, event, **payload):
        events.append({"event": event, **payload})

    fake, calls = _jina(answers or {})
    monkeypatch.setattr(module, "notify_user", notify)
    monkeypatch.setattr(old, "jina_read", fake)
    monkeypatch.setattr(resources_corpus, "jina_read", fake)
    old._READ_CACHE.clear()
    resources_corpus._READ_CACHE.clear()
    deps = new.RunDeps(conversation_id="c", owner_user_id="u")
    deps.seen_urls.update(seen)
    hits = module.begin_run()
    result = await getattr(module, tool)(SimpleNamespace(deps=deps), *args)
    cache = old._READ_CACHE if module is old else resources_corpus._READ_CACHE
    return {
        "result": result,
        "events": events,
        "hits": hits,
        "seen": sorted(deps.seen_urls),
        "memory": {url: body for url, (_, body) in cache.items()},
        "jina": calls,
    }


async def _both(engine, monkeypatch, tool, args, **kwargs):
    """The same call on old then new, each on a freshly seeded catalog, with
    what each stored for the URLs involved."""
    out = []
    for module in (old, new):
        await drop(engine)
        await seed(engine)
        run = await _run(module, tool, monkeypatch, args, **kwargs)
        rows = {}
        for url in (WARM, LONG, COLD, STALE):
            body, fetched = await stored(engine, url)
            rows[url] = (body, fetched is not None)
        run["stored"] = rows
        out.append(run)
    return out


@needs_postgres
@pytest.mark.parametrize(
    "args",
    [
        ("postgres queue",),
        ("queues",),
        ("pgbouncer",),
        ("connection pooling postgres",),
        ("kubernetes",),
        ("   ",),
        ("",),
        ("postgres", 1),
        ("postgres", 50),
    ],
)
async def test_search_resources_matches_main(corpus, monkeypatch, args):  # noqa: F811
    before, after = await _both(corpus, monkeypatch, "search_resources", args)
    assert after == before


@needs_postgres
@pytest.mark.parametrize(
    "url,answers",
    [
        (WARM, None),
        (LONG, None),  # a stored body longer than a fresh read keeps
        (COLD, None),
        (COLD, {COLD: {"title": "Huge", "content": "word " * 5_000}}),
        (STALE, None),
        (COLD, {COLD: {"error": "Jina Reader returned 502", "url": COLD}}),
        (WEB, None),  # seen, not curated: the in-process cache
        ("https://never-searched.test/", None),
        ("", None),
    ],
)
async def test_read_source_matches_main(corpus, monkeypatch, url, answers):  # noqa: F811
    seen = (WARM, LONG, COLD, STALE, TOOL, WEB)
    before, after = await _both(
        corpus, monkeypatch, "read_source", (url,), seen=seen, answers=answers
    )
    assert after == before


@needs_postgres
@pytest.mark.parametrize(
    "url,answers",
    [
        (WEB, None),
        (WEB, {WEB: {"error": "Failed to read URL (ConnectError): x", "url": WEB}}),
        (WARM, None),  # a curated URL the web search also returned
        ("https://never-searched.test/", None),
    ],
)
async def test_read_url_matches_main(corpus, monkeypatch, url, answers):  # noqa: F811
    before, after = await _both(
        corpus,
        monkeypatch,
        "read_url",
        (url,),
        seen=(WEB, WARM),
        answers=answers,
    )
    assert after == before
