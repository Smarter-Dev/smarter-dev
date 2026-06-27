"""Tests for the Monty handler runtime and its rails (offline)."""

from __future__ import annotations

from dataclasses import dataclass, field

from smarter_dev.web.handler_budget import CapExceeded, HandlerBudget
from smarter_dev.web.handler_runtime import run_handler_script


@dataclass
class _FakeEmitter:
    messages: list[tuple[str, str]] = field(default_factory=list)
    reactions: list[tuple[str, str, str]] = field(default_factory=list)

    async def create_message(self, channel_id: str, content: str) -> str:
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        self.reactions.append((channel_id, message_id, emoji))


@dataclass
class _StubLimiter:
    allow: bool = True
    calls: list = field(default_factory=list)

    async def hit(self, key: str, limit: int) -> bool:
        self.calls.append((key, limit))
        return self.allow


async def _run(script, *, budget=None, emitter=None, limiter=None, agent_runner=None):
    emitter = emitter or _FakeEmitter()
    limiter = limiter or _StubLimiter()
    kwargs = {}
    if agent_runner is not None:
        kwargs["agent_runner"] = agent_runner
    result = await run_handler_script(
        script,
        {"trigger_type": "message", "message_content": "hi"},
        channel_id="C1",
        guild_id="G1",
        emitter=emitter,
        limiter=limiter,
        budget=budget,
        **kwargs,
    )
    return result, emitter, limiter


async def test_happy_path_single_emitter():
    script = (
        'name = context["message_content"]\n'
        'await send_message(f"hello {name}")\n'
        'await send_message("again")\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok"
    assert [c for _, c in emitter.messages] == ["hello hi", "again"]
    assert result.usage["messages_sent"] == 2


async def test_send_then_react_uses_returned_message_id():
    script = (
        'mid = await send_message("hi")\n'
        'await add_reaction(mid, "🎉")\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok"
    assert emitter.reactions == [("C1", "msg1", "🎉")]


async def test_message_cap_breaches_mid_flight():
    script = "for i in range(5):\n    await send_message(str(i))\n"
    result, emitter, _ = await _run(script, budget=HandlerBudget(max_messages=3))
    assert result.outcome == "cap_exceeded"
    assert result.cap == "messages"
    # Two-then-fail: the first three went out before the breach, no rollback.
    assert len(emitter.messages) == 3
    assert result.usage["messages_sent"] == 3


async def test_agent_context_byte_cap_blocks_before_running_agent():
    ran = []

    async def runner(prompt, has_tools, budget):
        ran.append(prompt)
        return "ok"

    script = 'await spawn_agent("x" * 40000, has_tools=True)\n'
    result, _, _ = await _run(
        script, budget=HandlerBudget(max_agent_context_bytes=32 * 1024), agent_runner=runner
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "agent_context_bytes"
    assert ran == []  # agent never invoked


async def test_agent_call_cap():
    calls = []

    async def runner(prompt, has_tools, budget):
        calls.append(prompt)
        return "r"

    script = (
        'await spawn_agent("a")\n'
        'await spawn_agent("b")\n'
        'await spawn_agent("c")\n'
    )
    result, _, _ = await _run(
        script, budget=HandlerBudget(max_agent_calls=2), agent_runner=runner
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "agent_calls"
    assert calls == ["a", "b"]


async def test_search_read_budget_shared_across_agents():
    # Each agent reads two pages; with max_web_reads=3 the second agent's second
    # read breaches the shared pool — proving metering is shared, not per-agent.
    async def runner(prompt, has_tools, budget):
        budget.spend_web_read()
        budget.spend_web_read()
        return "done"

    script = 'await spawn_agent("a", has_tools=True)\nawait spawn_agent("b", has_tools=True)\n'
    result, _, _ = await _run(
        script,
        budget=HandlerBudget(max_agent_calls=2, max_web_reads=3),
        agent_runner=runner,
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "web_reads"


async def test_channel_window_denial_fails_loud():
    script = 'await send_message("hi")\n'
    result, emitter, _ = await _run(script, limiter=_StubLimiter(allow=False))
    assert result.outcome == "cap_exceeded"
    assert result.cap == "channel_messages_per_min"
    assert emitter.messages == []  # window denied before the REST emit


async def test_wall_clock_zero_budget_fails_immediately():
    script = 'await send_message("hi")\n'
    result, emitter, _ = await _run(script, budget=HandlerBudget(wall_clock_seconds=0.0))
    assert result.outcome == "cap_exceeded"
    assert result.cap == "wall_clock"
    assert emitter.messages == []


async def test_compile_error_is_captured_not_raised():
    result, _, _ = await _run("this is not valid python ::::\n")
    assert result.outcome == "error"
    assert "compile" in (result.error or "")
