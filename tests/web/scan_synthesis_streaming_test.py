"""Tests for Scan synthesis streaming."""

from __future__ import annotations

from pydantic_ai.usage import RunUsage

from smarter_dev.web.scan import agent
from smarter_dev.web.scan.agent import MODES, ResearchOutput, ResearchResult


class _FakeStream:
    def __init__(self, outputs: list[ResearchResult]) -> None:
        self._outputs = outputs
        self._usage = RunUsage(input_tokens=10, output_tokens=20)

    async def stream_output(self, *, debounce_by: float | None = 0.1):
        for output in self._outputs:
            yield output

    def usage(self) -> RunUsage:
        return self._usage


class _FakeRunStream:
    def __init__(self, outputs: list[ResearchResult]) -> None:
        self._stream = _FakeStream(outputs)

    async def __aenter__(self) -> _FakeStream:
        return self._stream

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeSynthesisAgent:
    def __init__(self, outputs: list[ResearchResult]) -> None:
        self.outputs = outputs

    def run_stream(self, *args, **kwargs) -> _FakeRunStream:
        return _FakeRunStream(self.outputs)


async def test_run_synthesis_emits_incremental_response_chunks(monkeypatch):
    outputs = [
        ResearchResult(response="A", sources=[], summary=""),
        ResearchResult(response="AB", sources=[], summary=""),
        ResearchResult(response="ABC", sources=[], summary="done"),
    ]
    monkeypatch.setattr(agent, "_synthesis_agent", _FakeSynthesisAgent(outputs))

    emitted: list[tuple[str, dict]] = []

    async def emit(event_type: str, **payload) -> None:
        emitted.append((event_type, payload))

    result, usage = await agent.run_synthesis(
        "test query",
        ResearchOutput(sources=[], key_insights=[], outline=[]),
        MODES["quick_answer"],
        emit=emit,
    )

    assert result == outputs[-1]
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20
    assert emitted == [
        ("status", {"stage": "synthesizing"}),
        ("response_chunk", {"delta": "A", "seq": 0, "offset": 0}),
        ("response_chunk", {"delta": "B", "seq": 1, "offset": 1}),
        ("response_chunk", {"delta": "C", "seq": 2, "offset": 2}),
    ]
