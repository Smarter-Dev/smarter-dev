"""Ordered Discord dispatch for fenced LaTeX in chatbot replies."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import hikari
import pytest

from smarter_dev.bot.services.chat_engine import ChannelEngine
from smarter_dev.bot.services.latex_renderer import LatexRenderer
from smarter_dev.bot.services.latex_renderer import LatexRenderError
from smarter_dev.bot.services.latex_renderer import RenderedLatex


def _forbidden() -> hikari.ForbiddenError:
    return hikari.ForbiddenError(
        "https://discord/api",
        {},
        "Missing Permissions",
        "Missing Permissions",
        50013,
    )


def _engine(create_message: AsyncMock, renderer: object) -> ChannelEngine:
    bot = MagicMock()
    bot.rest = MagicMock()
    bot.rest.create_message = create_message
    bot.d = {"latex_renderer": renderer}

    async def _noop_voice(*args, **kwargs):
        return None

    async def _noop_deactivate(_channel_id):
        return None

    return ChannelEngine(
        bot=bot,
        channel_id=42,
        guild_id=99,
        voice_send=_noop_voice,
        on_deactivate=_noop_deactivate,
    )


async def test_fenced_response_sends_text_image_text_in_order():
    renderer = MagicMock()
    renderer.render = AsyncMock(
        return_value=RenderedLatex(data=b"PNG", filename="equation.png")
    )
    create_message = AsyncMock()
    engine = _engine(create_message, renderer)

    sent = await engine._send_fenced_response(
        "before\n```latex\nE = mc^2\n```\nafter",
        reply_to=101,
    )

    assert sent is True
    renderer.render.assert_awaited_once_with("E = mc^2")
    calls = create_message.await_args_list
    assert len(calls) == 3
    assert calls[0].kwargs == {"content": "before", "reply": 101}
    assert "attachment" in calls[1].kwargs
    assert "reply" not in calls[1].kwargs
    assert calls[2].kwargs == {"content": "after"}


async def test_delimiter_response_sends_text_image_text_in_order():
    renderer = MagicMock()
    renderer.render = AsyncMock(
        return_value=RenderedLatex(data=b"PNG", filename="equation.png")
    )
    create_message = AsyncMock()
    engine = _engine(create_message, renderer)

    sent = await engine._send_fenced_response(
        "area is $$\\pi r^2$$ done",
        reply_to=101,
    )

    assert sent is True
    renderer.render.assert_awaited_once_with("\\pi r^2")
    calls = create_message.await_args_list
    assert len(calls) == 3
    assert calls[0].kwargs == {"content": "area is", "reply": 101}
    assert "attachment" in calls[1].kwargs
    assert "reply" not in calls[1].kwargs
    assert calls[2].kwargs == {"content": "done"}


async def test_response_starting_with_latex_puts_reply_anchor_on_image():
    renderer = MagicMock()
    renderer.render = AsyncMock(return_value=RenderedLatex(data=b"PNG"))
    create_message = AsyncMock()
    engine = _engine(create_message, renderer)

    await engine._send_fenced_response(
        "```latex\nx\n```\nafter",
        reply_to=101,
    )

    calls = create_message.await_args_list
    assert calls[0].kwargs["reply"] == 101
    assert "attachment" in calls[0].kwargs
    assert "reply" not in calls[1].kwargs


async def test_only_first_message_is_anchored_across_text_chunks_and_equations():
    renderer = MagicMock()
    renderer.render = AsyncMock(return_value=RenderedLatex(data=b"PNG"))
    create_message = AsyncMock()
    engine = _engine(create_message, renderer)
    leading_text = ("A" * 1200) + "\n" + ("B" * 1000)

    await engine._send_fenced_response(
        f"{leading_text}\n```latex\nx\n```\nafter",
        reply_to=101,
    )

    calls = create_message.await_args_list
    assert len(calls) == 4
    assert calls[0].kwargs["reply"] == 101
    assert all("reply" not in call.kwargs for call in calls[1:])
    assert "attachment" in calls[2].kwargs
    assert calls[3].kwargs["content"] == "after"


async def test_render_failure_sends_original_fence_in_place():
    renderer = MagicMock()
    renderer.render = AsyncMock(side_effect=LatexRenderError("bad formula"))
    create_message = AsyncMock()
    engine = _engine(create_message, renderer)

    await engine._send_fenced_response(
        "before\n```latex\nnot valid\n```\nafter",
        reply_to=None,
    )

    contents = [call.kwargs["content"] for call in create_message.await_args_list]
    assert contents == ["before", "```latex\nnot valid\n```", "after"]


async def test_attachment_failure_sends_original_fence_in_place():
    renderer = MagicMock()
    renderer.render = AsyncMock(return_value=RenderedLatex(data=b"PNG"))

    async def create_message(_channel_id, **kwargs):
        if "attachment" in kwargs:
            raise _forbidden()
        return MagicMock()

    create = AsyncMock(side_effect=create_message)
    engine = _engine(create, renderer)

    sent = await engine._send_fenced_response(
        "before\n```latex\nx\n```\nafter",
        reply_to=None,
    )

    assert sent is True
    calls = create.await_args_list
    assert len(calls) == 4
    assert calls[0].kwargs["content"] == "before"
    assert "attachment" in calls[1].kwargs
    assert calls[2].kwargs["content"] == "```latex\nx\n```"
    assert calls[3].kwargs["content"] == "after"


async def test_missing_renderer_sends_original_fence():
    create_message = AsyncMock()
    engine = _engine(create_message, renderer=None)

    await engine._send_fenced_response("```latex\nx\n```", reply_to=None)

    create_message.assert_awaited_once()
    assert create_message.await_args.kwargs["content"] == "```latex\nx\n```"


def test_both_answer_prompts_instruct_latex_delimiters():
    root = Path(__file__).resolve().parents[3]
    prompt_paths = [
        root / "smarter_dev/bot/agents/prompts/chat_agent.md",
        root / "smarter_dev/bot/agents/prompts/writer_agent.md",
    ]

    for prompt_path in prompt_paths:
        prompt = prompt_path.read_text(encoding="utf-8")
        # New guidance: standard delimiters, not the ```latex fence.
        assert "write math with standard LaTeX delimiters" in prompt
        assert "$$…$$" in prompt and "\\(…\\)" in prompt
        assert "A lone `$` is never treated as math" in prompt
        # The old fence-only instruction and its blanket ban are gone.
        assert "fenced block whose language is exactly `latex`" not in prompt
        assert "Never use `$`, `$$`, `\\(`, or `\\[`" not in prompt


@pytest.mark.asyncio
async def test_real_worker_renders_png_when_node_dependencies_are_installed():
    root = Path(__file__).resolve().parents[3]
    worker = root / "latex_renderer" / "worker.mjs"
    dependencies = root / "latex_renderer" / "node_modules"
    if not dependencies.exists():
        pytest.skip("Node renderer dependencies are not installed")

    renderer = LatexRenderer(worker_path=worker)
    try:
        result = await renderer.render(r"E = mc^2")
    finally:
        await renderer.close()

    assert result.mime_type == "image/png"
    assert result.data.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_worker_startup_has_a_separate_readiness_timeout(tmp_path):
    worker = tmp_path / "slow-worker.mjs"
    worker.write_text(
        """
import { createInterface } from "node:readline";
await new Promise(resolve => setTimeout(resolve, 75));
process.stdout.write(JSON.stringify({ready: true}) + "\\n");
const lines = createInterface({input: process.stdin, crlfDelay: Infinity});
for await (const line of lines) {
  const request = JSON.parse(line);
  process.stdout.write(JSON.stringify({
    id: request.id,
    png: Buffer.from("PNG").toString("base64"),
  }) + "\\n");
}
""",
        encoding="utf-8",
    )
    renderer = LatexRenderer(
        worker_path=worker,
        timeout_seconds=0.05,
        startup_timeout_seconds=1.0,
    )
    try:
        result = await renderer.render("x")
    finally:
        await renderer.close()

    assert result.data == b"PNG"
