"""Tests for the chat agent's sandboxed ``run_code`` tool.

These execute real Pydantic Monty (fast, deterministic, no network) and verify
output capture, error surfacing, and that ``reason`` is posted as a status.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from smarter_dev.bot.agents.chat_tools import ChatDeps, run_code


def _ctx() -> SimpleNamespace:
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = AsyncMock()
    return SimpleNamespace(deps=ChatDeps(bot=bot, channel_id=1, guild_id=2))


@pytest.mark.asyncio
async def test_run_code_returns_stdout_and_value():
    ctx = _ctx()
    out = await run_code(ctx, "adding numbers", "x = 2 + 3\nprint(x)\nx")
    assert "stdout:" in out
    assert "5" in out
    assert "return value: 5" in out


@pytest.mark.asyncio
async def test_run_code_posts_reason_as_status():
    ctx = _ctx()
    await run_code(ctx, "doing a calculation", "1 + 1")
    ctx.deps.bot.rest.create_message.assert_awaited_once()
    channel_id, content = ctx.deps.bot.rest.create_message.call_args.args
    assert channel_id == 1
    assert "doing a calculation" in content
    assert content.startswith("> -#")


@pytest.mark.asyncio
async def test_run_code_compile_error():
    ctx = _ctx()
    out = await run_code(ctx, "broken", "def (:")
    assert "COMPILE ERROR" in out


@pytest.mark.asyncio
async def test_run_code_runtime_error():
    ctx = _ctx()
    out = await run_code(ctx, "divide", "1 / 0")
    assert "RUNTIME ERROR" in out
    assert "ZeroDivisionError" in out


@pytest.mark.asyncio
async def test_run_code_has_no_network_access():
    ctx = _ctx()
    # Third-party / network imports are unavailable in the sandbox.
    out = await run_code(ctx, "try network", "import requests")
    assert "ERROR" in out


@pytest.mark.asyncio
async def test_run_code_stdlib_computation():
    ctx = _ctx()
    out = await run_code(
        ctx,
        "count hex colors",
        "import re\n"
        "vals = ['#000000', '#FFF', '#abcdez', '#AABBCC']\n"
        "len([v for v in vals if re.fullmatch(r'#[0-9a-fA-F]{6}', v)])",
    )
    assert "return value: 2" in out
