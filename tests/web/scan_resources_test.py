"""Tests for Scan research source handling."""

from __future__ import annotations

from smarter_dev.web.scan import runner
from smarter_dev.web.scan.agent import (
    ResearchOutput,
    ResearchSource,
    ResourceLink,
    _filter_unread_sources,
    _summarize_content,
)


def test_filter_unread_sources_keeps_only_read_urls() -> None:
    read = ResearchSource(
        url="https://example.com/read",
        title="Read",
        content="read content",
    )
    unread = ResearchSource(
        url="https://example.com/unread",
        title="Unread",
        content="unread content",
    )
    output = ResearchOutput(
        sources=[read, unread],
        key_insights=[],
        outline=[],
    )

    filtered = _filter_unread_sources(output, {"https://example.com/read/"})

    assert filtered.sources == [read]


async def test_enrich_resources_includes_all_sources_and_extra_resources(monkeypatch) -> None:
    async def fake_fetch_og_metadata(_http_client, _url: str) -> dict[str, str]:
        return {}

    monkeypatch.setattr(runner, "fetch_og_metadata", fake_fetch_og_metadata)

    sources = [
        ResearchSource(
            url=f"https://example.com/source-{i}",
            title=f"Source {i}",
            content="content",
            relevance=f"Relevance {i}",
        )
        for i in range(6)
    ]
    output = ResearchOutput(
        sources=sources,
        key_insights=[],
        outline=[],
        resources=[
            ResourceLink(
                url="https://example.com/source-0",
                title="Duplicate source",
                description="Should be deduped",
            ),
            ResourceLink(
                url="https://example.com/extra",
                title="Extra resource",
                description="Extra description",
            ),
        ],
    )
    emitted: list[tuple[str, dict]] = []

    async def emit(event_type: str, **payload) -> None:
        emitted.append((event_type, payload))

    result = await runner._enrich_resources(output, object(), emit)

    assert [r["url"] for r in result] == [
        "https://example.com/source-0",
        "https://example.com/source-1",
        "https://example.com/source-2",
        "https://example.com/source-3",
        "https://example.com/source-4",
        "https://example.com/source-5",
        "https://example.com/extra",
    ]
    assert emitted == [("resources", {"resources": result})]


class _FakeSummaryResult:
    def __init__(self, output: str) -> None:
        self.output = output


class _CapturingSummaryAgent:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run(self, prompt: str, **_kwargs) -> _FakeSummaryResult:
        self.prompts.append(prompt)
        return _FakeSummaryResult(f"summary {len(self.prompts)}")


async def test_summarize_content_covers_full_page_without_truncating(monkeypatch) -> None:
    fake_agent = _CapturingSummaryAgent()
    monkeypatch.setattr("smarter_dev.web.scan.agent._summarize_agent", fake_agent)

    content = ("a" * 24000) + ("b" * 24000) + "TAIL_MARKER"

    summary = await _summarize_content(content, "https://example.com/long")

    assert summary == "summary 1\n\nsummary 2\n\nsummary 3"
    assert len(fake_agent.prompts) == 3
    assert "TAIL_MARKER" in fake_agent.prompts[-1]
