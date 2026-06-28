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


@dataclass
class _FakeActor:
    calls: list = field(default_factory=list)

    async def ban_user(self, user_id, reason=None):
        self.calls.append(("ban", user_id, reason)); return f"banned {user_id}"

    async def kick_user(self, user_id):
        self.calls.append(("kick", user_id)); return f"kicked {user_id}"

    async def timeout_user(self, user_id, duration_seconds=600):
        self.calls.append(("timeout", user_id, duration_seconds)); return "ok"

    async def delete_message(self, channel_id, message_id):
        self.calls.append(("delete", channel_id, message_id)); return "ok"


async def _run(script, *, budget=None, emitter=None, limiter=None, agent_runner=None, actor=None):
    emitter = emitter or _FakeEmitter()
    limiter = limiter or _StubLimiter()
    kwargs = {}
    if agent_runner is not None:
        kwargs["agent_runner"] = agent_runner
    if actor is not None:
        kwargs["actor"] = actor
    result = await run_handler_script(
        script,
        {
            "trigger_type": "message",
            "message_content": "hi",
            "message_id": "M1",
            "author_id": "U1",
        },
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


# -- admin handlers (actor set) ------------------------------------------------

from smarter_dev.web.handler_budget import admin_budget


async def test_standard_handler_has_no_admin_functions():
    # No actor -> ban_user is not defined in the sandbox -> NameError -> error.
    result, _, _ = await _run('await ban_user("U1")\n')
    assert result.outcome == "error"


async def test_standard_handler_cannot_send_to_other_channel():
    result, emitter, _ = await _run('await send_message("hi", "OTHER")\n')
    assert result.outcome == "cap_exceeded"
    assert result.cap == "cross_channel_send"
    assert emitter.messages == []


async def test_admin_handler_moderation_metered():
    actor = _FakeActor()
    script = (
        'await delete_message(context["message_id"], "C1")\n'
        'await ban_user(context["author_id"], "scam")\n'
        'await send_message("banned a scammer", "MODCHAT")\n'
    )
    result, emitter, _ = await _run(
        script, budget=admin_budget(), actor=actor,
        # context needs message_id/author_id
    )
    assert result.outcome == "ok", result.error
    assert ("delete", "C1", "hi") not in actor.calls  # message_id is from context
    kinds = [c[0] for c in actor.calls]
    assert "delete" in kinds and "ban" in kinds
    assert result.usage["mod_actions"] == 2
    assert ("MODCHAT", "banned a scammer") in emitter.messages  # admin cross-channel ok


async def test_admin_mod_action_cap():
    actor = _FakeActor()
    script = "for i in range(5):\n    await kick_user(str(i))\n"
    result, _, _ = await _run(
        script, budget=HandlerBudget(max_mod_actions=3, max_messages=5), actor=actor
    )
    assert result.outcome == "cap_exceeded"
    assert result.cap == "mod_actions"
    assert len(actor.calls) == 3


async def test_random_globals_available_without_import():
    # Deterministic picks so the assertion is stable: randint(5,5)=5,
    # choice/shuffled/sample over singletons/known sizes.
    script = (
        "r = randint(5, 5)\n"
        'c = choice(["only"])\n'
        "s = shuffled([1])\n"
        "p = sample([1, 2, 3], 2)\n"
        'await send_message(f"{r}{c}{len(s)}{len(p)}")\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "5only12"


async def test_import_random_still_errors():
    # random is injected as globals; `import random` must still fail loud so the
    # judge's "reject disallowed imports" guidance matches runtime reality.
    result, emitter, _ = await _run("import random\nawait send_message('x')\n")
    assert result.outcome == "error"
    assert "random" in (result.error or "")
    assert emitter.messages == []


async def test_datetime_now_returns_host_clock():
    # datetime.now(tz) is an OS call Monty refuses unless the host wires a clock;
    # _clock_os grants it. A script must be able to compute a recency window
    # against an ISO timestamp from context — the canonical "new account" guard.
    script = (
        "import datetime\n"
        "now = datetime.datetime.now(datetime.timezone.utc)\n"
        'joined = datetime.datetime.fromisoformat("2000-01-01T00:00:00+00:00")\n'
        "age = (now - joined).total_seconds()\n"
        'await send_message("old" if age > 86400 else "new")\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "old"  # joined in year 2000 -> well past 24h


async def test_date_today_available():
    script = (
        "import datetime\n"
        "y = datetime.date.today().year\n"
        'await send_message("yes" if y >= 2026 else "no")\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "ok", result.error
    assert emitter.messages[0][1] == "yes"


async def test_filesystem_still_blocked_with_clock_wired():
    # Wiring the clock OS callback must not crack open any other OS surface:
    # filesystem reads still fail loud.
    script = (
        "from pathlib import Path\n"
        'await send_message(Path("/etc/passwd").read_text())\n'
    )
    result, emitter, _ = await _run(script)
    assert result.outcome == "error"
    assert emitter.messages == []
