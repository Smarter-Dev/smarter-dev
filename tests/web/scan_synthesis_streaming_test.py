"""Tests for Scan synthesis streaming."""

from __future__ import annotations

from pydantic_ai.usage import RunUsage

from smarter_dev.web.scan import agent
from smarter_dev.web.scan.agent import MODES, ResearchOutput, Source, SynthesisMetadata


class _FakeStream:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks
        self._usage = RunUsage(input_tokens=10, output_tokens=20)

    async def stream_text(self, *, delta: bool = False, debounce_by: float | None = 0.1):
        for chunk in self._chunks:
            yield chunk

    def usage(self) -> RunUsage:
        return self._usage


class _FakeRunStream:
    def __init__(self, chunks: list[str]) -> None:
        self._stream = _FakeStream(chunks)

    async def __aenter__(self) -> _FakeStream:
        return self._stream

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeTextAgent:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    def run_stream(self, *args, **kwargs) -> _FakeRunStream:
        return _FakeRunStream(self.chunks)


class _FakeResult:
    def __init__(self, output: SynthesisMetadata) -> None:
        self.output = output

    def usage(self) -> RunUsage:
        return RunUsage(input_tokens=3, output_tokens=4)


class _FakeMetadataAgent:
    def __init__(self, output: SynthesisMetadata) -> None:
        self.output = output

    async def run(self, *args, **kwargs) -> _FakeResult:
        return _FakeResult(self.output)


async def test_run_synthesis_emits_incremental_response_chunks(monkeypatch):
    source = Source(url="https://example.com", title="Example", cited=True)
    metadata = SynthesisMetadata(sources=[source], summary="done")
    monkeypatch.setattr(agent, "_synthesis_text_agent", _FakeTextAgent(["A", "B", "C"]))
    monkeypatch.setattr(agent, "_synthesis_metadata_agent", _FakeMetadataAgent(metadata))

    emitted: list[tuple[str, dict]] = []

    async def emit(event_type: str, **payload) -> None:
        emitted.append((event_type, payload))

    result, usage = await agent.run_synthesis(
        "test query",
        ResearchOutput(sources=[], key_insights=[], outline=[]),
        MODES["quick_answer"],
        emit=emit,
    )

    assert result.response == "ABC"
    assert result.sources == [source]
    assert result.summary == "done"
    assert usage.input_tokens == 13
    assert usage.output_tokens == 24
    assert emitted == [
        ("status", {"stage": "synthesizing"}),
        ("response_chunk", {"delta": "A", "seq": 0, "offset": 0}),
        ("response_chunk", {"delta": "B", "seq": 1, "offset": 1}),
        ("response_chunk", {"delta": "C", "seq": 2, "offset": 2}),
    ]
